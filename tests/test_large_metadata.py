"""Large QuickTime atoms must not hide metadata or disable metadata writes."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest

from converter.audit import Runner
from converter.config import Config
from converter.media import Media


EXIFTOOL = os.environ.get("EXIFTOOL", "exiftool")


class MemoryAudit:
    def emit(self, *args, **kwargs):
        pass


def atom(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


def sparse_movie(path):
    """Place a movie header after a 4 GiB hole, without encoding video."""
    epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
    capture = datetime(2024, 6, 1, 4, 34, 56, tzinfo=timezone.utc)
    stamp = int((capture - epoch).total_seconds())
    matrix = (0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    header = (b"\0" * 4 + struct.pack(">IIII", stamp, stamp, 1000, 1000)
              + struct.pack(">Ih", 0x10000, 0x100) + b"\0" * 10
              + struct.pack(">9I", *matrix) + b"\0" * 24 + struct.pack(">I", 2))
    ftyp = atom(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isommp42")
    padding = (1 << 32) + 32
    with path.open("wb") as stream:
        stream.write(ftyp)
        stream.write(struct.pack(">I4sQ", 1, b"free", padding))
        stream.seek(len(ftyp) + padding)
        stream.write(atom(b"moov", atom(b"mvhd", header)))


class StrictMetadataRunner:
    """Reject reads and writes which would fail on large source/output atoms."""

    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        if not any(argv[index:index + 2] == ["-api", "LargeFileSupport=1"]
                   for index in range(len(argv) - 1)):
            raise AssertionError("Metadata operation omitted large-file support")
        if "-j" in argv:
            return json.dumps([{
                "IFD0:ImageWidth": 128, "IFD0:ImageHeight": 64,
                "ExifIFD:ExifImageWidth": 128, "ExifIFD:ExifImageHeight": 64,
                "XMP-GPano:ProjectionType": "equirectangular",
                "XMP-GPano:UsePanoramaViewer": True,
                "XMP-GSpherical:ProjectionType": "equirectangular",
                "XMP-GSpherical:Spherical": True,
            }])
        return ""


class LargeMetadataTests(unittest.TestCase):
    def test_source_read_copy_write_and_verification_enable_large_file_support(self):
        for suffix in (".mp4", ".jpg"):
            with self.subTest(suffix=suffix):
                cfg = Config()
                runner = StrictMetadataRunner()
                media = Media(cfg, runner, MemoryAudit())
                source = Path("source.insv" if suffix == ".mp4" else "source.insp")
                output = Path("preview" + suffix)
                media.metadata(source)
                media.preserve_metadata(source, output, {}, SimpleNamespace(width=128, height=64))
                self.assertEqual(sum("-TagsFromFile" in argv for argv in runner.calls), 1)
                self.assertEqual(sum("-overwrite_original" in argv for argv in runner.calls), 2)
                self.assertEqual(sum("-j" in argv for argv in runner.calls), 3 if suffix == ".jpg" else 2)

    @unittest.skipUnless(shutil.which(EXIFTOOL), "ExifTool is required")
    def test_capture_time_after_a_large_atom_is_read_from_a_sparse_movie(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "large.insv"
            sparse_movie(source)
            info = source.stat()
            self.assertGreater(info.st_size, 1 << 32)
            self.assertLess(info.st_blocks * 512, 64 * 1024)

            # Explicitly reproduce the disabled API's partial, successful read.
            result = subprocess.run([
                EXIFTOOL, "-api", "LargeFileSupport=0", "-j", "-G1", "-n", str(source),
            ], check=True, capture_output=True, text=True)
            incomplete, = json.loads(result.stdout)
            self.assertIn("LargeFileSupport", incomplete["ExifTool:Warning"])
            self.assertNotIn("QuickTime:CreateDate", incomplete)

            audit = MemoryAudit()
            media = Media(Config(exiftool=EXIFTOOL), Runner(audit, threading.Event(), 30), audit)
            actual = media.metadata(source)
            self.assertEqual(actual.get("QuickTime:CreateDate"), "2024:06:01 04:34:56")
            self.assertNotIn("ExifTool:Warning", actual)
            self.assertEqual(source.stat().st_size, info.st_size)
            self.assertLess(source.stat().st_blocks * 512, 64 * 1024)


if __name__ == "__main__":
    unittest.main()
