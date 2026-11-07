"""Concurrent tasks must retain active work and finish without cleanup races."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import SimpleQueue
import tempfile
from threading import Barrier
import unittest

from converter.workspace import (TEMP_ROOT_NAME, cleanup_stale_workspaces,
                                 temporary_workspace)


class Audit:
    def __init__(self):
        self.events = SimpleQueue()

    def emit(self, event, **fields):
        self.events.put((event, fields))


class WorkspaceThreadTests(unittest.TestCase):
    def test_three_active_tasks_clean_nested_work_without_racing_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            audit = Audit()
            ready = Barrier(3)
            finished = Barrier(3)

            def task(index):
                for iteration in range(30):
                    with temporary_workspace(parent, audit, kind="preview") as work:
                        sentinel = work / "active.mp4"
                        content = f"task={index};iteration={iteration}".encode()
                        sentinel.write_bytes(content)
                        # Every task must reach the yielded workspace together:
                        # the bookkeeping lock must never cover SDK processing.
                        ready.wait(timeout=10)
                        for _ in range(3):
                            with temporary_workspace(parent, audit, kind="metadata-json") as nested:
                                (nested / "metadata.json").write_text("{}")
                                self.assertEqual(sentinel.read_bytes(), content)
                            self.assertTrue(sentinel.is_file())
                        finished.wait(timeout=10)

            with ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(task, range(3)))
            cleanup_stale_workspaces(parent, audit)
            self.assertFalse((parent / TEMP_ROOT_NAME).exists())
            events = []
            while not audit.events.empty():
                events.append(audit.events.get())
            warnings = [fields for event, fields in events
                        if event == "workspace.cleanup_failed"]
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
