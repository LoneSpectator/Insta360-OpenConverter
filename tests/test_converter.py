import io
from contextlib import redirect_stdout
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from converter import workspace
from converter.audit import Cancelled, Runner
from converter.config import Config
from converter.scan import Job, digest, scan
from converter.service import Service
from converter.sidecar import metadata_path
from scripts.prepare_sdk import prepare, unpack


EXIFTOOL = os.environ.get("EXIFTOOL", "exiftool")


class MemoryAudit:
    run_id = "test-run"

    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photo = self.root / "photo"
        self.photo.mkdir()
        self.event = self.photo / "旅行" / "20261102.中文 空格事件"
        self.event.mkdir(parents=True)
        self.data = self.root / "data"
        self.data.mkdir()
        self.audit = MemoryAudit()
        self.cfg = Config(photo_root=self.photo, data_root=self.data, settle_seconds=0,
                          width=128, height=64, exiftool=EXIFTOOL)

    def file(self, name, content=b"source"):
        path = self.event / name
        path.write_bytes(content)
        return path


class ScannerTests(Workspace):
    def test_pairs_and_single_file_dual_track_and_lrv(self):
        for name in ["VID_20261102_00_001.insv", "VID_20261102_10_001.insv",
                     "VID_20261102_00_002.INSV", "LRV_20261102_01_003.lrv", "IMG_1.INSP"]:
            self.file(name)
        jobs = scan(self.photo, self.audit)
        self.assertEqual(len(jobs), 4)
        pair = next(job for job in jobs if len(job.sources) == 2)
        self.assertIn("_00_", pair.primary.name)
        self.assertTrue(pair.output.name.endswith("_preview.mp4"))

    def test_orphan_lens_is_deferred(self):
        self.file("VID_20261102_10_001.insv")
        job, = scan(self.photo, self.audit)
        self.assertIn("Missing _00_", job.blocked)

    def test_symlinks_and_generated_files_are_excluded(self):
        original = self.file("real.insp")
        (self.event / "link.insp").symlink_to(original)
        (self.photo / "link").symlink_to(self.event, target_is_directory=True)
        self.file("real_preview.insp")
        temp = self.event / ".insta360-preview-stale"
        temp.mkdir()
        (temp / "partial.insp").write_bytes(b"x")
        self.assertEqual([j.primary for j in scan(self.photo, self.audit)], [original])

    def test_fixed_workspace_contents_are_excluded_and_unmanaged_files_preserved(self):
        original = self.file("real.insv")
        root = self.event / ".insta360-openconverter"
        root.mkdir()
        unmanaged = root / "unmanaged"
        unmanaged.mkdir()
        direct = root / "fake.insv"
        nested = unmanaged / "fake.insp"
        direct.write_bytes(b"unmanaged file: keep this")
        nested.write_bytes(b"unmanaged directory: keep this too")

        self.assertEqual([job.primary for job in scan(self.photo, self.audit)], [original])
        self.assertEqual(direct.read_bytes(), b"unmanaged file: keep this")
        self.assertEqual(nested.read_bytes(), b"unmanaged directory: keep this too")

    def test_output_collisions_block_all_owners(self):
        self.file("same.insv")
        self.file("same.lrv")
        self.assertTrue(all(j.blocked for j in scan(self.photo, self.audit)))

    def test_legacy_cpu_configuration_is_rejected(self):
        config = self.data / "config.toml"
        for setting in ('mode = "cpu"', 'mode = "gpu"', "cpu_compatible = true"):
            with self.subTest(setting=setting):
                config.write_text("\n".join([
                    f"photo_root = {json.dumps(str(self.photo))}", "[sdk]", setting, "",
                ]), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Unknown configuration keys"):
                    Config.load(config, self.data)

    def test_invalid_dimensions_and_overlapping_roots(self):
        self.cfg.width = 127
        with self.assertRaises(ValueError):
            self.cfg.validate()
        self.cfg.width = 128
        self.cfg.data_root = self.photo / "data"
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.cfg.validate()

    def test_extra_arguments_cannot_redirect_output_to_an_original(self):
        self.cfg.extra_args = ["-output", "/photo/source.insv"]
        with self.assertRaisesRegex(ValueError, "override"):
            self.cfg.validate()

    def test_extra_arguments_cannot_override_gpu_processing(self):
        for prefix in ("-", "--"):
            arguments_list = [[prefix + "disable_cuda"]]
            for value in ("true", "false"):
                arguments_list.extend([
                    [prefix + "disable_cuda", value],
                    [prefix + "disable_cuda=" + value],
                ])
            for value in ("cpu", "CPU", "Cpu"):
                arguments_list.extend([
                    [prefix + "image_processing_accel", value],
                    [prefix + "image_processing_accel=" + value],
                ])
            for arguments in arguments_list:
                with self.subTest(arguments=arguments):
                    self.cfg.extra_args = arguments
                    with self.assertRaises(ValueError):
                        self.cfg.validate()

    def test_gpu_processing_allows_software_codecs_and_auto_acceleration(self):
        for prefix in ("-", "--"):
            for arguments in ([prefix + "enable_soft_encode", prefix + "enable_soft_decode"],
                              [prefix + "image_processing_accel", "auto"],
                              [prefix + "image_processing_accel=auto"]):
                with self.subTest(arguments=arguments):
                    self.cfg.extra_args = arguments
                    self.cfg.validate()

    def test_sdk_model_or_library_update_invalidates_completed_job_fingerprint(self):
        binary = self.data / "sdk/media/bin/MediaSDKTest"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"unchanged executable")
        self.cfg.executable = str(binary)
        manifest = binary.parent.parent / ".insta360-bootstrap.json"
        manifest.write_text(json.dumps({"runtime_sha256": "original models and libraries"}))
        original = self.cfg.fingerprint()
        manifest.write_text(json.dumps({"runtime_sha256": "updated models or libraries"}))
        self.assertNotEqual(self.cfg.fingerprint(), original)
        self.assertEqual(binary.read_bytes(), b"unchanged executable")


class InstanceLockTests(Workspace):
    def test_all_commands_lock_before_preparing_shared_sdk_runtimes(self):
        from converter.__main__ import main
        service = Service(self.cfg, self.audit, Runner(self.audit, threading.Event(), 1))
        self.addCleanup(service.close)
        (self.data / "config.toml").write_text(
            "photo_root = " + json.dumps(str(self.photo)), encoding="utf-8")
        for command in ("doctor", "once", "watch", "metadata"):
            argv = ["--data-dir", str(self.data), command]
            if command == "metadata":
                argv.append(str(self.event / "test.insp"))
            with self.subTest(command=command), patch("converter.__main__.prepare_sdks") as prepare:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(argv), 1)
                prepare.assert_not_called()
        self.assertFalse((self.data / "sdk").exists())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.audit = MemoryAudit()
        self.stop = threading.Event()
        self.runner = Runner(self.audit, self.stop, 1)

    def test_captures_stdout_separately_from_stderr(self):
        output = self.runner.run([sys.executable, "-c",
                                  'import sys; print("json"); print("diagnostic", file=sys.stderr)'], capture=True)
        self.assertEqual(output.strip(), "json")
        self.assertTrue(any(v.get("stream") == "stderr" for _, v in self.audit.events))

    def test_nonzero_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "exited 7"):
            self.runner.run([sys.executable, "-c", "raise SystemExit(7)"])

    def test_timeout_terminates_process(self):
        with self.assertRaises(TimeoutError):
            self.runner.run([sys.executable, "-c", "import time; time.sleep(10)"])
        self.assertTrue(any(k == "command.terminate" for k, _ in self.audit.events))

    def test_timeout_after_pipes_are_closed(self):
        with self.assertRaises(TimeoutError):
            self.runner.run([sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"])

    def test_shutdown_cancels_inflight_command(self):
        timer = threading.Timer(0.2, self.stop.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                self.runner.run([sys.executable, "-c", "import time; time.sleep(10)"])
        finally:
            timer.join()


class ArchiveTests(Workspace):
    def test_rejects_archive_escape_and_symlinks(self):
        for name, link in [("../escape", False), ("link", True)]:
            archive = self.root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                    tar.addfile(member)
                else:
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(ValueError):
                unpack(archive, self.data)

    def test_prepares_sdk_tree_without_installing_it(self):
        archive = self.root / "sdk.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, payload in [("SDK/bin/MediaSDKTest", b"executable"),
                                  ("SDK/lib/library.so", b"library"),
                                  ("SDK/bin/models/model.ins", b"model")]:
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = 0o755
                tar.addfile(member, io.BytesIO(payload))
        destination = self.root / "prepared"
        prepare(archive, destination, digest(archive))
        self.assertTrue((destination / "bin/MediaSDKTest").is_file())
        with self.assertRaises(ValueError):
            prepare(archive, destination)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            prepare(archive, self.root / "wrong-hash", "0" * 64)


@unittest.skipUnless(shutil.which(EXIFTOOL) and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Integration tests require ffmpeg, ffprobe and ExifTool")
class PipelineTests(Workspace):
    """Real media tools + an explicit SDK double; does not validate proprietary stitching."""

    def setUp(self):
        super().setUp()
        self.video = self.root / "blank.mp4"
        self.image = self.root / "blank.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=128x64:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.5",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.video)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.video), "-frames:v", "1",
                        str(self.image)], check=True)
        self.sdk = self.root / "fake_sdk.py"
        self.sdk.write_text(f'''#!{sys.executable}
import os, shutil, sys
from pathlib import Path
if "-help" in sys.argv:
    print("-inputs -output -model_root_dir -output_size")
    sys.exit(255)  # Some official demo versions do this on help.
source = Path(sys.argv[sys.argv.index("-inputs") + 1])
output = Path(sys.argv[sys.argv.index("-output") + 1])
if "bad" in source.name:
    print("SDK error callback (simulated)", file=sys.stderr)
    sys.exit(0)  # Must still be treated as failed: there is no output.
fixture = {str(self.image)!r} if output.suffix == ".jpg" else {str(self.video)!r}
shutil.copyfile(fixture, output)
''', encoding="utf-8")
        self.sdk.chmod(0o755)
        self.cfg.executable = str(self.sdk)
        self.cfg.models = str(self.root / "models")
        Path(self.cfg.models).mkdir()
        self.metadata_sdk = self.root / "fake_metadata.py"
        self.metadata_payload = self.root / "sdk-payload.json"
        self.metadata_sdk.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
schema = {{"reader": "insta360-metadata", "schema_version": 1, "sdk": "InsMetaDataSDK"}}
if sys.argv[1:] == ["--schema"]:
    print(json.dumps(schema))
    sys.exit(0)
source = Path(sys.argv[1])
directory = Path(sys.argv[2])
if not source.is_file() or not directory.is_dir() or any(directory.iterdir()):
    print("Expected a regular input file and an empty output directory", file=sys.stderr)
    sys.exit(1)
if "metadata-fail" in source.name:
    print("InsMetaDataSDK Parse failed (simulated)", file=sys.stderr)
    sys.exit(3)
payload = Path({str(self.metadata_payload)!r})
data = json.loads(payload.read_text()) if payload.exists() else {{}}
tracks = {{}}
for name in ("gps", "gyro", "exposure", "timelapse"):
    values = data.pop(name, [])
    (directory / (name + ".jsonl")).write_text("".join(json.dumps(value) + "\\n" for value in values))
    tracks[name] = {{"available": bool(values), "count": len(values), "file": name + ".jsonl"}}
summary = {{**schema, "camera_type": "", "firmware_version": "", "serial_number": "",
           "gyro_first_timestamp_ms": 0, **data, "tracks": tracks}}
(directory / "sdk.json").write_text(json.dumps(summary, indent=4))
''', encoding="utf-8")
        self.metadata_sdk.chmod(0o755)
        self.cfg.metadata_reader = str(self.metadata_sdk)
        self.cfg.metadata_library_dirs = []
        self.runner = Runner(self.audit, threading.Event(), 30)
        self.service = Service(self.cfg, self.audit, self.runner)
        self.addCleanup(lambda: self.service.close())

    def source(self, name):
        path = self.event / name
        shutil.copyfile(self.image if path.suffix.lower() == ".insp" else self.video, path)
        subprocess.run([EXIFTOOL, "-overwrite_original", "-XMP-exif:DateTimeOriginal=2024:06:01 12:34:56+08:00",
                        "-XMP-tiff:Make=Insta360", "-XMP-tiff:Model=X4",
                        "-XMP-exif:GPSLatitude=31.25", "-XMP-exif:GPSLongitude=-121.5", str(path)],
                       check=True, stdout=subprocess.DEVNULL)
        return path

    def test_metadata_output_is_empty_and_separate_from_workspace_control_files(self):
        source = self.source("metadata-output.insp")
        original = digest(source)
        gyro = [{"timestamp": 123, "rotation": [1.25, -2.0, 3.0]}]
        self.metadata_payload.write_text(json.dumps({"gyro": gyro}, indent=4), encoding="utf-8")
        extract = self.service.media.metadata_sdk.extract
        destinations = []

        def inspect_output(path, directory):
            self.assertEqual(path, source)
            self.assertEqual(list(directory.iterdir()), [])
            workspace = directory.parent
            self.assertEqual(workspace.parent, self.event / ".insta360-openconverter")
            self.assertTrue(workspace.name.startswith("metadata-read-"))
            self.assertTrue((workspace / ".workspace.json").is_file())
            self.assertTrue((workspace / ".workspace.lock").is_file())
            destinations.append(directory)
            return extract(path, directory)

        with patch.object(self.service.media.metadata_sdk, "extract", inspect_output):
            summary = self.service.media.source_metadata(source)
        self.assertEqual(len(destinations), 1)
        actual = json.loads(metadata_path(source).read_text(encoding="utf-8"))
        self.assertEqual(actual["Insta360SDK"]["tracks"]["gyro"]["records"], gyro)
        self.assertEqual(summary["Insta360SDK"]["tracks"]["gyro"]["count"], 1)
        self.assertFalse((self.event / ".insta360-openconverter").exists())
        self.assertEqual(digest(source), original)

    def test_photo_metadata_idempotence_and_original_unchanged(self):
        source = self.source("IMG_中文 照片.INSP")
        original = digest(source)
        original_time = source.stat().st_mtime_ns
        self.service.doctor()
        self.assertEqual(self.service.scan_once()["success"], 1)
        output = Job((source,)).output
        metadata = self.service.media.metadata(output)
        self.assertEqual(metadata["XMP-GPano:ProjectionType"], "equirectangular")
        self.assertEqual(metadata["XMP-exif:DateTimeOriginal"], "2024:06:01 12:34:56+08:00")
        self.assertAlmostEqual(float(metadata["XMP-exif:GPSLongitude"]), -121.5)
        self.assertEqual(metadata["XMP-tiff:Model"], "X4")
        self.assertEqual(digest(source), original)
        self.assertEqual(output.stat().st_mtime_ns, original_time)
        self.assertEqual(self.service.scan_once()["skipped"], 1)
        self.assertFalse((self.event / ".insta360-openconverter").exists())
        self.assertTrue(metadata_path(source).is_file())
        self.assertFalse((self.data / "metadata").exists())
        report = json.loads(Job((source,)).report.read_text())
        self.assertEqual(report["files"][source.name]["status"], "success")

    def test_video_gets_real_spherical_uuid_and_retains_audio(self):
        source = self.source("VID_20261102_00_001.insv")
        self.assertEqual(self.service.scan_once()["success"], 1)
        output = Job((source,)).output
        self.assertIn(bytes.fromhex("ffcc8263f8554a938814587a02521fdd"), output.read_bytes())
        metadata = self.service.media.metadata(output)
        self.assertEqual(metadata["XMP-GSpherical:ProjectionType"], "equirectangular")
        self.assertEqual(metadata["XMP-exif:DateTimeOriginal"], "2024:06:01 12:34:56+08:00")
        probe = self.service.media.probe(output)
        self.assertTrue(any(s["codec_type"] == "audio" for s in probe["streams"]))

    def test_no_capture_time_is_not_fabricated(self):
        source = self.event / "no-time.insp"
        shutil.copyfile(self.image, source)
        self.assertEqual(self.service.scan_once()["success"], 1)
        actual = self.service.media.metadata(Job((source,)).output)
        self.assertNotIn("XMP-exif:DateTimeOriginal", actual)
        self.assertTrue(any(event == "metadata.capture_time_missing" for event, _ in self.audit.events))

    def test_pair_replaces_old_orphan_report(self):
        secondary = self.source("VID_20261102_10_001.insv")
        self.assertEqual(self.service.scan_once()["deferred"], 1)
        primary = self.source("VID_20261102_00_001.insv")
        self.assertEqual(self.service.scan_once()["success"], 1)
        report = json.loads(Job((primary,)).report.read_text())
        for source in (primary, secondary):
            self.assertEqual(report["files"][source.name]["status"], "success")
            self.assertTrue(metadata_path(source).is_file())
        self.assertEqual(Job((primary,)).report, Job((secondary,)).report)
        self.assertEqual(len(list(self.event.glob("*_preview.mp4"))), 1)
        primary.unlink()
        self.assertEqual(self.service.scan_once()["deferred"], 1)
        report = json.loads(Job((secondary,)).report.read_text())
        self.assertEqual(report["files"][secondary.name]["status"], "deferred")

    def test_missing_secondary_metadata_is_repaired_without_reencoding_preview(self):
        primary = self.source("VID_20261102_00_007.insv")
        secondary = self.source("VID_20261102_10_007.insv")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((primary, secondary)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        metadata_path(secondary).unlink()
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 1)
        self.assertTrue(metadata_path(primary).is_file())
        self.assertTrue(metadata_path(secondary).is_file())
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(len(list(self.event.glob("*_preview.mp4"))), 1)
        self.assertTrue(any(event == "job.metadata_repaired" for event, _ in self.audit.events))
        report = json.loads(Job((primary, secondary)).report.read_text())
        for source in (primary, secondary):
            self.assertEqual(report["files"][source.name]["status"], "success")

    def test_directory_report_merges_mixed_statuses_and_keeps_one_file_per_directory(self):
        good = self.source("good.insp")
        bad = self.source("bad.insv")
        waiting = self.source("VID_20261102_10_008.insv")
        nested = self.event / "nested"
        nested.mkdir()
        other = self.source("nested/other.insp")
        counts = self.service.scan_once()
        self.assertEqual((counts["success"], counts["failed"], counts["deferred"]), (2, 1, 1))
        report_path = Job((good,)).report
        report = json.loads(report_path.read_text())
        self.assertEqual(report_path.name, "Insta360_OpenConverter_convert_report.txt")
        self.assertEqual(set(report["files"]), {good.name, bad.name, waiting.name})
        self.assertEqual(report["files"][good.name]["status"], "success")
        self.assertEqual(report["files"][bad.name]["status"], "failed")
        self.assertEqual(report["files"][waiting.name]["status"], "deferred")
        self.assertTrue(all(entry["present"] for entry in report["files"].values()))
        self.assertEqual(next(iter(report["files"])), bad.name)
        self.assertEqual(report["summary"], {
            "failed": 1, "total": 3, "processing": 0, "pending": 0, "deferred": 1, "success": 1,
        })
        failure = report["files"][bad.name]
        self.assertRegex(failure["error"], "[\u4e00-\u9fff]")
        self.assertEqual(failure["error_code"], "sdk_no_output")
        self.assertIn("nonempty output file", failure["error_raw"])
        self.assertRegex(report["files"][waiting.name]["reason"], "[\u4e00-\u9fff]")
        self.assertIn("Missing _00_ lens", report["files"][waiting.name]["reason_raw"])
        self.assertEqual(list(self.event.glob("*.txt")), [report_path])
        other_report = Job((other,)).report
        self.assertEqual(list(nested.glob("*.txt")), [other_report])
        self.assertEqual(set(json.loads(other_report.read_text())["files"]), {other.name})

    def test_directory_report_shows_processing_and_all_pending_files_during_conversion(self):
        first = self.source("aaa-good.insp")
        failing = self.source("zzz-bad.insv")
        convert = self.service.media.convert
        conversions = []

        def inspect_progress(job, output, plan=None):
            temporary_root = self.event / ".insta360-openconverter"
            self.assertEqual(output.parent.parent, temporary_root)
            self.assertTrue(temporary_root.is_dir())
            self.assertTrue(output.parent.is_dir())
            self.assertFalse(list(self.event.glob(".insta360-preview-*")))
            self.assertFalse(list(self.event.glob(".insta360-metadata-*")))
            if not conversions:
                report = json.loads(job.report.read_text())
                self.assertEqual(set(report["files"]), {first.name, failing.name})
                self.assertEqual(report["files"][first.name]["status"], "processing")
                self.assertEqual(report["files"][failing.name]["status"], "pending")
                self.assertTrue(all(entry["present"] for entry in report["files"].values()))
                self.assertEqual(report["summary"], {
                    "failed": 0, "total": 2, "processing": 1, "pending": 1,
                    "deferred": 0, "success": 0,
                })
            conversions.append(job.primary)
            return convert(job, output, plan)

        with patch.object(self.service.media, "convert", inspect_progress):
            counts = self.service.scan_once()
        self.assertEqual((counts["success"], counts["failed"]), (1, 1))
        self.assertEqual(conversions, [first, failing])
        report = json.loads(Job((first,)).report.read_text())
        self.assertEqual(list(report["files"]), [failing.name, first.name])
        self.assertEqual(report["summary"], {
            "failed": 1, "total": 2, "processing": 0, "pending": 0,
            "deferred": 0, "success": 1,
        })
        self.assertRegex(report["files"][failing.name]["error"], "[\u4e00-\u9fff]")
        self.assertIn("nonempty output file", report["files"][failing.name]["error_raw"])
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_legacy_directory_report_migrates_history_without_reencoding(self):
        source = self.source("report-migration.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((source,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        report_path = Job((source,)).report
        report_path.unlink()
        legacy = self.event / "insta360-preview-report.txt"
        legacy.write_text(
            "Insta360 OpenConverter directory report v1\n旧版目录处理说明\n\n"
            + json.dumps({"files": {
                source.name: {"status": "success", "run_id": "legacy-run"},
                "removed.insv": {"status": "failed", "error": "Historical SDK failure",
                                 "note": "Keep this historical audit detail"},
            }}, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 1)

        self.assertFalse(legacy.exists())
        self.assertEqual(report_path.name, "Insta360_OpenConverter_convert_report.txt")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["files"][source.name]["status"], "success")
        self.assertTrue(report["files"][source.name]["present"])
        historical = report["files"]["removed.insv"]
        self.assertEqual(historical["status"], "failed")
        self.assertFalse(historical["present"])
        self.assertEqual(historical["note"], "Keep this historical audit detail")
        self.assertEqual(historical["error_raw"], "Historical SDK failure")
        self.assertEqual(report["summary"], {
            "failed": 0, "total": 1, "processing": 0, "pending": 0,
            "deferred": 0, "success": 1,
        })
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(list(self.event.glob("*.txt")), [report_path])

    def test_missing_directory_report_restores_all_skipped_successes_without_reencoding(self):
        first = self.source("first.insp")
        second = self.source("second.insp")
        self.assertEqual(self.service.scan_once()["success"], 2)
        report_path = Job((first,)).report
        report_path.unlink()
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 2)
        report = json.loads(report_path.read_text())
        self.assertEqual(set(report["files"]), {first.name, second.name})
        self.assertTrue(all(entry["status"] == "success" for entry in report["files"].values()))
        self.assertEqual(list(self.event.glob("*.txt")), [report_path])

    def test_removing_last_original_updates_progress_without_erasing_audit_record(self):
        source = self.source("removed.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        report_path = Job((source,)).report
        source.unlink()
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            counts = self.service.scan_once()
        self.assertEqual(sum(counts.values()), 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["summary"]["total"], 0)
        self.assertEqual(report["summary"]["success"], 0)
        self.assertFalse(report["files"][source.name]["present"])
        self.assertEqual(report["files"][source.name]["status"], "success")

    def test_same_stem_sources_are_blocked_before_metadata_name_collision(self):
        image = self.source("same.insp")
        video = self.source("same.insv")
        counts = self.service.scan_once()
        self.assertEqual((counts["success"], counts["failed"], counts["deferred"]), (0, 0, 2))
        self.assertEqual(metadata_path(image).name, "same_metadata.json")
        self.assertEqual(metadata_path(video), metadata_path(image))
        report = json.loads(Job((image,)).report.read_text())
        for source in (image, video):
            self.assertFalse(Job((source,)).output.exists())
            self.assertEqual(report["files"][source.name]["status"], "deferred")
            self.assertIn("同名元数据", report["files"][source.name]["reason"])
            self.assertIn("metadata", report["files"][source.name]["reason_raw"].lower())
        self.assertFalse(metadata_path(image).exists())
        self.assertFalse((self.data / "metadata").exists())

    def test_old_metadata_storage_migrates_without_reencoding_completed_preview(self):
        source = self.source("migration.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((source,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        old = self.service.state.get(source)
        detail = json.loads(old["detail"])
        historical = self.data / "metadata" / "old-snapshot.json"
        historical.parent.mkdir()
        historical.write_text('{\n    "historical": true\n}\n', encoding="utf-8")
        historical_bytes = historical.read_bytes()
        detail["metadata_snapshots"] = [str(historical)]
        detail.pop("metadata_info", None)
        self.service.state.put(source, old["fingerprint"], "success", **detail)
        metadata_path(source).unlink()
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 1)
        self.assertTrue(metadata_path(source).is_file())
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(historical.read_bytes(), historical_bytes)
        migrated = json.loads(self.service.state.get(source)["detail"])
        self.assertEqual(migrated["metadata_snapshots"], [str(metadata_path(source))])

    def test_failed_metadata_repair_retry_preserves_completed_preview_without_reencoding(self):
        source = self.source("repair-retry.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((source,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        metadata_path(source).unlink()
        with patch.object(self.service.media, "source_metadata", side_effect=RuntimeError("Temporary SDK failure")):
            self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 1)
        self.assertTrue(metadata_path(source).is_file())
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(self.service.state.get(source)["status"], "success")

    def test_externally_edited_metadata_is_preserved_and_reported_as_failure(self):
        source = self.source("metadata-edited.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        path = metadata_path(source)
        path.write_text('{"operator_note": "preserve this change"}\n', encoding="utf-8")
        previous = path.read_bytes()
        preview = Job((source,)).output
        preview_sha = digest(preview)
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(digest(preview), preview_sha)
        report = json.loads(Job((source,)).report.read_text())
        self.assertEqual(report["files"][source.name]["status"], "failed")
        self.assertRegex(report["files"][source.name]["error"], "[\u4e00-\u9fff]")
        self.assertIn("externally changed metadata", report["files"][source.name]["error_raw"])
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_sdk_false_success_does_not_stop_other_files(self):
        bad = self.source("bad.insv")
        good = self.source("good.insv")
        counts = self.service.scan_once()
        self.assertEqual((counts["failed"], counts["success"]), (1, 1))
        self.assertFalse(Job((bad,)).output.exists())
        self.assertTrue(Job((good,)).output.exists())
        counts = self.service.scan_once()
        self.assertEqual((counts["failed"], counts["success"], counts["skipped"]), (1, 0, 1))
        commands = [fields["argv"] for event, fields in self.audit.events
                    if event == "command.start" and fields["argv"][0] == str(self.sdk)]
        self.assertEqual(sum(str(bad) in argv for argv in commands), 2)
        self.assertEqual(sum(str(good) in argv for argv in commands), 1)
        self.assertTrue(any(event == "job.retry" and fields["source"] == bad
                            for event, fields in self.audit.events))
        self.assertFalse(any(event == "job.retry_delayed" for event, _ in self.audit.events))
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_report_cleanup_failure_does_not_fail_conversion_or_reencode_next_batch(self):
        source = self.source("cleanup-recovery.insp")
        original = digest(source)
        remove_child = workspace._remove_child
        failures = []

        def fail_first_cleanup(root_fd, name, child_fd):
            if not failures:
                failures.append(name)
                raise OSError(errno.ENOTEMPTY, "Directory not empty")
            return remove_child(root_fd, name, child_fd)

        with patch("converter.workspace._remove_child", side_effect=fail_first_cleanup):
            counts = self.service.scan_once()

        self.assertEqual((counts["success"], counts["failed"]), (1, 0))
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("report-"))
        preview = Job((source,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        report = json.loads(Job((source,)).report.read_text())
        self.assertEqual(report["files"][source.name]["status"], "success")
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["failures"], [])
        self.assertEqual(self.service.state.get(source)["status"], "success")
        self.assertTrue(any(event == "workspace.cleanup_failed" for event, _ in self.audit.events))
        self.assertFalse(any(event in ("report.scan_failed", "job.failed") for event, _ in self.audit.events))
        with patch.object(self.service.media, "convert", side_effect=AssertionError("Unexpected re-encoding")):
            self.assertEqual(self.service.scan_once()["skipped"], 1)
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(digest(source), original)
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_sdk_failure_remains_the_reported_cause_when_workspace_cleanup_also_fails(self):
        source = self.source("sdk-failure.insv")
        original = digest(source)
        remove_child = workspace._remove_child
        failures = []

        def fail_preview_cleanup(root_fd, name, child_fd):
            if name.startswith("preview-") and not failures:
                failures.append(name)
                raise OSError(errno.ENOTEMPTY, "Directory not empty")
            return remove_child(root_fd, name, child_fd)

        with patch("converter.workspace._remove_child", side_effect=fail_preview_cleanup):
            with patch.object(self.service.media, "convert", side_effect=RuntimeError("SDK original failure")):
                counts = self.service.scan_once()

        self.assertEqual((counts["failed"], counts["success"]), (1, 0))
        self.assertEqual(len(failures), 1)
        report = json.loads(Job((source,)).report.read_text())
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual([failure["file"] for failure in report["failures"]], [source.name])
        self.assertEqual(report["files"][source.name]["error_raw"], "SDK original failure")
        self.assertEqual(json.loads(self.service.state.get(source)["detail"])["error"], "SDK original failure")
        self.assertFalse(Job((source,)).output.exists())
        self.assertEqual(digest(source), original)
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_report_initialization_failure_is_attempted_once_and_preserves_completed_preview(self):
        completed = self.source("aaa-completed.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((completed,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        original = digest(completed)
        existing_detail = json.loads(self.service.state.get(completed)["detail"])
        pending = self.source("bbb-pending.insp")
        report_path = Job((completed,)).report
        report_path.unlink()
        sibling = self.event.with_name("20261103.独立事件")
        sibling.mkdir()
        independent = sibling / "independent.insp"
        shutil.copyfile(self.image, independent)
        link = os.link
        attempts = []

        def fail_report_publication(source, destination, *args, **kwargs):
            if Path(destination) == report_path:
                attempts.append(Path(destination))
                raise OSError(errno.ENOSPC, "Report filesystem full")
            return link(source, destination, *args, **kwargs)

        with patch("converter.reports.os.link", side_effect=fail_report_publication):
            with patch.object(self.service.media, "convert", wraps=self.service.media.convert) as convert:
                counts = self.service.scan_once()

        self.assertEqual((counts["failed"], counts["success"]), (2, 1))
        self.assertEqual(attempts, [report_path])
        self.assertEqual([call.args[0].primary for call in convert.call_args_list], [independent])
        self.assertTrue(Job((independent,)).output.is_file())
        independent_report = json.loads(Job((independent,)).report.read_text())
        self.assertEqual(independent_report["files"][independent.name]["status"], "success")
        self.assertFalse(Job((pending,)).output.exists())
        self.assertFalse(metadata_path(pending).exists())
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual(digest(completed), original)
        self.assertIn(previous[0], self.service.state.hashes(preview))
        failure = json.loads(self.service.state.get(completed)["detail"])
        self.assertIn("Report filesystem full", failure["error"])
        self.assertEqual(failure["preview_detail"], existing_detail)
        self.assertFalse(report_path.exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_next_batch_processes_new_files_and_skips_completed_previews(self):
        completed = self.source("existing.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        preview = Job((completed,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        new = self.source("new.insp")

        with patch.object(self.service.media, "convert", wraps=self.service.media.convert) as convert:
            counts = self.service.scan_once()

        self.assertEqual((counts["success"], counts["skipped"], counts["failed"]), (1, 1, 0))
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(convert.call_args.args[0].primary, new)
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertTrue(Job((new,)).output.is_file())
        report = json.loads(Job((new,)).report.read_text())
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["success"], 2)

    def test_restart_resumes_interrupted_batch_without_reencoding_completed_files(self):
        completed = self.source("aaa-completed.insp")
        interrupted = self.source("bbb-interrupted.insv")
        pending = self.source("ccc-pending.insp")
        originals = {source: digest(source) for source in (completed, interrupted, pending)}
        convert = self.service.media.convert

        def interrupt_second_job(job, output, plan=None):
            if job.primary == interrupted:
                output.write_bytes(b"partial SDK output")
                self.runner.stop.set()
                raise Cancelled("Simulated shutdown during SDK conversion")
            return convert(job, output, plan)

        with patch.object(self.service.media, "convert", interrupt_second_job):
            with self.assertRaises(Cancelled):
                self.service.scan_once()

        preview = Job((completed,)).output
        previous = (digest(preview), preview.stat().st_mtime_ns)
        self.assertFalse(Job((interrupted,)).output.exists())
        self.assertFalse(Job((pending,)).output.exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())
        report = json.loads(Job((completed,)).report.read_text())
        self.assertEqual(report["files"][completed.name]["status"], "success")
        self.assertEqual(report["files"][interrupted.name]["status"], "processing")
        self.assertEqual(report["files"][pending.name]["status"], "pending")

        self.service.close()
        self.runner = Runner(self.audit, threading.Event(), 30)
        self.service = Service(self.cfg, self.audit, self.runner)
        begin_scan = self.service.reports.begin_scan
        registered = []

        def inspect_resumed_registration(jobs, **kwargs):
            errors = begin_scan(jobs, **kwargs)
            report = json.loads(Job((completed,)).report.read_text())
            self.assertEqual(report["files"][completed.name]["status"], "success")
            self.assertEqual(report["files"][interrupted.name]["status"], "pending")
            self.assertEqual(report["files"][pending.name]["status"], "pending")
            self.assertEqual(report["summary"], {
                "failed": 0, "total": 3, "processing": 0, "pending": 2,
                "deferred": 0, "success": 1,
            })
            registered.append(report)
            return errors

        with patch.object(self.service.reports, "begin_scan", inspect_resumed_registration):
            with patch.object(self.service.media, "convert", wraps=self.service.media.convert) as resumed_convert:
                counts = self.service.scan_once()

        self.assertEqual(len(registered), 1)
        self.assertEqual((counts["success"], counts["skipped"], counts["failed"]), (2, 1, 0))
        self.assertEqual([call.args[0].primary for call in resumed_convert.call_args_list],
                         [interrupted, pending])
        self.assertEqual((digest(preview), preview.stat().st_mtime_ns), previous)
        self.assertEqual({source: digest(source) for source in originals}, originals)
        self.assertFalse((self.event / ".insta360-openconverter").exists())
        report = json.loads(Job((completed,)).report.read_text())
        self.assertEqual(report["summary"], {
            "failed": 0, "total": 3, "processing": 0, "pending": 0,
            "deferred": 0, "success": 3,
        })

    def test_unowned_output_and_text_files_are_preserved(self):
        source = self.source("original.insp")
        output = Job((source,)).output
        output.write_bytes(b"user data")
        report = Job((source,)).report
        report.write_text("user explanation", encoding="utf-8")
        sibling = self.event.with_name("20261103.独立事件")
        sibling.mkdir()
        independent = sibling / "independent.insp"
        shutil.copyfile(self.image, independent)
        counts = self.service.scan_once()
        self.assertEqual((counts["failed"], counts["success"]), (1, 1))
        self.assertEqual(output.read_bytes(), b"user data")
        self.assertEqual(report.read_text(), "user explanation")
        self.assertTrue(Job((independent,)).output.is_file())
        independent_report = json.loads(Job((independent,)).report.read_text())
        self.assertEqual(independent_report["files"][independent.name]["status"], "success")

    def test_source_change_replaces_owned_preview(self):
        source = self.source("change.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        subprocess.run([EXIFTOOL, "-overwrite_original", "-XMP-tiff:Model=X5", str(source)],
                       check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(self.service.scan_once()["success"], 1)
        self.assertEqual(self.service.media.metadata(Job((source,)).output)["XMP-tiff:Model"], "X5")

    def test_external_edit_is_not_overwritten(self):
        source = self.source("external.insp")
        self.assertEqual(self.service.scan_once()["success"], 1)
        output = Job((source,)).output
        output.write_bytes(b"changed by user")
        self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertEqual(output.read_bytes(), b"changed by user")

    def test_source_mutation_during_sdk_does_not_publish(self):
        source = self.source("mutate.insv")
        convert = self.service.media.convert

        def mutate(job, output, plan=None):
            convert(job, output, plan)
            with source.open("ab") as file:
                file.write(b"upload still in progress")

        with patch.object(self.service.media, "convert", mutate):
            self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertFalse(Job((source,)).output.exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_source_mutation_during_metadata_extraction_does_not_publish_json(self):
        source = self.source("metadata-mutates.insv")
        extract = self.service.media.metadata_sdk.extract

        def mutate(path, directory):
            result = extract(path, directory)
            with path.open("ab") as file:
                file.write(b"upload still in progress")
            return result

        with patch.object(self.service.media.metadata_sdk, "extract", mutate):
            self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertFalse(metadata_path(source).exists())
        self.assertFalse(Job((source,)).output.exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_recent_files_wait_and_missing_preview_is_rebuilt(self):
        source = self.source("recent.insp")
        self.cfg.settle_seconds = 120
        self.assertEqual(self.service.scan_once()["deferred"], 1)
        self.assertFalse(Job((source,)).output.exists())
        self.cfg.settle_seconds = 0
        self.assertEqual(self.service.scan_once()["success"], 1)
        Job((source,)).output.unlink()
        self.assertEqual(self.service.scan_once()["success"], 1)

    def test_only_one_service_can_use_a_state_directory(self):
        with self.assertRaisesRegex(RuntimeError, "Another converter"):
            Service(self.cfg, self.audit, self.runner)

    def test_default_conversion_uses_sdk_gpu_processing(self):
        source = self.source("gpu-default.lrv")
        self.assertEqual(self.service.scan_once()["success"], 1)
        self.assertTrue(Job((source,)).output.is_file())
        commands = [fields["argv"] for event, fields in self.audit.events
                    if event == "command.start" and fields["argv"][0] == str(self.sdk)]
        self.assertEqual(len(commands), 1)
        arguments = {argument.split("=", 1)[0].lstrip("-") for argument in commands[0]}
        self.assertTrue(arguments.isdisjoint({
            "disable_cuda", "image_processing_accel", "enable_soft_encode", "enable_soft_decode",
        }))

    def test_cli_watch_writes_audit_and_stops_cleanly(self):
        source = self.source("cli.insp")
        data = self.root / "cli-data"
        data.mkdir()
        config = data / "config.toml"
        config.write_text("\n".join([
            f"photo_root = {json.dumps(str(self.photo))}", "width = 128", "height = 64",
            "settle_seconds = 0", "scan_interval = 1", "[sdk]",
            f"executable = {json.dumps(str(self.sdk))}",
            f"models = {json.dumps(self.cfg.models)}", "library_dirs = []", "[tools]",
            f"exiftool = {json.dumps(EXIFTOOL)}", "[metadata]",
            f"reader = {json.dumps(str(self.metadata_sdk))}", "library_dirs = []", "",
        ]), encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "converter", "--data-dir", str(data), "watch"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not (data / "health.json").exists() and process.poll() is None:
                if time.monotonic() > deadline:
                    self.fail("CLI did not finish a scan")
                time.sleep(0.1)
            self.assertIsNone(process.poll(), process.stderr.read().decode() if process.poll() else "")
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=10), 0)
            logs = list((data / "log").glob("*.log"))
            self.assertEqual(len(logs), 1)
            events = [json.loads(line) for line in logs[0].read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "service.stop")
            self.assertTrue(any(e["event"] == "file.publish_new" for e in events))
            self.assertTrue(Job((source,)).output.exists())
            self.assertEqual(json.loads((data / "health.json").read_text())["status"], "ok")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()

    def test_official_fields_override_standard_metadata_and_keep_sensor_tracks(self):
        source = self.source("sdk-fields.insv")
        payload = {"camera_type": "Insta360 X5", "firmware_version": "v1.2.3", "serial_number": "TEST-SERIAL",
                   "gyro_first_timestamp_ms": 1_700_000_000_000,
                   "gps": [{"timestampMs": 1_700_000_000, "latitude": 31.1234567890123,
                            "north_south": "S", "longitude": 121.2345678901234,
                            "east_west": "W", "altitude": 12.75}],
                   "gyro": [{"timestamp": 123456789, "acceleration": [0.1, 0.2, 0.3], "rotation": [1, 2, 3]}],
                   "exposure": [{"timestamp": 123456789, "shutter_speed_s": 0.004}],
                   "timelapse": [{"timestamp": 123456789}]}
        self.metadata_payload.write_text(json.dumps(payload, indent=4))
        self.assertEqual(self.service.scan_once()["success"], 1)
        actual = self.service.media.metadata(Job((source,)).output)
        self.assertEqual(actual["XMP-tiff:Model"], "Insta360 X5")
        self.assertEqual(actual["XMP-aux:SerialNumber"], "TEST-SERIAL")
        self.assertEqual(actual["XMP-aux:Firmware"], "v1.2.3")
        self.assertAlmostEqual(float(actual["XMP-exif:GPSLatitude"]), -31.1234567890123, places=8)
        self.assertAlmostEqual(float(actual["XMP-exif:GPSLongitude"]), -121.2345678901234, places=8)
        self.assertEqual(actual["XMP-exif:DateTimeOriginal"], "2024:06:01 12:34:56+08:00")
        snapshot_path = metadata_path(source)
        sdk = json.loads(snapshot_path.read_text())["Insta360SDK"]
        self.assertEqual(sdk["gyro_first_timestamp_ms"], payload["gyro_first_timestamp_ms"])
        for name in ("gps", "gyro", "exposure", "timelapse"):
            track = sdk["tracks"][name]
            self.assertEqual(track["count"], 1)
            self.assertEqual(track["records"], payload[name])
            self.assertEqual(track["json_pointer"], f"/Insta360SDK/tracks/{name}/records")
            self.assertNotIn("file", track)
            original_jsonl = "".join(json.dumps(value) + "\n" for value in payload[name]).encode()
            self.assertEqual(track["sha256"], hashlib.sha256(original_jsonl).hexdigest())
            self.assertEqual(track["bytes"], len(original_jsonl))
        self.assertNotIn("snapshot_directory", sdk)
        self.assertFalse((self.data / "metadata").exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_metadata_sdk_failure_is_not_replaced_with_an_exiftool_success(self):
        source = self.source("metadata-fail.insv")
        self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertFalse(Job((source,)).output.exists())
        failure = json.loads(Job((source,)).report.read_text())["files"][source.name]
        self.assertRegex(failure["error"], "[\u4e00-\u9fff]")
        self.assertIn("InsMetaDataSDK Parse failed", failure["error_raw"])
        self.assertFalse(metadata_path(source).exists())
        self.assertFalse((self.data / "metadata").exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())

    def test_gyro_origin_does_not_create_a_capture_date(self):
        source = self.event / "gyro-origin.insp"
        shutil.copyfile(self.image, source)
        self.metadata_payload.write_text(json.dumps({"gyro_first_timestamp_ms": 1_700_000_000_000}, indent=4))
        self.assertEqual(self.service.scan_once()["success"], 1)
        self.assertNotIn("XMP-exif:DateTimeOriginal", self.service.media.metadata(Job((source,)).output))

    def test_standalone_metadata_command_does_not_require_media_sdk_or_gpu(self):
        source = self.source("inspect.insv")
        before = digest(source)
        data = self.root / "inspect-data"
        data.mkdir()
        (data / "config.toml").write_text("\n".join([
            f"photo_root = {json.dumps(str(self.photo))}", "[sdk]",
            'executable = "/missing/MediaSDKTest"', 'models = "/missing/models"',
            "[tools]", f"exiftool = {json.dumps(EXIFTOOL)}", "[metadata]",
            f"reader = {json.dumps(str(self.metadata_sdk))}", "library_dirs = []", "",
        ]), encoding="utf-8")
        for _ in range(2):
            result = subprocess.run([sys.executable, "-m", "converter", "--data-dir", str(data),
                                     "metadata", str(source)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata_file = metadata_path(source)
        self.assertEqual(metadata_file.name, "inspect_metadata.json")
        metadata = json.loads(metadata_file.read_text())
        self.assertEqual(metadata["Insta360SDK"]["sdk"], "InsMetaDataSDK")
        self.assertEqual(metadata["OpenConverter"]["source_sha256"], before)
        self.assertEqual(list(self.event.glob("*_metadata.json")), [metadata_file])
        self.assertFalse((data / "metadata").exists())
        self.assertFalse((self.event / ".insta360-openconverter").exists())
        self.assertEqual(digest(source), before)
        self.assertFalse(Job((source,)).output.exists())

    def test_source_quality_defaults_use_dynamic_size_and_record_actual_parameters(self):
        self.cfg.width = self.cfg.height = 0
        photo = self.source("original-size.insp")
        video = self.source("original-rate.insv")
        self.assertEqual(self.service.scan_once()["success"], 2)
        self.assertEqual((self.cfg.width, self.cfg.height), (0, 0))
        image = self.service.media.metadata(Job((photo,)).output)
        self.assertEqual(image["XMP-GPano:FullPanoWidthPixels"], 128)
        self.assertEqual(image["IFD0:ImageWidth"], 128)
        self.assertEqual(image["ExifIFD:ExifImageWidth"], 128)
        self.assertEqual(image["XMP-GPano:FullPanoHeightPixels"], 64)
        report = json.loads(Job((video,)).report.read_text())
        quality = report["files"][video.name]["quality"]
        self.assertEqual(quality["target"]["width"], 128)
        self.assertEqual(quality["target"]["height"], 64)
        self.assertEqual(quality["target"]["codec"], "h264")
        self.assertEqual(quality["target"]["fps"], "10")
        self.assertFalse(quality["flowstate"])
        actual = quality["actual"]["streams"][0]
        self.assertEqual(actual["width"], 128)
        self.assertEqual(actual["fps"], "10")
        commands = [fields["argv"] for event, fields in self.audit.events
                    if event == "command.start" and fields["argv"][0] == str(self.sdk)]
        self.assertTrue(all("-enable_flowstate" not in args for args in commands))
        source_rate = self.service.media.probe(video)["streams"][0]["bit_rate"]
        video_command = next(args for args in commands if str(video) in args)
        self.assertEqual(video_command[video_command.index("-bitrate") + 1], source_rate)


if __name__ == "__main__":
    unittest.main()
