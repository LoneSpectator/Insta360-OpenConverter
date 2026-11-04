"""Bootstrap lifecycle tests use explicit SDK/compiler doubles, not camera media."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

from converter.audit import Cancelled, Runner
from converter.bootstrap import (MANIFEST, MARKER, MARKER_TEXT, discover_source,
                                 file_sha256, prepare_sdks, tree_sha256)
from converter.config import Config


class MemoryAudit:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class CompilerDouble(Runner):
    """Only replace compilation; subprocess execution still exercises Runner."""
    def __init__(self, audit):
        super().__init__(audit, threading.Event(), 10)
        self.compiles = 0
        self.fail_compile = False
        self.entered = None
        self.release = None
        self.compile_calls = []

    def run(self, argv, *, cwd=None, env=None, capture=False, accepted_returncodes=(0,)):
        if str(argv[0]) == "g++":
            self.compiles += 1
            self.compile_calls.append((list(map(str, argv)), Path(cwd), dict(env)))
            if self.entered is not None:
                self.entered.set()
                if not self.release.wait(5):
                    raise TimeoutError("Test compilation gate timed out")
            if self.fail_compile:
                raise RuntimeError("Explicit compiler failure double")
            output = Path(argv[argv.index("-o") + 1])
            schema = {"reader": "insta360-metadata", "schema_version": 1, "sdk": "InsMetaDataSDK"}
            output.write_text(f"#!{sys.executable}\nprint({json.dumps(schema)!r})\n", encoding="utf-8")
            output.chmod(0o755)
            return ""
        return super().run(argv, cwd=cwd, env=env, capture=capture,
                           accepted_returncodes=accepted_returncodes)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "sdk-inputs"
        self.source.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = Config(data_root=self.data)
        self.cfg.source_root = self.source
        self.cfg.executable = str(self.data / "sdk/media/bin/MediaSDKTest")
        self.cfg.metadata_reader = str(self.data / "sdk/metadata/bin/insta360-metadata")
        self.audit = MemoryAudit()
        self.runner = CompilerDouble(self.audit)
        self.metadata = self.archive("InsMetaDataSDK-20260629_184813-2.0.2-linux64-default.tar_1782730793365.gz",
                                     self.metadata_files())
        self.media = self.archive("MediaSDK-3.2.0-20261026-linux64.tar_1793004103214.gz",
                                  self.media_files())

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def metadata_files(payload=b"explicit closed SDK fixture"):
        return {"SDK/include/metaData.h": b"header", "SDK/include/common.h": b"common",
                "SDK/lib/libInsMetaDataSDK.so": payload}

    @staticmethod
    def media_files():
        return {"SDK/bin/MediaSDKTest": b"#!/bin/sh\necho '-inputs -output -model_root_dir'\nexit 255\n",
                "SDK/bin/models/model.ins": b"explicit model fixture",
                "SDK/lib/library.so": b"explicit closed SDK fixture"}

    def archive(self, name, files):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as archive:
            for name, payload in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = 0o755 if name.endswith("MediaSDKTest") else 0o644
                archive.addfile(member, io.BytesIO(payload))
        return path

    def prepare(self, metadata_only=False):
        prepare_sdks(self.cfg, self.audit, self.runner, metadata_only=metadata_only)

    @property
    def runtime(self):
        return self.data / "sdk/metadata"

    def test_first_start_builds_both_sdks_and_confines_compiler_work(self):
        inputs = {path.name: file_sha256(path) for path in self.source.iterdir()}
        self.prepare()
        self.assertTrue(Path(self.cfg.executable).is_file())
        self.assertTrue(Path(self.cfg.metadata_reader).is_file())
        self.assertEqual(self.runner.compiles, 1)
        self.assertEqual(inputs, {path.name: file_sha256(path) for path in self.source.iterdir()})
        for argv, cwd, env in self.runner.compile_calls:
            self.assertTrue(cwd.is_relative_to(self.data / "sdk"))
            self.assertEqual([env[key] for key in ("TMPDIR", "TMP", "TEMP")], [str(cwd)] * 3)
            self.assertTrue(Path(argv[argv.index("-o") + 1]).is_relative_to(cwd))
        manifest_text = (self.runtime / MANIFEST).read_text()
        self.assertIn('\n    "project":', manifest_text)
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["source"]["version"], "2.0.2")
        self.assertEqual(manifest["source"]["kind"], "archive")
        self.assertEqual(manifest["runtime_sha256"], tree_sha256(self.runtime, runtime=True))
        self.assertTrue(manifest["builder"]["adapter_source_sha256"])

    def test_unchanged_restart_hashes_cache_without_compiling(self):
        self.prepare()
        count = len([event for event, _ in self.audit.events if event == "command.start"])
        self.prepare()
        self.assertEqual(self.runner.compiles, 1)
        self.assertEqual(len([event for event, _ in self.audit.events if event == "command.start"]), count)
        self.assertEqual(len([event for event, _ in self.audit.events if event == "sdk.cache.hit"]), 2)

    def test_damaged_reader_library_and_manifest_each_rebuild(self):
        self.prepare(metadata_only=True)
        for relative in ("bin/insta360-metadata", "lib/libInsMetaDataSDK.so", MANIFEST):
            with self.subTest(relative=relative):
                target = self.runtime / relative
                target.write_text("damaged", encoding="utf-8")
                previous = self.runner.compiles
                self.prepare(metadata_only=True)
                self.assertEqual(self.runner.compiles, previous + 1)
                self.assertNotEqual(target.read_text(), "damaged")

    def test_missing_adapter_rebuilds(self):
        self.prepare(metadata_only=True)
        Path(self.cfg.metadata_reader).unlink()
        self.prepare(metadata_only=True)
        self.assertEqual(self.runner.compiles, 2)

    def test_source_archive_change_and_new_adapter_source_rebuild(self):
        self.prepare(metadata_only=True)
        self.archive(self.metadata.name, self.metadata_files(b"updated fixture"))
        self.prepare(metadata_only=True)
        self.assertEqual(self.runner.compiles, 2)
        source = self.root / "updated-adapter.cc"
        source.write_text("explicit changed adapter test fixture", encoding="utf-8")
        with mock.patch("converter.bootstrap.ADAPTER_SOURCE", source):
            self.prepare(metadata_only=True)
        self.assertEqual(self.runner.compiles, 3)

    def test_compile_failure_preserves_old_runtime_and_removes_staging(self):
        self.prepare(metadata_only=True)
        previous = tree_sha256(self.runtime)
        self.archive(self.metadata.name, self.metadata_files(b"updated fixture"))
        self.runner.fail_compile = True
        with self.assertRaisesRegex(RuntimeError, "official SDK is precompiled"):
            self.prepare(metadata_only=True)
        self.assertEqual(tree_sha256(self.runtime), previous)
        self.assertEqual(sorted(path.name for path in (self.data / "sdk").iterdir()),
                         [".insta360-bootstrap.lock", "metadata"])

    def test_metadata_only_needs_no_media_sdk(self):
        self.media.unlink()
        self.prepare(metadata_only=True)
        self.assertEqual(self.runner.compiles, 1)
        self.assertFalse((self.data / "sdk/media").exists())

    def test_external_custom_paths_are_left_untouched(self):
        self.cfg.executable = "/custom/MediaSDKTest"
        self.cfg.metadata_reader = "/custom/insta360-metadata"
        shutil.rmtree(self.source)
        self.prepare()
        self.assertFalse((self.data / "sdk").exists())

    def test_unowned_runtime_is_not_overwritten(self):
        self.runtime.mkdir(parents=True)
        sentinel = self.runtime / "user-file"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ownership marker"):
            self.prepare(metadata_only=True)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_empty_legacy_mount_target_can_be_initialized(self):
        self.runtime.mkdir(parents=True)
        self.prepare(metadata_only=True)
        self.assertEqual((self.runtime / MARKER).read_text(), MARKER_TEXT)

    def test_source_and_cache_paths_cannot_overlap(self):
        self.cfg.source_root = self.data
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.prepare(metadata_only=True)

    def test_rejects_cache_symlink(self):
        (self.data / "sdk").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare(metadata_only=True)
        self.assertFalse((self.source / ".insta360-bootstrap.lock").exists())

    def test_rejects_destination_and_lock_symlinks(self):
        (self.data / "sdk").mkdir()
        self.runtime.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            self.prepare(metadata_only=True)
        self.runtime.unlink()
        (self.data / "sdk/.insta360-bootstrap.lock").unlink()
        (self.data / "sdk/.insta360-bootstrap.lock").symlink_to(self.metadata)
        with self.assertRaises(OSError):
            self.prepare(metadata_only=True)

    def test_rejects_source_symlink_escape_and_archive_traversal(self):
        self.metadata.unlink()
        sdk = self.source / "metadata"
        (sdk / "include").mkdir(parents=True)
        (sdk / "include/metaData.h").symlink_to(self.media)
        with self.assertRaisesRegex(ValueError, "symlink escapes"):
            self.prepare(metadata_only=True)
        shutil.rmtree(sdk)
        self.archive(self.metadata.name, {"../outside": b"must not write"})
        with self.assertRaisesRegex(RuntimeError, "escapes destination"):
            self.prepare(metadata_only=True)
        self.assertFalse((self.data / "sdk/outside").exists())

    def test_supports_normalized_unpacked_sources(self):
        self.metadata.unlink()
        sdk = self.source / "metadata"
        for name, content in self.metadata_files().items():
            target = sdk / Path(name).relative_to("SDK")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.prepare(metadata_only=True)
        self.assertEqual(json.loads((self.runtime / MANIFEST).read_text())["source"]["kind"], "directory")

    def test_selects_highest_parsed_version_not_latest_timestamp(self):
        self.archive("MediaSDK-3.1.15-20990101-linux64.tar_999999999.gz", self.media_files())
        selected, kind, version = discover_source(self.source, "media")
        self.assertEqual(selected, self.media)
        self.assertEqual(version, (3, 2, 0))
        self.assertEqual(kind, "archive")
        newer = self.archive("MediaSDK-3.10.0-20270101-linux64.tar_1.gz", self.media_files())
        self.assertEqual(discover_source(self.source, "media")[0], newer)

    def test_same_version_and_unknown_version_ambiguity_fail(self):
        duplicate = self.archive("MediaSDK-3.2.0-another-linux64.tar_2.gz", self.media_files())
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            discover_source(self.source, "media")
        duplicate.unlink()
        self.archive("MediaSDK-linux64.tar.gz", self.media_files())
        with self.assertRaisesRegex(ValueError, "without unique version"):
            discover_source(self.source, "media")

    def test_camera_sdk_archives_are_ignored_and_subdirectories_supported(self):
        self.archive("CameraSDK-2.1.8-linux-x86_64.tar.gz", {"unrelated": b"camera"})
        nested = self.source / "metadata" / self.metadata.name
        nested.parent.mkdir()
        self.metadata.rename(nested)
        self.prepare(metadata_only=True)
        self.assertEqual(json.loads((self.runtime / MANIFEST).read_text())["source"]["path"],
                         "metadata/" + self.metadata.name)

    def test_lock_serializes_concurrent_initialization(self):
        self.runner.entered = threading.Event()
        self.runner.release = threading.Event()
        second = CompilerDouble(MemoryAudit())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(self.prepare, True)
            self.assertTrue(self.runner.entered.wait(3))
            second_future = pool.submit(prepare_sdks, self.cfg, second.audit, second, True)
            self.runner.release.set()
            first_future.result(timeout=5)
            second_future.result(timeout=5)
        self.assertEqual(self.runner.compiles + second.compiles, 1)

    def test_lock_wait_honors_shutdown(self):
        base = self.data / "sdk"
        base.mkdir()
        with (base / ".insta360-bootstrap.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.runner.stop.set()
            with self.assertRaises(Cancelled):
                self.prepare(metadata_only=True)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "Requires Debian package extraction tool")
    def test_nested_debian_sdk_extracts_without_running_maintainer_script(self):
        package_root = self.root / "debian-sdk"
        (package_root / "DEBIAN").mkdir(parents=True)
        (package_root / "DEBIAN").chmod(0o755)
        (package_root / "DEBIAN/control").write_text(
            "Package: sdk-bootstrap-test\nVersion: 3.2.0\nArchitecture: amd64\n"
            "Maintainer: Test <test@example.invalid>\nDescription: Explicit SDK fixture\n",
            encoding="utf-8")
        sentinel = self.root / "maintainer-must-not-run"
        postinst = package_root / "DEBIAN/postinst"
        postinst.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
        postinst.chmod(0o755)
        for name, payload in self.media_files().items():
            path = package_root / "opt" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if path.name == "MediaSDKTest":
                path.chmod(0o755)
        (package_root / "opt/SDK/lib/library-link.so").symlink_to("library.so")
        # A highly compressed model spans several decoder output buffers.
        (package_root / "opt/SDK/bin/models/model.ins").write_bytes(b"\0" * (1024 * 1024 + 13))
        package = self.root / "MediaSDK-3.2.0.deb"
        for compression in (None, "gzip", "xz", "zstd", "none"):
            with self.subTest(compression=compression or "platform_default"):
                command = ["dpkg-deb", "--build", "--root-owner-group"]
                if compression:
                    command.append("-Z" + compression)
                subprocess.run(command + [str(package_root), str(package)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.archive(self.media.name, {"SDK/MediaSDK-3.2.0.deb": package.read_bytes()})
                self.prepare()
                self.assertTrue((self.data / "sdk/media/lib/library-link.so").is_symlink())
                self.assertEqual((self.data / "sdk/media/bin/models/model.ins").stat().st_size,
                                 1024 * 1024 + 13)
                self.assertFalse(sentinel.exists())
        commands = [fields["argv"] for event, fields in self.audit.events if event == "command.start"]
        self.assertTrue(any(command[:2] == ["dpkg-deb", "--extract"] for command in commands))
        self.assertFalse(any("--install" in command for command in commands))

    def test_debian_symlink_escape_rejected_before_extractor_runs(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as tar:
            link = tarfile.TarInfo("SDK/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            tar.addfile(link)
        package = bytearray(b"!<arch>\n")
        for name, content in [("debian-binary", b"2.0\n"), ("data.tar.gz", payload.getvalue())]:
            header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n".encode()
            self.assertEqual(len(header), 60)
            package.extend(header + content)
            if len(content) % 2:
                package.extend(b"\n")
        self.archive(self.media.name, {"SDK/MediaSDK-3.2.0.deb": bytes(package)})
        with self.assertRaisesRegex(RuntimeError, "link escapes destination"):
            self.prepare()
        self.assertFalse(any(event == "command.start" for event, _ in self.audit.events))
        self.assertFalse((self.data / "sdk/outside").exists())


if __name__ == "__main__":
    unittest.main()
