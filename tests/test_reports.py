import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter.reports import DirectoryReports, REPORT_HEADER, REPORT_NAME, report_paths
from converter.scan import Job


class Audit:
    run_id = "test-run"

    def __init__(self):
        self.events = []

    def emit(self, event, **detail):
        self.events.append((event, detail))


class DirectoryReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.event = self.root / "20261104.中文 事件"
        self.event.mkdir()
        self.audit = Audit()
        self.reports = DirectoryReports(self.audit)

    def job(self, name, parent=None):
        source = (parent or self.event) / name
        source.write_bytes(b"source")
        return Job((source,))

    def data(self, parent=None):
        text = ((parent or self.event) / REPORT_NAME).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(REPORT_HEADER))
        return json.loads(text.split("\n\n", 1)[1])

    def test_multiple_files_share_one_report_and_updates_preserve_other_entries(self):
        photo = self.job("IMG_一.insp")
        video = self.job("VID_二.insv")
        self.reports.write(photo, "success", capture_fields={"Model": "X4"})
        self.reports.write(video, "failed", error="SDK error")
        self.reports.write(video, "success", preview_sha256="done")
        data = self.data()["files"]
        self.assertEqual(set(data), {photo.primary.name, video.primary.name})
        self.assertEqual(data[photo.primary.name]["capture_fields"]["Model"], "X4")
        self.assertEqual(data[video.primary.name]["status"], "success")
        self.assertNotIn("error", data[video.primary.name])
        self.assertEqual(data[video.primary.name]["event"], self.event.name)
        self.assertEqual(list(self.event.glob("*.txt")), [self.event / REPORT_NAME])
        self.assertFalse((self.root / REPORT_NAME).exists())

    def test_pair_has_separate_entries_and_one_report(self):
        first = self.job("VID_00_001.insv").primary
        second = self.job("VID_10_001.insv").primary
        job = Job((first, second))
        self.assertEqual(report_paths(job), (self.event / REPORT_NAME,))
        self.reports.write(job, "success")
        entries = self.data()["files"]
        self.assertEqual(set(entries), {first.name, second.name})
        self.assertEqual(entries[first.name]["preview"], entries[second.name]["preview"])
        self.assertEqual(entries[first.name]["sources"], [str(first), str(second)])

    def test_subdirectories_have_independent_reports_inside_each_directory(self):
        child = self.event / "subdirectory"
        child.mkdir()
        first = self.job("same.insp")
        second = self.job("same.insp", child)
        self.reports.write(first, "deferred", reason="uploading")
        self.reports.write(second, "success")
        self.assertEqual(self.data()["files"]["same.insp"]["status"], "deferred")
        self.assertEqual(self.data(child)["files"]["same.insp"]["status"], "success")
        self.assertEqual(set(self.event.rglob("*.txt")),
                         {self.event / REPORT_NAME, child / REPORT_NAME})

    def test_report_uses_four_space_json_indentation(self):
        self.reports.write(self.job("photo.insp"), "success")
        text = (self.event / REPORT_NAME).read_text(encoding="utf-8")
        self.assertIn('\n    "files": {\n        "photo.insp": {\n            "status": "success",', text)
        self.assertNotIn("\t", text)

    def test_check_does_not_write_report(self):
        job = self.job("photo.insp")
        self.reports.check(job)
        self.assertFalse((self.event / REPORT_NAME).exists())
        self.assertFalse(self.audit.events)

    def test_has_requires_each_source_entry_with_requested_status(self):
        unrelated = self.job("other.insp")
        first = self.job("VID_00_001.insv").primary
        second = self.job("VID_10_001.insv").primary
        pair = Job((first, second))
        self.assertFalse(self.reports.has(pair, "success"))
        self.reports.write(unrelated, "deferred", reason="uploading")
        self.reports.write(Job((first,)), "success")
        self.assertFalse(self.reports.has(pair, "success"))
        self.reports.write(pair, "failed", error="retry")
        self.assertTrue(self.reports.has(pair, "failed"))
        self.assertFalse(self.reports.has(pair, "success"))
        self.reports.write(pair, "success")
        self.assertTrue(self.reports.has(pair, "success"))
        self.assertEqual(self.data()["files"][unrelated.primary.name]["reason"], "uploading")

    def test_has_rejects_invalid_report_instead_of_masking_conflict(self):
        job = self.job("photo.insp")
        (self.event / REPORT_NAME).write_text("unrelated notes")
        with self.assertRaisesRegex(RuntimeError, "unrelated text file"):
            self.reports.has(job, "success")

    def test_has_checks_other_directories_after_a_missing_entry(self):
        child = self.event / "child"
        child.mkdir()
        first = self.job("photo.insp").primary
        second = self.job("video.insv", child).primary
        (child / REPORT_NAME).write_text("unrelated notes")
        with self.assertRaisesRegex(RuntimeError, "unrelated text file"):
            self.reports.has(Job((first, second)), "success")

    def test_unrelated_malformed_directory_and_symlink_paths_are_preserved(self):
        job = self.job("photo.insp")
        target = self.event / REPORT_NAME
        for content in ("user notes", REPORT_HEADER + "\ninvalid-json",
                        REPORT_HEADER + '\n{"files": []}',
                        REPORT_HEADER + '\n{"files": {"../outside": {}}}'):
            with self.subTest(content=content):
                target.write_text(content)
                with self.assertRaises(RuntimeError):
                    self.reports.check(job)
                with self.assertRaises(RuntimeError):
                    self.reports.write(job, "success")
                self.assertEqual(target.read_text(), content)
        target.unlink()
        target.mkdir()
        with self.assertRaises(RuntimeError):
            self.reports.write(job, "success")
        target.rmdir()
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        target.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.reports.write(job, "success")
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(target.is_symlink())

    def test_report_changed_during_write_is_preserved_and_temporary_is_removed(self):
        job = self.job("photo.insp")
        self.reports.write(job, "success")
        target = self.event / REPORT_NAME
        from converter import reports as module
        original = module._snapshot
        calls = 0

        def changed(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                target.write_text("external replacement")
            return original(path)

        with patch("converter.reports._snapshot", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "changed before publication"):
                self.reports.write(job, "failed")
        self.assertEqual(target.read_text(), "external replacement")
        self.assertFalse(list(self.event.glob(".insta360-preview-report-*")))

    def test_first_report_created_concurrently_is_not_overwritten(self):
        job = self.job("photo.insp")
        target = self.event / REPORT_NAME
        from converter import reports as module
        original = module.os.link

        def create_user_file(source, destination):
            target.write_text("created concurrently")
            return original(source, destination)

        with patch("converter.reports.os.link", side_effect=create_user_file):
            with self.assertRaises(FileExistsError):
                self.reports.write(job, "success")
        self.assertEqual(target.read_text(), "created concurrently")
        self.assertFalse(list(self.event.glob(".insta360-preview-report-*")))


if __name__ == "__main__":
    unittest.main()
