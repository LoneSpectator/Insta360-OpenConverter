import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from converter.audit import Cancelled
from converter.metadata import TRACKS
from converter.scan import digest, snapshot
from converter.sidecar import MetadataSidecars
from converter.state import State
from converter.workspace import TEMP_ROOT_NAME


class Audit:
    def __init__(self):
        self.events = []
        self.callback = None

    def emit(self, event, **fields):
        self.events.append((event, fields))
        if self.callback:
            self.callback(event, fields)


class MetadataSidecarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "中文 camera file.insv"
        self.source.write_bytes(b"original camera data")
        self.before = snapshot(self.source)
        self.source_sha = digest(self.source)
        self.directory = self.root / "native-output"
        self.directory.mkdir()
        self.records = {name: [] for name in TRACKS}
        self.records["gps"] = [{"timestampMs": 987654321012345, "latitude": 31.1234567890123,
                                "longitude": -121.1234567890123, "north_south": "N"}]
        self.records["gyro"] = [{"timestamp": index, "x": index / 1000} for index in range(3)]
        self.metadata = {"ExifIFD:DateTimeOriginal": "2024:06:01 12:34:56",
                         "XMP-tiff:Model": "相机 X5",
                         "Insta360SDK": {"sdk": "InsMetaDataSDK", "tracks": {}}}
        for name in TRACKS:
            raw = "".join(json.dumps(item, ensure_ascii=False) + "\n"
                          for item in self.records[name]).encode()
            (self.directory / (name + ".jsonl")).write_bytes(raw)
            self.metadata["Insta360SDK"]["tracks"][name] = {
                "file": name + ".jsonl", "available": bool(self.records[name]),
                "count": len(self.records[name]), "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        self.state = State(self.root / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.audit, self.stop = Audit(), threading.Event()
        self.store = MetadataSidecars(self.state, self.audit, self.stop)
        self.output = self.source.with_name(self.source.stem + "_metadata.json")

    def save(self):
        return self.store.save(self.source, self.metadata, self.directory, self.before, self.source_sha)

    def assert_no_temporary(self):
        self.assertFalse(list(self.root.glob(".insta360-metadata-*")))
        self.assertFalse((self.root / TEMP_ROOT_NAME).exists())

    def test_all_metadata_and_tracks_merge_into_one_four_space_json(self):
        self.assertEqual(self.save(), self.output)
        text = self.output.read_text(encoding="utf-8")
        actual = json.loads(text)
        self.assertEqual(text, json.dumps(actual, ensure_ascii=False, indent=4) + "\n")
        self.assertEqual(actual["XMP-tiff:Model"], "相机 X5")
        self.assertEqual(actual["OpenConverter"]["source_sha256"], self.source_sha)
        for name in TRACKS:
            description = actual["Insta360SDK"]["tracks"][name]
            self.assertEqual(description["records"], self.records[name])
            self.assertNotIn("file", description)
            self.assertEqual(description["json_pointer"], f"/Insta360SDK/tracks/{name}/records")
            self.assertEqual(description["sha256"], self.metadata["Insta360SDK"]["tracks"][name]["sha256"])
            self.assertNotIn("records", self.metadata["Insta360SDK"]["tracks"][name])
        self.assertEqual(self.source.read_bytes(), b"original camera data")
        self.assertEqual(self.state.hashes(self.output), [digest(self.output)])
        staged = [fields["destination"] for event, fields in self.audit.events
                  if event == "file.write_metadata_snapshot"]
        self.assertTrue(staged)
        self.assertTrue(all(path.is_relative_to(self.root / TEMP_ROOT_NAME) for path in staged))
        self.assert_no_temporary()

    def test_existing_owned_sidecar_can_be_replaced_on_later_extraction(self):
        self.save()
        self.metadata["XMP-tiff:Model"] = "changed model"
        self.save()
        self.assertEqual(json.loads(self.output.read_text())["XMP-tiff:Model"], "changed model")
        self.assertTrue(any(event == "file.replace_owned_metadata" for event, _ in self.audit.events))
        self.assertEqual(self.state.hashes(self.output), [digest(self.output)])
        self.assert_no_temporary()

    def test_same_stem_different_original_cannot_replace_owned_metadata(self):
        self.save()
        original = self.output.read_bytes()
        second = self.source.with_suffix(".insp")
        second.write_bytes(b"a different original with the same stem")
        with self.assertRaisesRegex(RuntimeError, "another source"):
            self.store.save(second, self.metadata, self.directory, snapshot(second), digest(second))
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(json.loads(original)["OpenConverter"]["source"], str(self.source))
        self.assert_no_temporary()

    def test_large_unicode_sidecar_can_be_checked_with_a_bounded_header(self):
        self.metadata["XMP:Description"] = "传感器" * 30000
        self.save()
        self.assertGreater(self.output.stat().st_size, 65536)
        self.save()
        actual = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(next(iter(actual)), "OpenConverter")
        self.assertEqual(actual["XMP:Description"], self.metadata["XMP:Description"])
        self.assert_no_temporary()

    def test_unowned_sidecar_is_preserved(self):
        self.output.write_text("user metadata")
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            self.save()
        self.assertEqual(self.output.read_text(), "user metadata")
        self.assert_no_temporary()

    def test_externally_modified_owned_sidecar_is_preserved(self):
        self.save()
        self.output.write_text("user edited generated metadata")
        with self.assertRaisesRegex(RuntimeError, "externally changed"):
            self.save()
        self.assertEqual(self.output.read_text(), "user edited generated metadata")
        self.assert_no_temporary()

    def test_sidecar_symlink_is_rejected_without_touching_target(self):
        target = self.root / "user.json"
        target.write_text("user metadata")
        self.output.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.save()
        self.assertEqual(target.read_text(), "user metadata")
        self.assertTrue(self.output.is_symlink())
        self.assert_no_temporary()

    def test_sidecar_directory_is_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.save()
        self.assertTrue(self.output.is_dir())

    def test_existing_metadata_directory_form_is_preserved_without_creating_json(self):
        counterpart = self.source.with_name(self.source.stem + "_metadata")
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    counterpart.mkdir()
                elif kind == "file":
                    counterpart.write_text("user metadata")
                else:
                    counterpart.symlink_to(self.source)
                with self.assertRaisesRegex(RuntimeError, "both metadata forms"):
                    self.save()
                self.assertTrue(counterpart.exists())
                self.assertFalse(self.output.exists())
                self.assert_no_temporary()
                if kind == "directory":
                    counterpart.rmdir()
                else:
                    counterpart.unlink()

    def test_source_changed_during_extraction_does_not_publish(self):
        self.source.write_bytes(b"changed original")
        with self.assertRaisesRegex(RuntimeError, "Source changed"):
            self.save()
        self.assertFalse(self.output.exists())
        self.assert_no_temporary()

    def test_source_change_preserves_previous_sidecar(self):
        self.save()
        original = self.output.read_bytes()
        def mutate(event, fields):
            if event == "file.merge_sdk_track":
                self.source.write_bytes(b"changed while merging")
        self.audit.callback = mutate
        with self.assertRaisesRegex(RuntimeError, "Source changed"):
            self.save()
        self.assertEqual(self.output.read_bytes(), original)
        self.assert_no_temporary()

    def test_destination_created_during_merge_is_preserved(self):
        def create(event, fields):
            if event == "file.merge_sdk_track" and not self.output.exists():
                self.output.write_text("concurrent user file")
        self.audit.callback = create
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            self.save()
        self.assertEqual(self.output.read_text(), "concurrent user file")
        self.assert_no_temporary()

    def test_cancel_during_track_merge_removes_temporary_output(self):
        def cancel(event, fields):
            if event == "file.merge_sdk_track":
                self.stop.set()
        self.audit.callback = cancel
        with self.assertRaises(Cancelled):
            self.save()
        self.assertFalse(self.output.exists())
        self.assert_no_temporary()

    def test_invalid_or_changed_tracks_do_not_publish_partial_json(self):
        path = self.directory / "gps.jsonl"
        for payload in (b"broken JSON\n", b"[]\n", b'{"x":1}', b'{"x":1}\n', b""):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.save()
                self.assertFalse(self.output.exists())
                self.assert_no_temporary()


if __name__ == "__main__":
    unittest.main()
