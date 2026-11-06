"""SDK command policy with an explicit runner double, without SDK execution."""

import json
from pathlib import Path
import tempfile
import unittest

from converter.config import Config
from converter.media import Media
from converter.quality import build_plan
from converter.scan import Job


class MemoryAudit:
    def emit(self, *args, **kwargs):
        pass


class CommandRunner:
    """Return supplied probes/help and create only a synthetic SDK output file."""

    def __init__(self, cfg, probes, help_text="-enable_h265_encoder -enable_10bit"):
        self.cfg = cfg
        self.probes = probes
        self.help_text = help_text
        self.calls = []

    def run(self, argv, **kwargs):
        argv = [str(value) for value in argv]
        self.calls.append((argv, kwargs))
        if argv[0] == self.cfg.ffprobe:
            return json.dumps(self.probes[argv[-1]])
        if argv == [self.cfg.executable, "-help"]:
            return self.help_text
        if argv[0] != self.cfg.executable or "-output" not in argv:
            raise AssertionError(f"Unexpected test command: {argv}")
        output = Path(argv[argv.index("-output") + 1])
        output.write_bytes(b"synthetic SDK output, not encoded media")
        return ""

    @property
    def conversion_commands(self):
        return [argv for argv, _ in self.calls if "-output" in argv]

    @property
    def help_calls(self):
        return [(argv, options) for argv, options in self.calls if "-help" in argv]


def probe(width=5760, height=2880, depth=8, bitrate=90000000):
    return {
        "streams": [{
            "codec_type": "video",
            "codec_name": "hevc" if depth > 8 else "h264",
            "width": width,
            "height": height,
            "avg_frame_rate": "30000/1001",
            "pix_fmt": "yuv420p10le" if depth > 8 else "yuv420p",
            "bits_per_raw_sample": str(depth),
            "bit_rate": str(bitrate),
            "disposition": {"attached_pic": 0},
        }],
        "format": {"duration": "10.0", "bit_rate": str(bitrate)},
    }


class MediaCommandQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photo = self.root / "photo"
        self.photo.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = Config(photo_root=self.photo, data_root=self.data)
        self.source = self.photo / "video.insv"
        self.source.write_bytes(b"original input")
        self.job = Job((self.source,))
        self.output = self.job.output

    def media(self, source_probe, help_text="-enable_h265_encoder -enable_10bit"):
        runner = CommandRunner(self.cfg, {str(self.source): source_probe}, help_text)
        return Media(self.cfg, runner, MemoryAudit()), runner

    def test_ten_bit_source_uses_hevc_ten_bit_and_source_size_and_bitrate(self):
        media, runner = self.media(probe(7680, 3840, depth=10, bitrate=140000000))
        media.convert(self.job, self.output)

        self.assertEqual(len(runner.conversion_commands), 1)
        command = runner.conversion_commands[0]
        self.assertEqual(command[command.index("-output_size") + 1], "7680x3840")
        self.assertEqual(command[command.index("-bitrate") + 1], "140000000")
        self.assertIn("-enable_h265_encoder", command)
        self.assertIn("-enable_10bit", command)
        self.assertNotIn("-enable_flowstate", command)
        self.assertEqual(len(runner.help_calls), 1)
        self.assertTrue(runner.help_calls[0][1]["capture"])
        self.assertEqual(runner.help_calls[0][1]["accepted_returncodes"], (0, 1, 255))
        self.assertTrue(self.output.is_file())
        self.assertGreater(self.output.stat().st_size, 0)
        self.assertEqual(self.source.read_bytes(), b"original input")

    def test_unsupported_ten_bit_help_rejects_before_conversion(self):
        media, runner = self.media(
            probe(depth=10), help_text="-output_size -enable_h265_encoder")

        with self.assertRaisesRegex(RuntimeError, "10-bit"):
            media.convert(self.job, self.output)

        self.assertEqual(len(runner.help_calls), 1)
        self.assertEqual(runner.conversion_commands, [])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), b"original input")

    def test_eight_bit_defaults_do_not_enable_ten_bit_or_flowstate(self):
        media, runner = self.media(probe())
        media.convert(self.job, self.output)

        command, = runner.conversion_commands
        self.assertEqual(command[command.index("-output_size") + 1], "5760x2880")
        self.assertEqual(command[command.index("-bitrate") + 1], "90000000")
        self.assertNotIn("-enable_10bit", command)
        self.assertNotIn("-enable_h265_encoder", command)
        self.assertNotIn("-enable_flowstate", command)
        self.assertEqual(runner.help_calls, [])
        self.assertTrue(self.output.is_file())

    def test_photo_flowstate_is_independent_of_video_flowstate(self):
        self.source = self.photo / "photo.insp"
        self.source.write_bytes(b"original photo")
        self.job = Job((self.source,))
        self.output = self.job.output
        for photo_enabled, video_enabled in ((True, False), (False, True)):
            with self.subTest(photo_enabled=photo_enabled, video_enabled=video_enabled):
                self.cfg.photo_flowstate = photo_enabled
                self.cfg.flowstate = video_enabled
                self.output.unlink(missing_ok=True)
                media, runner = self.media(probe())
                media.convert(self.job, self.output)
                command, = runner.conversion_commands
                self.assertEqual("-enable_flowstate" in command, photo_enabled)
                self.assertEqual(command[command.index("-output_size") + 1], "5760x2880")
                self.assertEqual(runner.help_calls, [])
                self.assertTrue(self.output.is_file())

    def test_supplied_plan_controls_command_without_reprobing(self):
        plan = build_plan(
            Config(width=3840, height=1920, bitrate=55000000, codec="h265"),
            [probe()],
        )
        runner = CommandRunner(self.cfg, probes={})
        media = Media(self.cfg, runner, MemoryAudit())
        media.convert(self.job, self.output, plan=plan)

        command, = runner.conversion_commands
        self.assertEqual(command[command.index("-output_size") + 1], "3840x1920")
        self.assertEqual(command[command.index("-bitrate") + 1], "55000000")
        self.assertIn("-enable_h265_encoder", command)
        self.assertNotIn("-enable_10bit", command)
        self.assertFalse(any(argv[0] == self.cfg.ffprobe for argv, _ in runner.calls))


if __name__ == "__main__":
    unittest.main()
