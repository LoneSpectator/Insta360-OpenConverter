from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter.config import Config


class ConcurrencyConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.path = self.data / "config.toml"

    def load(self, setting):
        self.path.write_text(setting + "\n", encoding="utf-8")
        return Config.load(self.path, self.data)

    def test_default_and_example_use_three_jobs(self):
        cfg = Config()
        self.assertEqual(cfg.max_concurrent_jobs, 3)
        cfg.validate()
        sample = Path.cwd() / "config.example.toml"
        self.assertEqual(Config.load(sample, Path("/data")).max_concurrent_jobs, 3)

    def test_existing_config_defaults_to_three_and_explicit_values_load(self):
        self.assertEqual(self.load("").max_concurrent_jobs, 3)
        for value in (1, 3, 8):
            with self.subTest(value=value):
                self.assertEqual(self.load(f"max_concurrent_jobs = {value}").max_concurrent_jobs, value)

    def test_non_positive_or_non_integer_values_are_rejected(self):
        for value in (True, False, 0, -1, 1.0, "3", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_concurrent_jobs must be a positive integer"):
                    Config(max_concurrent_jobs=value).validate()

    def test_invalid_toml_values_do_not_start_a_worker_pool(self):
        for value in ("true", "false", "0", "-1", "1.5", '"3"'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_concurrent_jobs must be a positive integer"):
                    self.load(f"max_concurrent_jobs = {value}")

    def test_concurrency_preserves_the_existing_render_fingerprint(self):
        # Historical fingerprint for default configuration without installed
        # SDK files. Adding a scheduling option must keep completed jobs valid.
        previous = "674d0537177f7b21fce8111935be23885c6c83a1ecf3ead76c76b8d16ef2aae9"
        with patch("converter.config.Path.is_file", return_value=False):
            cfg = Config()
            for value in (1, 3, 8):
                with self.subTest(value=value):
                    cfg.max_concurrent_jobs = value
                    self.assertEqual(cfg.fingerprint(), previous)
            cfg.width, cfg.height = 3840, 1920
            self.assertNotEqual(cfg.fingerprint(), previous)


if __name__ == "__main__":
    unittest.main()
