import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from converter.audit import Runner
from converter.config import Config
from converter.reports import REPORT_NAME
from converter.scan import Job, snapshot
from converter.service import Service
from converter.sidecar import metadata_path


class Audit:
    run_id = "report-failure-test"

    def __init__(self):
        self.events = []

    def emit(self, event, **detail):
        self.events.append((event, detail))


class ServiceReportFailureTests(unittest.TestCase):
    def test_compatible_sdk_rebuild_reuses_and_migrates_completed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo, data = root / "photo", root / "data"
            photo.mkdir()
            data.mkdir()
            source = photo / "completed.insp"
            source.write_bytes(b"original")
            job = Job((source,))
            job.output.write_bytes(b"completed preview")
            metadata = metadata_path(source)
            metadata.write_text("{}\n")
            cfg = Config(photo_root=photo, data_root=data, settle_seconds=0)
            audit = Audit()
            service = Service(cfg, audit, Runner(audit, threading.Event(), 10))
            try:
                old = job.fingerprint("previous-compatible-sdk-build")[0]
                service.state.put(source, old, "success", output_info=snapshot(job.output),
                                  metadata_snapshots=[str(metadata)], metadata_info=[snapshot(metadata)])
                service.state.register_config_alias("previous-compatible-sdk-build", cfg.fingerprint())
                with patch.object(service.media, "convert", side_effect=AssertionError("Unexpected rendering")):
                    with patch.object(service.media, "source_metadata", side_effect=AssertionError("Unexpected extraction")):
                        self.assertEqual(service.scan_once()["skipped"], 1)
                self.assertEqual(service.state.get(source)["fingerprint"], job.fingerprint(cfg.fingerprint())[0])
                self.assertEqual(job.output.read_bytes(), b"completed preview")
            finally:
                service.close()

    def test_empty_directory_report_conflict_counts_as_scan_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo, data = root / "photo", root / "data"
            photo.mkdir()
            data.mkdir()
            report = photo / REPORT_NAME
            report.write_text("User notes that must survive", encoding="utf-8")
            audit = Audit()
            service = Service(Config(photo_root=photo, data_root=data), audit,
                              Runner(audit, threading.Event(), 10))
            try:
                counts = service.scan_once()
            finally:
                service.close()
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(json.loads((data / "health.json").read_text())["status"], "degraded")
            self.assertEqual(report.read_text(), "User notes that must survive")
            self.assertTrue(any(event == "report.scan_failed" for event, _ in audit.events))


if __name__ == "__main__":
    unittest.main()
