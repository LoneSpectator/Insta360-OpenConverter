import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from converter.reports import (DirectoryReports, LEGACY_REPORT_HEADER, LEGACY_REPORT_NAME,
                               REPORT_FORMAT, REPORT_NAME, report_paths)
from converter.scan import Job
from converter.workspace import TEMP_ROOT_NAME


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
        self.assertFalse(((parent or self.event) / TEMP_ROOT_NAME).exists())
        with ((parent or self.event) / REPORT_NAME).open(encoding="utf-8") as file:
            data = json.load(file)
        self.assertEqual(data["format"], REPORT_FORMAT)
        self.assertEqual(data["schema_version"], 1)
        return data

    def legacy(self, files, parent=None):
        path = (parent or self.event) / LEGACY_REPORT_NAME
        path.write_text(LEGACY_REPORT_HEADER + "old description\n\n"
                        + json.dumps({"files": files}, ensure_ascii=False, indent=4) + "\n",
                        encoding="utf-8")
        return path

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
        self.assertNotIn("error_raw", data[video.primary.name])
        self.assertEqual(data[video.primary.name]["event"], self.event.name)
        self.assertEqual(list(self.event.glob("*.txt")), [self.event / REPORT_NAME])
        self.assertFalse((self.root / REPORT_NAME).exists())

    def test_report_is_standard_json_without_repeated_prose(self):
        self.reports.write(self.job("photo.insp"), "success")
        path = self.event / REPORT_NAME
        self.assertEqual(path.name, "Insta360_OpenConverter_convert_report.txt")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("{\n"))
        self.assertNotIn("本文件", text)
        self.assertNotIn("\t", text)
        self.assertIn('\n    "files": {\n        "photo.insp": {\n            "status": "success",', text)
        self.assertEqual(list(self.data()), ["format", "schema_version", "updated_at", "summary", "failures", "files"])

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
        self.assertEqual(self.data()["summary"]["success"], 2)

    def test_subdirectories_have_independent_reports_inside_each_directory(self):
        child = self.event / "subdirectory"
        child.mkdir()
        first = self.job("same.insp")
        second = self.job("same.insp", child)
        self.reports.write(first, "deferred", reason="Source is empty or still settling after upload")
        self.reports.write(second, "success")
        self.assertEqual(self.data()["files"]["same.insp"]["status"], "deferred")
        self.assertEqual(self.data(child)["files"]["same.insp"]["status"], "success")
        self.assertEqual(set(self.event.rglob("*.txt")), {self.event / REPORT_NAME, child / REPORT_NAME})

    def test_begin_scan_registers_pending_and_tracks_processing_and_results(self):
        first, second, third = [self.job(name) for name in ("a.insp", "b.insv", "c.insv")]
        self.assertEqual(self.reports.begin_scan([first, second, third]), {})
        self.assertEqual(self.data()["summary"], {"failed": 0, "total": 3, "processing": 0,
                                                "pending": 3, "deferred": 0, "success": 0})
        self.reports.write(first, "processing")
        self.assertEqual(self.data()["summary"]["processing"], 1)
        self.assertEqual(self.data()["files"][first.primary.name]["status_text"], "处理中")
        self.reports.write(first, "success", preview_sha256="audit-hash")
        self.reports.write(second, "failed", error="SDK returned without a nonempty output file; inspect command.output logs")
        self.reports.write(third, "deferred", reason="Source is empty or still settling after upload")
        data = self.data()
        self.assertEqual(data["summary"], {"failed": 1, "total": 3, "processing": 0,
                                           "pending": 0, "deferred": 1, "success": 1})
        self.assertEqual(list(data["files"]), [second.primary.name, third.primary.name, first.primary.name])
        failure = data["failures"][0]
        self.assertEqual(failure["file"], second.primary.name)
        self.assertEqual(failure["status_text"], "失败")
        self.assertEqual(failure["error_code"], "sdk_no_output")
        self.assertNotIn("error_raw", failure)
        self.assertIn("SDK returned", data["files"][second.primary.name]["error_raw"])
        self.assertNotEqual(data["files"][third.primary.name]["reason"], data["files"][third.primary.name]["reason_raw"])
        self.assertEqual(data["files"][first.primary.name]["preview_sha256"], "audit-hash")

    def test_begin_scan_does_not_rewrite_unchanged_report(self):
        job = self.job("photo.insp")
        self.reports.begin_scan([job])
        self.reports.write(job, "success")
        path = self.event / REPORT_NAME
        previous = (path.read_bytes(), path.stat().st_mtime_ns)
        self.audit.events.clear()
        self.assertEqual(self.reports.begin_scan([job]), {})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), previous)
        self.assertFalse(self.audit.events)

    def test_begin_scan_retains_missing_file_history_but_excludes_it_from_counts(self):
        removed, kept = self.job("removed.insp"), self.job("kept.insp")
        self.reports.begin_scan([removed, kept])
        self.reports.write(removed, "failed", error="SDK returned without a nonempty output file")
        self.reports.write(kept, "success")
        removed.primary.unlink()
        self.reports.begin_scan([kept])
        data = self.data()
        self.assertFalse(data["files"][removed.primary.name]["present"])
        self.assertIn("error_raw", data["files"][removed.primary.name])
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["failed"], 0)
        self.assertEqual(data["failures"], [])
        self.assertEqual(data["summary"]["success"], 1)

    def test_begin_scan_resets_interrupted_processing_to_pending(self):
        job = self.job("photo.insp")
        self.reports.write(job, "processing")
        self.reports.begin_scan([job])
        data = self.data()
        self.assertEqual(data["files"][job.primary.name]["status"], "pending")
        self.assertEqual(data["files"][job.primary.name]["status_text"], "待处理")
        self.assertEqual(data["summary"]["processing"], 0)
        self.assertEqual(data["summary"]["pending"], 1)

    def test_begin_scan_marks_last_removed_file_historical_and_zeroes_counts(self):
        job = self.job("last.insv")
        self.reports.write(job, "success", preview_sha256="previous-output")
        job.primary.unlink()
        self.assertEqual(self.reports.begin_scan([], directories={self.event}), {})
        data = self.data()
        self.assertEqual(data["summary"]["total"], 0)
        self.assertFalse(data["files"][job.primary.name]["present"])
        self.assertEqual(data["files"][job.primary.name]["preview_sha256"], "previous-output")

    def test_begin_scan_ignores_unrelated_legacy_in_empty_directory(self):
        old = self.event / LEGACY_REPORT_NAME
        old.write_text("user notes")
        self.assertEqual(self.reports.begin_scan([], directories={self.event}), {})
        self.assertEqual(old.read_text(), "user notes")
        self.assertFalse((self.event / REPORT_NAME).exists())

    def test_begin_scan_isolates_conflicting_directory(self):
        child = self.event / "child"
        child.mkdir()
        bad, good = self.job("bad.insp"), self.job("good.insp", child)
        target = self.event / REPORT_NAME
        target.write_text("unrelated notes")
        errors = self.reports.begin_scan([bad, good])
        self.assertEqual(set(errors), {self.event})
        self.assertEqual(target.read_text(), "unrelated notes")
        self.assertEqual(self.data(child)["summary"]["pending"], 1)

    def test_managed_legacy_report_migrates_without_losing_history(self):
        job = self.job("photo.insp")
        old = self.legacy({"historical.insv": {"status": "success", "run_id": "old-run", "preview_sha256": "oldhash"},
                           job.primary.name: {"status": "failed", "error": "SDK returned without a nonempty output file"}})
        self.reports.begin_scan([job])
        data = self.data()
        self.assertFalse(old.exists())
        self.assertEqual(data["files"]["historical.insv"]["preview_sha256"], "oldhash")
        self.assertFalse(data["files"]["historical.insv"]["present"])
        self.assertEqual(data["files"][job.primary.name]["error_code"], "sdk_no_output")
        self.assertNotEqual(data["files"][job.primary.name]["error"], data["files"][job.primary.name]["error_raw"])
        self.assertTrue(any(event == "file.remove_legacy_report" for event, _ in self.audit.events))

    def test_unrelated_or_malformed_legacy_is_preserved(self):
        job = self.job("photo.insp")
        old = self.event / LEGACY_REPORT_NAME
        for content in ("unrelated notes", LEGACY_REPORT_HEADER + "\ninvalid-json"):
            with self.subTest(content=content):
                old.write_text(content)
                self.reports.write(job, "success")
                self.assertEqual(old.read_text(), content)
                (self.event / REPORT_NAME).unlink()

    def test_non_utf8_legacy_is_preserved_and_does_not_block_new_report(self):
        job = self.job("photo.insp")
        old = self.event / LEGACY_REPORT_NAME
        old.write_bytes(b"\xff\xfe user binary data")
        self.reports.write(job, "success")
        self.assertEqual(old.read_bytes(), b"\xff\xfe user binary data")
        self.assertEqual(self.data()["summary"]["success"], 1)

    def test_failed_new_publication_preserves_legacy(self):
        job = self.job("photo.insp")
        old = self.legacy({job.primary.name: {"status": "success", "run_id": "old-run"}})
        previous = old.read_bytes()
        with patch("converter.reports.os.link", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.reports.write(job, "success")
        self.assertEqual(old.read_bytes(), previous)
        self.assertFalse((self.event / REPORT_NAME).exists())

    def test_modified_legacy_is_not_deleted_after_new_publication(self):
        job = self.job("photo.insp")
        old = self.legacy({job.primary.name: {"status": "success"}})
        from converter import reports as module
        original = module.os.link

        def modify_legacy(source, destination):
            old.write_text("external replacement")
            return original(source, destination)

        with patch("converter.reports.os.link", side_effect=modify_legacy):
            self.reports.write(job, "success")
        self.assertEqual(old.read_text(), "external replacement")
        self.assertEqual(self.data()["summary"]["success"], 1)

    def test_interrupted_legacy_cleanup_recovers_on_next_scan(self):
        job = self.job("photo.insp")
        raw = "SDK returned without a nonempty output file"
        old = self.legacy({job.primary.name: {"status": "failed", "error": raw,
                                              "run_id": "old-run", "preview_sha256": "oldhash"},
                           "removed.insv": {"status": "deferred", "reason": "Source is empty or still settling after upload"}})
        original = Path.unlink

        def interrupt(path, *args, **kwargs):
            if path == old:
                raise InterruptedError("Interrupted after publishing the canonical report")
            return original(path, *args, **kwargs)

        with patch("converter.reports.Path.unlink", new=interrupt):
            errors = self.reports.begin_scan([job])
        self.assertIn(self.event, errors)
        self.assertTrue(old.is_file())
        data = self.data()
        self.assertEqual(data["files"][job.primary.name]["error_raw"], raw)
        self.assertFalse(data["files"]["removed.insv"]["present"])
        self.assertEqual(self.reports.begin_scan([job]), {})
        self.assertFalse(old.exists())
        recovered = self.data()
        self.assertEqual(recovered["files"], data["files"])
        self.assertEqual(recovered["files"][job.primary.name]["preview_sha256"], "oldhash")

    def test_legacy_history_not_represented_in_current_report_is_retained(self):
        job = self.job("photo.insp")
        self.reports.write(job, "success", preview_sha256="current-hash")
        old = self.legacy({"extra-history.insv": {"status": "success", "preview_sha256": "legacy-hash"}})
        previous = old.read_bytes()
        self.assertEqual(self.reports.begin_scan([job]), {})
        self.assertEqual(old.read_bytes(), previous)
        self.assertNotIn("extra-history.insv", self.data()["files"])
        self.assertTrue(any(event == "report.legacy_cleanup_skipped" for event, _ in self.audit.events))

    def test_legacy_same_file_with_different_processing_details_is_retained(self):
        job = self.job("photo.insp")
        self.reports.write(job, "success", preview_sha256="current-hash")
        old = self.legacy({job.primary.name: {"status": "success", "preview_sha256": "different-hash"}})
        previous = old.read_bytes()
        self.reports.write(job, "success", preview_sha256="current-hash")
        self.assertEqual(old.read_bytes(), previous)
        self.assertEqual(self.data()["files"][job.primary.name]["preview_sha256"], "current-hash")
        self.assertTrue(any(event == "report.legacy_cleanup_skipped" for event, _ in self.audit.events))

    def test_new_result_does_not_delete_legacy_details_it_replaced(self):
        job = self.job("photo.insp")
        self.reports.write(job, "failed", error="SDK returned without a nonempty output file")
        old_entry = self.data()["files"][job.primary.name]
        old = self.legacy({job.primary.name: old_entry})
        previous = old.read_bytes()
        self.reports.write(job, "success", preview_sha256="new-output")
        self.assertEqual(old.read_bytes(), previous)
        self.assertEqual(self.data()["files"][job.primary.name]["status"], "success")

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
        self.reports.write(unrelated, "deferred", reason="Source is empty or still settling after upload")
        self.reports.write(Job((first,)), "success")
        self.assertFalse(self.reports.has(pair, "success"))
        self.reports.write(pair, "failed", error="SDK failure")
        self.assertTrue(self.reports.has(pair, "failed"))
        self.reports.write(pair, "success")
        self.assertTrue(self.reports.has(pair, "success"))
        self.assertEqual(self.data()["files"][unrelated.primary.name]["reason_raw"], "Source is empty or still settling after upload")

    def test_has_rejects_invalid_report_instead_of_masking_conflict(self):
        job = self.job("photo.insp")
        (self.event / REPORT_NAME).write_text("unrelated notes")
        with self.assertRaisesRegex(RuntimeError, "unrelated text file"):
            self.reports.has(job, "success")

    def test_has_checks_other_directories_after_a_missing_entry(self):
        child = self.event / "child"
        child.mkdir()
        first, second = self.job("photo.insp").primary, self.job("video.insv", child).primary
        (child / REPORT_NAME).write_text("unrelated notes")
        with self.assertRaisesRegex(RuntimeError, "unrelated text file"):
            self.reports.has(Job((first, second)), "success")

    def test_unrelated_malformed_directory_and_symlink_paths_are_preserved(self):
        job = self.job("photo.insp")
        target = self.event / REPORT_NAME
        for content in ("user notes", '{"files": {}}',
                        json.dumps({"format": REPORT_FORMAT, "schema_version": 1, "files": []}),
                        json.dumps({"format": REPORT_FORMAT, "schema_version": 1, "files": {}}),
                        json.dumps({"format": REPORT_FORMAT, "schema_version": 1, "files": {"../outside": {}}})):
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
        self.assertFalse((self.event / TEMP_ROOT_NAME).exists())

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
        self.assertFalse((self.event / TEMP_ROOT_NAME).exists())


if __name__ == "__main__":
    unittest.main()
