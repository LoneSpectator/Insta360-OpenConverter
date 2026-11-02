import io
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

from converter.audit import Cancelled, Runner
from converter.config import Config
from converter.scan import Job, digest, scan
from converter.service import Service
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

    def test_output_collisions_block_all_owners(self):
        self.file("same.insv")
        self.file("same.lrv")
        self.assertTrue(all(j.blocked for j in scan(self.photo, self.audit)))

    def test_cpu_requires_explicit_compatible_sdk(self):
        self.cfg.mode = "cpu"
        with self.assertRaisesRegex(ValueError, "requires GPU"):
            self.cfg.validate()
        self.cfg.cpu_compatible = True
        self.cfg.validate()

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
if "metadata-fail" in Path(sys.argv[1]).name:
    print("InsMetaDataSDK Parse failed (simulated)", file=sys.stderr)
    sys.exit(3)
payload = Path({str(self.metadata_payload)!r})
data = json.loads(payload.read_text()) if payload.exists() else {{}}
directory = Path(sys.argv[2])
tracks = {{}}
for name in ("gps", "gyro", "exposure", "timelapse"):
    values = data.pop(name, [])
    (directory / (name + ".jsonl")).write_text("".join(json.dumps(value) + "\\n" for value in values))
    tracks[name] = {{"available": bool(values), "count": len(values), "file": name + ".jsonl"}}
summary = {{**schema, "camera_type": "", "firmware_version": "", "serial_number": "",
           "gyro_first_timestamp_ms": 0, **data, "tracks": tracks}}
(directory / "sdk.json").write_text(json.dumps(summary))
''', encoding="utf-8")
        self.metadata_sdk.chmod(0o755)
        self.cfg.metadata_reader = str(self.metadata_sdk)
        self.cfg.metadata_library_dirs = []
        self.runner = Runner(self.audit, threading.Event(), 30)
        self.service = Service(self.cfg, self.audit, self.runner)
        self.addCleanup(self.service.close)

    def source(self, name):
        path = self.event / name
        shutil.copyfile(self.image if path.suffix.lower() == ".insp" else self.video, path)
        subprocess.run([EXIFTOOL, "-overwrite_original", "-XMP-exif:DateTimeOriginal=2024:06:01 12:34:56+08:00",
                        "-XMP-tiff:Make=Insta360", "-XMP-tiff:Model=X4",
                        "-XMP-exif:GPSLatitude=31.25", "-XMP-exif:GPSLongitude=-121.5", str(path)],
                       check=True, stdout=subprocess.DEVNULL)
        return path

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
        self.assertFalse(list(self.event.glob(".insta360-preview-*")))
        self.assertTrue(list((self.data / "metadata").glob("*.json")))
        self.assertIn('"status": "success"', Job((source,)).report.read_text())

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
        for source in (primary, secondary):
            self.assertIn('"status": "success"', Job((source,)).report.read_text())
        self.assertEqual(len(list(self.event.glob("*_preview.mp4"))), 1)
        primary.unlink()
        self.assertEqual(self.service.scan_once()["deferred"], 1)
        self.assertIn('"status": "deferred"', Job((secondary,)).report.read_text())

    def test_sdk_false_success_does_not_stop_other_files(self):
        bad = self.source("bad.insv")
        good = self.source("good.insv")
        counts = self.service.scan_once()
        self.assertEqual((counts["failed"], counts["success"]), (1, 1))
        self.assertFalse(Job((bad,)).output.exists())
        self.assertTrue(Job((good,)).output.exists())
        self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertTrue(any(event == "job.retry_delayed" for event, _ in self.audit.events))

    def test_unowned_output_and_text_files_are_preserved(self):
        source = self.source("original.insp")
        output = Job((source,)).output
        output.write_bytes(b"user data")
        report = Job((source,)).report
        report.write_text("user explanation", encoding="utf-8")
        self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertEqual(output.read_bytes(), b"user data")
        self.assertEqual(report.read_text(), "user explanation")

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

        def mutate(job, output):
            convert(job, output)
            with source.open("ab") as file:
                file.write(b"upload still in progress")

        with patch.object(self.service.media, "convert", mutate):
            self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertFalse(Job((source,)).output.exists())
        self.assertFalse(list(self.event.glob(".insta360-preview-*")))

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

    def test_cpu_command_is_explicit(self):
        self.cfg.mode = "cpu"
        self.cfg.cpu_compatible = True
        source = self.source("cpu.lrv")
        self.assertEqual(self.service.scan_once()["success"], 1)
        commands = [fields["argv"] for event, fields in self.audit.events
                    if event == "command.start" and fields["argv"][0] == str(self.sdk)]
        self.assertIn("-enable_soft_encode", commands[0])
        self.assertIn("-disable_cuda", commands[0])
        self.assertIn("cpu", commands[0])

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
        self.metadata_payload.write_text(json.dumps(payload))
        self.assertEqual(self.service.scan_once()["success"], 1)
        actual = self.service.media.metadata(Job((source,)).output)
        self.assertEqual(actual["XMP-tiff:Model"], "Insta360 X5")
        self.assertEqual(actual["XMP-aux:SerialNumber"], "TEST-SERIAL")
        self.assertEqual(actual["XMP-aux:Firmware"], "v1.2.3")
        self.assertAlmostEqual(float(actual["XMP-exif:GPSLatitude"]), -31.1234567890123, places=8)
        self.assertAlmostEqual(float(actual["XMP-exif:GPSLongitude"]), -121.2345678901234, places=8)
        self.assertEqual(actual["XMP-exif:DateTimeOriginal"], "2024:06:01 12:34:56+08:00")
        snapshot_path, = (self.data / "metadata").glob("*.json")
        sdk = json.loads(snapshot_path.read_text())["Insta360SDK"]
        self.assertEqual(sdk["gyro_first_timestamp_ms"], payload["gyro_first_timestamp_ms"])
        directory = Path(sdk["snapshot_directory"])
        for name in ("gps", "gyro", "exposure", "timelapse"):
            self.assertEqual(sdk["tracks"][name]["count"], 1)
            values = [json.loads(line) for line in (directory / (name + ".jsonl")).read_text().splitlines()]
            self.assertEqual(values, payload[name])
            self.assertEqual(digest(directory / (name + ".jsonl")), sdk["tracks"][name]["sha256"])

    def test_metadata_sdk_failure_is_not_replaced_with_an_exiftool_success(self):
        source = self.source("metadata-fail.insv")
        self.assertEqual(self.service.scan_once()["failed"], 1)
        self.assertFalse(Job((source,)).output.exists())
        self.assertIn("InsMetaDataSDK Parse failed", Job((source,)).report.read_text())
        self.assertFalse(list((self.data / "metadata").glob(".insta360-metadata-*")))

    def test_gyro_origin_does_not_create_a_capture_date(self):
        source = self.event / "gyro-origin.insp"
        shutil.copyfile(self.image, source)
        self.metadata_payload.write_text(json.dumps({"gyro_first_timestamp_ms": 1_700_000_000_000}))
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
        result = subprocess.run([sys.executable, "-m", "converter", "--data-dir", str(data),
                                 "metadata", str(source)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata_file, = (data / "metadata").glob("*.json")
        metadata = json.loads(metadata_file.read_text())
        self.assertEqual(metadata["Insta360SDK"]["sdk"], "InsMetaDataSDK")
        self.assertTrue(Path(metadata["Insta360SDK"]["snapshot_directory"]).is_dir())
        self.assertEqual(digest(source), before)
        self.assertFalse(Job((source,)).output.exists())


if __name__ == "__main__":
    unittest.main()
