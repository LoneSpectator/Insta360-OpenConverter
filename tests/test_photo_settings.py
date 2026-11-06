import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter.config import Config
from converter.scan import Job, snapshot


class PhotoSettingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photo = self.root / "photo"
        self.data = self.root / "data"
        self.photo.mkdir()
        self.data.mkdir()

    def config(self, **overrides):
        return Config(photo_root=self.photo, data_root=self.data, **overrides)

    def job(self, name):
        source = self.photo / name
        source.write_bytes(b"original media fixture")
        return Job((source,))

    @staticmethod
    def legacy_fingerprint(sources, config):
        return hashlib.sha256(json.dumps([sources, config], sort_keys=True).encode()).hexdigest()

    def test_photo_default_is_independent_of_video_default(self):
        cfg = self.config()
        self.assertTrue(cfg.photo_flowstate)
        self.assertFalse(cfg.flowstate)
        cfg.validate()

    def test_photo_setting_loads_as_a_top_level_boolean(self):
        config = self.data / "config.toml"
        for setting, expected in (("", True), ("photo_flowstate = true", True),
                                  ("photo_flowstate = false", False)):
            with self.subTest(setting=setting):
                config.write_text("photo_root = " + json.dumps(str(self.photo)) + "\n" + setting + "\n")
                cfg = Config.load(config, self.data)
                self.assertIs(cfg.photo_flowstate, expected)
                self.assertFalse(cfg.flowstate)

    def test_photo_setting_rejects_non_boolean_values(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "photo_flowstate must be a boolean"):
                    self.config(photo_flowstate=value).validate()

    def test_photo_setting_preserves_the_existing_global_render_fingerprint(self):
        cfg = self.config()
        original = cfg.fingerprint()
        cfg.photo_flowstate = False
        self.assertEqual(cfg.fingerprint(), original)
        cfg.flowstate = True
        self.assertNotEqual(cfg.fingerprint(), original)

    def test_video_job_fingerprint_is_exactly_the_legacy_format(self):
        for suffix in (".insv", ".lrv", ".INSV"):
            with self.subTest(suffix=suffix):
                job = self.job("video" + suffix)
                identity, sources = job.fingerprint("existing-video-config")
                self.assertEqual(identity, self.legacy_fingerprint(sources, "existing-video-config"))
                self.assertEqual(job.fingerprint("existing-video-config", photo_flowstate=False)[0], identity)
                self.assertEqual(job.fingerprint("existing-video-config", photo_flowstate=True)[0], identity)

    def test_photo_revision_refreshes_legacy_exports_and_tracks_photo_setting(self):
        for suffix in (".insp", ".INSP"):
            with self.subTest(suffix=suffix):
                job = self.job("photo" + suffix)
                enabled, sources = job.fingerprint("same-render-config")
                disabled = job.fingerprint("same-render-config", photo_flowstate=False)[0]
                self.assertNotEqual(enabled, self.legacy_fingerprint(sources, "same-render-config"))
                self.assertNotEqual(enabled, disabled)
                self.assertEqual(job.fingerprint("same-render-config", photo_flowstate=True)[0], enabled)

    def test_supplied_snapshots_are_reused_without_another_file_read(self):
        job = self.job("photo.insp")
        sources = [snapshot(job.primary)]
        expected = job.fingerprint("config", photo_flowstate=False)[0]
        saved = [dict(item) for item in sources]
        with patch("converter.scan.snapshot", side_effect=AssertionError("Unexpected snapshot read")):
            actual, returned = job.fingerprint("config", sources, photo_flowstate=False)
        self.assertEqual(actual, expected)
        self.assertIs(returned, sources)
        self.assertEqual(sources, saved)


if __name__ == "__main__":
    unittest.main()
