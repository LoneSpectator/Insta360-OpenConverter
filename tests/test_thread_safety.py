from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from converter.audit import Audit, Runner
from converter.reports import DirectoryReports, REPORT_NAME
from converter.scan import Job
from converter.state import State


class QuietAudit:
    run_id = "concurrent-test"

    def emit(self, *args, **kwargs):
        pass


class SharedStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_worker_threads_preserve_jobs_artifacts_and_config_aliases(self):
        state = State(self.root / "state.sqlite3")
        self.addCleanup(state.close)
        ready = threading.Barrier(3)

        def update(worker):
            ready.wait(timeout=10)
            for number in range(10):
                key = f"source-{worker}-{number}"
                state.put(key, "recipe", "success", worker=worker, number=number)
                state.intend(key, "temporary")
                state.published(key, "published")
                self.assertEqual(state.get(key)["status"], "success")
                self.assertEqual(state.hashes(key), ["published"])
            state.register_config_alias(f"previous-{worker}", "current")

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(update, range(3)))
        self.assertEqual(state.config_aliases("current"), [f"previous-{i}" for i in range(3)])
        for worker in range(3):
            for number in range(10):
                record = state.get(f"source-{worker}-{number}")
                self.assertEqual(json.loads(record["detail"]), {"worker": worker, "number": number})

    def test_artifact_intent_read_modify_write_is_atomic(self):
        state = State(self.root / "state.sqlite3")
        self.addCleanup(state.close)
        hashes = state.hashes
        ready = threading.Barrier(2)

        def slow_read(path):
            result = hashes(path)
            time.sleep(0.02)
            return result

        def intend(value):
            ready.wait(timeout=10)
            state.intend("same-preview", value)

        with patch.object(state, "hashes", side_effect=slow_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(intend, ("first", "second")))
        self.assertEqual(set(state.hashes("same-preview")), {"first", "second"})


class ConcurrentReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = DirectoryReports(QuietAudit())

    def job(self, directory, name):
        parent = self.root / directory
        parent.mkdir(exist_ok=True)
        source = parent / name
        source.write_bytes(b"source")
        return Job((source,))

    def test_same_directory_workers_keep_all_results_and_failure_summary(self):
        jobs = [self.job("event", f"photo-{i}.insp") for i in range(3)]
        self.assertEqual(self.reports.begin_scan(jobs), {})
        ready = threading.Barrier(3)

        def write(item):
            index, job = item
            ready.wait(timeout=10)
            self.reports.check(job)
            self.reports.write(job, "processing")
            status = "failed" if index == 1 else "success"
            self.reports.write(job, status, error="SDK returned without a nonempty output file")
            self.assertTrue(self.reports.has(job, status))

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(write, enumerate(jobs)))
        report = json.loads((self.root / "event" / REPORT_NAME).read_text())
        self.assertEqual(set(report["files"]), {job.primary.name for job in jobs})
        self.assertEqual(report["summary"], {"failed": 1, "total": 3, "processing": 0,
                                              "pending": 0, "deferred": 0, "success": 2})
        self.assertEqual(report["failures"][0]["file"], jobs[1].primary.name)
        self.assertEqual(report["failures"][0]["error_code"], "sdk_no_output")

    def test_reports_in_different_directories_can_publish_concurrently(self):
        jobs = [self.job(f"event-{i}", "photo.insp") for i in range(2)]
        self.assertEqual(self.reports.begin_scan(jobs), {})
        publishing = threading.Barrier(2)
        publish = self.reports._publish

        def overlapping_publish(*args, **kwargs):
            publishing.wait(timeout=10)
            return publish(*args, **kwargs)

        with patch.object(self.reports, "_publish", side_effect=overlapping_publish):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda job: self.reports.write(job, "success"), jobs))
        for job in jobs:
            self.assertTrue(self.reports.has(job, "success"))


class ConcurrentAuditTests(unittest.TestCase):
    def test_audit_records_remain_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()) as output:
            audit = Audit(Path(temporary))
            self.addCleanup(audit.close)
            ready = threading.Barrier(3)

            def emit(worker):
                ready.wait(timeout=10)
                for sequence in range(20):
                    audit.emit("worker.event", worker=worker, sequence=sequence, text="审计" * 1024)

            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(emit, range(3)))
                list(pool.map(lambda _: audit.close(), range(3)))
            text = audit.path.read_text(encoding="utf-8")
            self.assertEqual(text, output.getvalue())
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(len(records), 60)
            self.assertEqual({(r["worker"], r["sequence"]) for r in records},
                             {(worker, sequence) for worker in range(3) for sequence in range(20)})
            self.assertTrue(all(record["text"] == "审计" * 1024 for record in records))

    def test_shared_runner_keeps_captured_outputs_and_pid_attribution_separate(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            audit = Audit(Path(temporary))
            self.addCleanup(audit.close)
            runner = Runner(audit, threading.Event(), 10)

            def run(worker):
                return runner.run([sys.executable, "-c", f"print('job-{worker}')"], capture=True)

            with ThreadPoolExecutor(max_workers=3) as pool:
                self.assertEqual(list(pool.map(run, range(3))), [f"job-{worker}\n" for worker in range(3)])
            audit.close()
            records = [json.loads(line) for line in audit.path.read_text().splitlines()]
            spawned = {record["pid"]: record["argv"] for record in records if record["event"] == "command.spawn"}
            self.assertEqual(len(spawned), 3)
            self.assertEqual({record["pid"] for record in records if record["event"] == "command.finish"},
                             set(spawned))
            for record in records:
                if record["event"] == "command.output":
                    self.assertIn(record["pid"], spawned)
                    self.assertIn(record["text"].strip(), spawned[record["pid"]][-1])


if __name__ == "__main__":
    unittest.main()
