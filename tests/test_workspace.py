from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import fcntl
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


@contextmanager
def delayed_open_file_deletion():
    """Model NFS retaining an unlinked open lock as a hidden .nfs file."""
    from converter import workspace as module
    original_open, original_unlink, original_close = os.open, os.unlink, os.close
    locks, retained = set(), {}
    observations = {"delayed_unlinks": 0}

    def opened(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if path == ".workspace.lock":
            locks.add(descriptor)
        return descriptor

    def unlinked(path, *args, **kwargs):
        if path == ".workspace.lock":
            parent = kwargs["dir_fd"]
            info = os.stat(path, dir_fd=parent)
            for descriptor in tuple(locks):
                opened_info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == (opened_info.st_dev, opened_info.st_ino):
                    duplicate = os.dup(parent)
                    os.rename(path, ".nfs-simulated", src_dir_fd=parent, dst_dir_fd=parent)
                    retained[descriptor] = duplicate
                    observations["delayed_unlinks"] += 1
                    return
        return original_unlink(path, *args, **kwargs)

    def closed(descriptor):
        if descriptor in retained:
            parent = retained.pop(descriptor)
            original_unlink(".nfs-simulated", dir_fd=parent)
            original_close(parent)
        locks.discard(descriptor)
        return original_close(descriptor)

    with patch.object(module.os, "open", opened), patch.object(module.os, "unlink", unlinked), \
            patch.object(module.os, "close", closed):
        yield observations


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

    def test_open_file_delete_deferral_does_not_leave_empty_report_directories(self):
        with delayed_open_file_deletion() as observed:
            with temporary_workspace(self.parent, self.audit, kind="report") as work:
                (work / "report.txt").write_text("published data")
        self.assertEqual(observed["delayed_unlinks"], 0)
        self.assertFalse(self.root.exists())
        self.assertFalse(any(event == "workspace.cleanup_failed" for event, _ in self.audit.events))

    def test_stale_cleanup_also_closes_its_lock_before_deletion(self):
        self.crash_workspace()
        with delayed_open_file_deletion() as observed:
            self.assertEqual(cleanup_stale_workspaces(self.parent, self.audit), 1)
        self.assertEqual(observed["delayed_unlinks"], 0)
        self.assertFalse(self.root.exists())

    def test_cleanup_failure_preserves_result_and_recoverable_controls(self):
        from converter import workspace as module
        original = module.os.rmdir
        name = None

        def unavailable(path, *args, **kwargs):
            if path == name:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
            return original(path, *args, **kwargs)

        def operation():
            nonlocal name
            with temporary_workspace(self.parent, self.audit, kind="report") as work:
                name = work.name
                (work / "staged.txt").write_text("temporary")
                return "operation completed"

        with patch.object(module.os, "rmdir", unavailable):
            self.assertEqual(operation(), "operation completed")
        leftover = self.root / name
        marker = json.loads((leftover / ".workspace.json").read_text())
        self.assertEqual(marker["phase"], "cleanup")
        with (leftover / ".workspace.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        warning = next(detail for event, detail in self.audit.events if event == "workspace.cleanup_failed")
        self.assertEqual(warning["error_code"], "workspace_cleanup_failed")
        self.assertEqual(warning["level"], "warning")
        self.assertIn("清理未完成", warning["message"])
        self.assertIn("Directory not empty", warning["error"])
        self.assertEqual(cleanup_stale_workspaces(self.parent, self.audit), 1)
        self.assertFalse(self.root.exists())

    def test_cleanup_failure_does_not_replace_original_sdk_error(self):
        from converter import workspace as module
        original = module.os.rmdir
        name = None
        expected = RuntimeError("SDK processing failed")

        def unavailable(path, *args, **kwargs):
            if path == name:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
            return original(path, *args, **kwargs)

        with patch.object(module.os, "rmdir", unavailable):
            with self.assertRaises(RuntimeError) as caught:
                with temporary_workspace(self.parent, self.audit, kind="preview") as work:
                    name = work.name
                    raise expected
        self.assertIs(caught.exception, expected)
        self.assertEqual(cleanup_stale_workspaces(self.parent), 1)

    def test_only_cleanup_phase_can_recover_a_missing_lock(self):
        work = self.crash_workspace()
        (work / ".workspace.lock").unlink()
        self.assertEqual(cleanup_stale_workspaces(self.parent), 0)
        self.assertTrue((work / "partial.mp4").is_file())
        marker_path = work / ".workspace.json"
        marker = json.loads(marker_path.read_text())
        marker["phase"] = "cleanup"
        marker_path.write_text(json.dumps(marker, indent=4))
        self.assertEqual(cleanup_stale_workspaces(self.parent), 1)
        self.assertFalse(self.root.exists())

    def test_empty_legacy_remnants_are_recovered_without_deleting_unknown_data(self):
        self.root.mkdir()
        empty = self.root / "report-0123456789abcdef"
        empty.mkdir()
        nonempty = self.root / "report-1111111111111111"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("keep")
        unknown = self.root / "user-empty-directory"
        unknown.mkdir()
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("outside")
        link = self.root / "report-2222222222222222"
        link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup_stale_workspaces(self.parent, self.audit), 1)
        self.assertFalse(empty.exists())
        self.assertEqual((nonempty / "keep.txt").read_text(), "keep")
        self.assertTrue(unknown.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "keep.txt").read_text(), "outside")

    def test_cleanup_during_allocation_does_not_remove_the_new_workspace(self):
        from converter import workspace as module
        original = module.os.mkdir
        cleaned_during_allocation = []

        def cleanup_after_mkdir(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith(".alloc-report-"):
                cleaned_during_allocation.append(cleanup_stale_workspaces(self.parent))
            return result

        with patch.object(module.os, "mkdir", cleanup_after_mkdir):
            with temporary_workspace(self.parent, self.audit, kind="report") as work:
                self.assertTrue(work.name.startswith("report-"))
                self.assertTrue(work.is_dir())
                self.assertFalse(list(self.root.glob(".alloc-*")))
        self.assertEqual(cleaned_during_allocation, [0])
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
