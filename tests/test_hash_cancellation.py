import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from converter.audit import Cancelled
from converter.scan import digest


class HashCancellationTests(unittest.TestCase):
    def test_stopped_job_does_not_open_another_file(self):
        stop = threading.Event()
        stop.set()
        source = Mock()
        with self.assertRaises(Cancelled):
            digest(source, stop=stop)
        source.open.assert_not_called()

    def test_hashing_stops_after_the_current_read_chunk(self):
        stop = threading.Event()

        class StoppingFile(io.BytesIO):
            reads = 0

            def read(self, size=-1):
                self.reads += 1
                value = super().read(size)
                stop.set()
                return value

        stream = StoppingFile(b"x" * (4 * 1024 * 1024))
        source = SimpleNamespace(open=lambda mode: stream)
        with self.assertRaises(Cancelled):
            digest(source, stop=stop)
        self.assertEqual(stream.reads, 1)
        self.assertTrue(stream.closed)
