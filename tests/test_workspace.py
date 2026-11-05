from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from converter.workspace import (TEMP_ROOT_NAME, cleanup_stale_workspaces,
                                 temporary_workspace)


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, event, **detail):
        self.events.append((event, detail))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / TEMP_ROOT_NAME
        self.audit = Audit()

    def test_work_is_contained_in_one_predictable_root_and_removed_after_success(self):
        with temporary_workspace(self.parent, self.audit, kind="preview") as work:
            self.assertEqual(work.parent, self.root)
            self.assertTrue(work.name.startswith("preview-"))
            self.assertEqual(list(self.parent.iterdir()), [self.root])
            (work / "output.mp4").write_bytes(b"temporary media")
            marker = (work / ".workspace.json").read_text()
            self.assertIn('\n    "schema_version": 1,', marker)
            self.assertEqual(json.loads(marker)["kind"], "preview")
        self.assertFalse(self.root.exists())
        events = [event for event, _ in self.audit.events]
        self.assertIn("file.create_temporary_directory", events)
        self.assertIn("file.remove_temporary_directory", events)

    def test_failure_and_normal_cancellation_remove_temporary_work(self):
        class Cancelled(BaseException):
            pass

        for error in (RuntimeError("failed"), Cancelled()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with temporary_workspace(self.parent) as work:
                        (work / "partial.jpg").write_bytes(b"partial")
                        raise error
                self.assertFalse(self.root.exists())

    def test_nested_contexts_preserve_active_work_and_remove_root_after_last_exit(self):
        with temporary_workspace(self.parent, kind="metadata") as outer:
            with temporary_workspace(self.parent, kind="merge") as inner:
                self.assertNotEqual(inner, outer)
                self.assertTrue(outer.is_dir())
                self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
                self.assertTrue(inner.is_dir())
            self.assertTrue(outer.is_dir())
            self.assertFalse(inner.exists())
        self.assertFalse(self.root.exists())

    def test_read_only_sdk_subdirectories_can_be_cleaned(self):
        with temporary_workspace(self.parent) as work:
            nested = work / "SDK/lib"
            nested.mkdir(parents=True)
            (nested / "library.so").write_bytes(b"closed SDK fixture")
            (nested / "library.so").chmod(0o444)
            nested.chmod(0o555)
            nested.parent.chmod(0o555)
        self.assertFalse(self.root.exists())

    def crash_workspace(self):
        code = (
            "import os,sys; from pathlib import Path; "
            "from converter.workspace import temporary_workspace\n"
            "with temporary_workspace(Path(sys.argv[1]), kind='interrupted') as work:\n"
            "    (work/'partial.mp4').write_bytes(b'partial output')\n"
            "    os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", code, str(self.parent)], check=True)
        return next(self.root.iterdir())

    def test_crashed_process_leaves_marked_work_that_next_scan_cleans(self):
        work = self.crash_workspace()
        self.assertTrue((work / "partial.mp4").is_file())
        self.assertEqual(cleanup_stale_workspaces(self.parent, self.audit), 1)
        self.assertFalse(self.root.exists())
        self.assertTrue(any(event == "file.remove_stale_workspace"
                            for event, _ in self.audit.events))

    def test_new_task_also_cleans_stale_work_before_starting(self):
        old = self.crash_workspace()
        with temporary_workspace(self.parent) as work:
            self.assertFalse(old.exists())
            self.assertEqual(list(self.root.iterdir()), [work])
        self.assertFalse(self.root.exists())

    def test_unknown_contents_and_symlink_children_are_preserved(self):
        self.root.mkdir()
        unknown = self.root / "user-data"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("user data")
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("outside")
        link = self.root / "linked-work"
        link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        with temporary_workspace(self.parent):
            self.assertTrue(unknown.is_dir())
        self.assertEqual((unknown / "keep.txt").read_text(), "user data")
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "keep.txt").read_text(), "outside")

    def test_root_symlink_is_never_followed_or_removed(self):
        outside = self.parent / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep")
        self.root.symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        with self.assertRaisesRegex(RuntimeError, "not a real directory"):
            with temporary_workspace(self.parent):
                self.fail("Symlink workspace root must not be used")
        self.assertTrue(self.root.is_symlink())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_symlinks_inside_owned_work_are_unlinked_without_touching_targets(self):
        sentinel = self.parent / "keep.txt"
        sentinel.write_text("keep")
        with temporary_workspace(self.parent) as work:
            (work / "outside-link").symlink_to(sentinel)
        self.assertFalse(self.root.exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_invalid_marker_or_lock_symlink_is_not_removed(self):
        work = self.crash_workspace()
        (work / ".workspace.json").write_text('{"format": "unrelated"}')
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        self.assertTrue(work.is_dir())
        marker = {"format": "Insta360_OpenConverter_workspace", "schema_version": 1,
                  "kind": "interrupted"}
        (work / ".workspace.json").write_text(json.dumps(marker, indent=4))
        lock = work / ".workspace.lock"
        lock.unlink()
        sentinel = self.parent / "outside.lock"
        sentinel.write_text("keep")
        lock.symlink_to(sentinel)
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        self.assertTrue(work.is_dir())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_partial_creation_failure_does_not_leave_an_empty_root(self):
        from converter import workspace as module
        original = module.os.open

        def fail_lock(path, *args, **kwargs):
            if path == ".workspace.lock":
                raise PermissionError("injected lock creation failure")
            return original(path, *args, **kwargs)

        with patch("converter.workspace.os.open", side_effect=fail_lock):
            with self.assertRaisesRegex(PermissionError, "injected"):
                with temporary_workspace(self.parent):
                    self.fail("Creation must fail before work begins")
        self.assertFalse(self.root.exists())

    def test_concurrent_tasks_keep_each_others_workspaces(self):
        def task(_):
            for _ in range(12):
                with temporary_workspace(self.parent) as work:
                    (work / "file.txt").write_text("temporary")
                    self.assertTrue(work.is_dir())

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(task, range(4)))
        self.assertFalse(self.root.exists())

    def test_cleanup_missing_root_does_not_create_directories(self):
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_kind_cannot_escape_the_hidden_root(self):
        with self.assertRaises(ValueError):
            with temporary_workspace(self.parent, kind="../outside"):
                self.fail("Invalid kind must be rejected")
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
