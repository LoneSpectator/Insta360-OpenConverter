import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from converter.config import Config
from converter.media import capture_fields
from converter.metadata import MetadataSDK, coordinate


class Audit:
    def emit(self, *args, **kwargs):
        pass


class PreparedRunner:
    """A protocol fixture, not the proprietary SDK."""

    def run(self, *args, **kwargs):
        return json.dumps({"reader": "insta360-metadata", "schema_version": 1, "sdk": "InsMetaDataSDK"})


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cfg = Config(photo_root=self.root / "photo", data_root=self.root / "data")
        self.reader = MetadataSDK(self.cfg, PreparedRunner(), Audit())
        self.schema = {"reader": "insta360-metadata", "schema_version": 1, "sdk": "InsMetaDataSDK",
                       "camera_type": "Insta360 X4", "firmware_version": "1.0", "serial_number": "TEST",
                       "gyro_first_timestamp_ms": 123456789012345,
                       "tracks": {}}
        for name in ("gps", "gyro", "exposure", "timelapse"):
            self.schema["tracks"][name] = {"available": False, "count": 0, "file": name + ".jsonl"}
            (self.root / (name + ".jsonl")).write_text("")
        self.save()

    def save(self):
        (self.root / "sdk.json").write_text(json.dumps(self.schema, indent=4), encoding="utf-8")

    def test_missing_tracks_are_recorded_separately_from_parse_failure(self):
        actual = self.reader.extract(self.root / "sample.insv", self.root)
        self.assertIsNone(actual["first_valid_gps"])
        self.assertFalse(actual["tracks"]["gps"]["available"])
        self.assertEqual(actual["tracks"]["gps"]["count"], 0)
        self.assertEqual(actual["tracks"]["gps"]["bytes"], 0)

    def test_rejects_path_escape_in_track_descriptor(self):
        self.schema["tracks"]["gps"]["file"] = "../outside.jsonl"
        self.save()
        with self.assertRaisesRegex(ValueError, "track descriptor"):
            self.reader.extract(self.root / "sample.insv", self.root)

    def test_rejects_track_symlink(self):
        path = self.root / "gps.jsonl"
        path.unlink()
        path.symlink_to(self.root / "gyro.jsonl")
        with self.assertRaisesRegex(ValueError, "Missing SDK track"):
            self.reader.extract(self.root / "sample.insv", self.root)

    def test_rejects_sdk_false_success_and_truncated_track(self):
        self.schema["tracks"]["gyro"].update(available=True, count=2)
        (self.root / "gyro.jsonl").write_text('{"timestamp":1}\n')
        self.save()
        with self.assertRaisesRegex(ValueError, "record count mismatch"):
            self.reader.extract(self.root / "sample.insv", self.root)
        (self.root / "sdk.json").unlink()
        with self.assertRaisesRegex(ValueError, "summary file"):
            self.reader.extract(self.root / "sample.insv", self.root)

    def test_selects_first_valid_gps_and_does_not_normalize_timestamp(self):
        invalid = {"latitude": 100, "north_south": "N", "longitude": 20, "east_west": "E"}
        valid = {"latitude": 31.1234567890123, "north_south": "S", "longitude": 121.2345678901234,
                 "east_west": "W", "timestampMs": 987654321098765, "altitude": -5.0}
        (self.root / "gps.jsonl").write_text(json.dumps(invalid) + "\n" + json.dumps(valid) + "\n")
        self.schema["tracks"]["gps"].update(available=True, count=2)
        self.save()
        actual = self.reader.extract(self.root / "sample.insv", self.root)
        point = actual["first_valid_gps"]
        self.assertEqual(point["latitude"], -31.1234567890123)
        self.assertEqual(point["longitude"], -121.2345678901234)
        self.assertEqual(point["timestamp_raw"], valid["timestampMs"])
        self.assertEqual(point["altitude"], -5.0)

    def test_invalid_coordinates_are_not_written_as_zero(self):
        for value, hemisphere in [(True, "N"), (float("nan"), "N"), (float("inf"), "N"),
                                  (91, "N"), (20, "?"), (-20, "N")]:
            with self.subTest(value=value, hemisphere=hemisphere):
                self.assertIsNone(coordinate(value, hemisphere, "N", "S", 90))
        self.assertEqual(coordinate(0, "S", "N", "S", 90), 0)

    def test_sdk_gyro_origin_never_supplies_capture_date(self):
        fields = capture_fields({"Insta360SDK": self.schema})
        self.assertIsNone(fields["DateTimeOriginal"])
        self.assertEqual(fields["Model"], "Insta360 X4")
        self.assertEqual(fields["SerialNumber"], "TEST")

    def test_config_metadata_section_and_binary_refresh(self):
        config = self.root / "config.toml"
        binary = self.root / "reader"
        binary.write_bytes(b"first build")
        config.write_text(f'photo_root = {json.dumps(str(self.cfg.photo_root))}\n'
                          f'[metadata]\nreader = {json.dumps(str(binary))}\nlibrary_dirs = []\n')
        loaded = Config.load(config, self.cfg.data_root)
        self.assertEqual(loaded.metadata_reader, str(binary))
        before = loaded.fingerprint()
        binary.write_bytes(b"replacement build with SDK changes")
        self.assertNotEqual(loaded.fingerprint(), before)


@unittest.skipUnless(os.environ.get("INSTA360_METADATA_READER"), "Local official SDK is not supplied to public CI")
class OfficialSDKTests(unittest.TestCase):
    def test_real_adapter_loads_sdk_and_rejects_invalid_input(self):
        reader = os.environ["INSTA360_METADATA_READER"]
        result = json.loads(subprocess.check_output([reader, "--schema"], text=True))
        self.assertEqual(result["sdk"], "InsMetaDataSDK")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invalid.insv"
            source.write_bytes(b"not an Insta360 camera file")
            output = root / "output"
            output.mkdir()
            actual = subprocess.run([reader, source, output], capture_output=True, text=True, timeout=20)
            self.assertEqual(actual.returncode, 3, actual.stderr)
            self.assertIn("InsMetaDataSDK Parse failed", actual.stderr)
            self.assertFalse((output / "sdk.json").exists())


if __name__ == "__main__":
    unittest.main()
