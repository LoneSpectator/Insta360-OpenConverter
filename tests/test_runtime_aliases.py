from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter.__main__ import main
from converter.config import Config
from converter.service import Service
from converter.state import State


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.photo = self.root / "photo"
        self.data.mkdir()
        self.photo.mkdir()
        base = self.data / "sdk"
        self.cfg = Config(photo_root=self.photo, data_root=self.data,
                          executable=str(base / "media/bin/MediaSDKTest"),
                          models=str(base / "media/bin/models"),
                          library_dirs=[str(base / "media/lib"), str(base / "media/bin")],
                          metadata_reader=str(base / "metadata/bin/insta360-metadata"),
                          metadata_library_dirs=[str(base / "metadata/lib")])
        self.manifests = {}
        for component, identity in (("media", "a" * 64), ("metadata", "b" * 64)):
            path = base / component / ".insta360-bootstrap.json"
            path.parent.mkdir(parents=True)
            self.manifests[component] = path
            self.write_manifest(component, identity)
        reader = Path(self.cfg.metadata_reader)
        reader.parent.mkdir()
        reader.write_bytes(b"same compiled metadata reader")

    def write_manifest(self, sdk_component, identity, **extra):
        self.manifests[sdk_component].write_text(json.dumps({
            "project": "Insta360-OpenConverter", "schema_version": 1,
            "component": sdk_component, "runtime_sha256": identity,
            "builder": {"sha256": "initial builder"}, **extra,
        }, indent=4) + "\n", encoding="utf-8")

    def change_builder_and_reader_timestamp(self, *args, **kwargs):
        self.write_manifest("metadata", "b" * 64, builder={"sha256": "updated preparer"})
        reader = Path(self.cfg.metadata_reader)
        old = reader.stat()
        os.utime(reader, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))

    def run_prepare(self, prepare):
        previous = self.cfg.fingerprint()
        with patch("converter.__main__.Config.load", return_value=self.cfg), \
                patch("converter.__main__.prepare_sdks", side_effect=prepare), \
                patch.object(Service, "doctor"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.data), "doctor"]), 0)
        current = self.cfg.fingerprint()
        state = State(self.data / "state.sqlite3")
        try:
            return previous, current, state.config_aliases(current)
        finally:
            state.close()

    def test_runtime_identity_ignores_builder_and_reader_timestamp(self):
        identity = self.cfg.managed_runtime_identity()
        previous = self.cfg.fingerprint()
        self.change_builder_and_reader_timestamp()
        self.assertEqual(self.cfg.managed_runtime_identity(), identity)
        self.assertNotEqual(self.cfg.fingerprint(), previous)
        self.write_manifest("media", "c" * 64)
        self.assertNotEqual(self.cfg.managed_runtime_identity(), identity)

    def test_doctor_records_compatible_rebuild_for_later_watch_process(self):
        previous, current, aliases = self.run_prepare(self.change_builder_and_reader_timestamp)
        self.assertNotEqual(previous, current)
        self.assertEqual(aliases, [previous])

    def test_changed_runtime_content_does_not_register_alias(self):
        previous, current, aliases = self.run_prepare(
            lambda *args, **kwargs: self.write_manifest("media", "c" * 64))
        self.assertNotEqual(previous, current)
        self.assertEqual(aliases, [])

    def test_custom_paths_do_not_register_alias(self):
        self.cfg.executable = str(self.root / "custom/MediaSDKTest")
        self.assertIsNone(self.cfg.managed_runtime_identity())
        _, _, aliases = self.run_prepare(self.change_builder_and_reader_timestamp)
        self.assertEqual(aliases, [])

    def test_first_start_does_not_register_alias(self):
        self.manifests["media"].unlink()
        self.assertIsNone(self.cfg.managed_runtime_identity())
        _, _, aliases = self.run_prepare(
            lambda *args, **kwargs: self.write_manifest("media", "a" * 64))
        self.assertEqual(aliases, [])

    def test_identity_rejects_invalid_unbounded_and_symlink_manifests(self):
        path = self.manifests["metadata"]
        for extra in ({"schema_version": True}, {"project": "other"},
                      {"component": "media"}, {"runtime_sha256": "invalid"}):
            with self.subTest(extra=extra):
                self.write_manifest("metadata", "b" * 64, **extra)
                self.assertIsNone(self.cfg.managed_runtime_identity())
        path.write_text(" " * 65537)
        self.assertIsNone(self.cfg.managed_runtime_identity())
        path.unlink()
        path.symlink_to(self.manifests["media"])
        self.assertIsNone(self.cfg.managed_runtime_identity())

    def test_default_fingerprint_algorithm_has_not_changed(self):
        with patch("converter.config.Path.is_file", return_value=False):
            self.assertEqual(Config().fingerprint(),
                             "674d0537177f7b21fce8111935be23885c6c83a1ecf3ead76c76b8d16ef2aae9")


class AliasStateTests(unittest.TestCase):
    def test_aliases_survive_reopen_and_follow_transitive_rebuilds_without_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = State(path)
            state.register_config_alias("old", "middle")
            state.close()
            state = State(path)
            try:
                state.register_config_alias("middle", "current")
                state.register_config_alias("old", "middle")
                state.register_config_alias("current", "current")
                self.assertEqual(state.config_aliases("current"), ["middle", "old"])
                state.register_config_alias("current", "old")
                self.assertEqual(state.config_aliases("current"), ["middle", "old"])
                self.assertEqual(state.config_aliases("unrelated"), [])
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
