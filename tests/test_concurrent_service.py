"""Bounded jobs must overlap without losing reports or surviving shutdown."""

import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from converter.audit import Cancelled, Runner
from converter.config import Config
from converter.reports import REPORT_NAME
from converter.scan import Job
from converter.service import Service
from converter.sidecar import metadata_path
from converter.workspace import TEMP_ROOT_NAME


CHILD = r'''
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

control, key, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])

def change(start):
    with (control / "counter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = control / "counter.json"
        counts = json.loads(path.read_text())
        counts["active"] += 1 if start else -1
        counts["maximum"] = max(counts["maximum"], counts["active"])
        record = {"key": key, "pid": os.getpid(), "cwd": os.getcwd(), "output": str(output)}
        counts["started" if start else "finished"].append(record)
        path.write_text(json.dumps(counts))

def shutdown(number, frame):
    raise SystemExit(128 + number)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
change(True)
try:
    temporary = control / (key + ".ready.tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}))
    temporary.replace(control / (key + ".ready"))
    deadline = time.monotonic() + 20
    while not (control / (key + ".release")).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Test controller did not release the SDK child")
        time.sleep(0.01)
    if (control / (key + ".fail")).exists():
        print("controlled SDK failure for " + key, file=sys.stderr)
        raise SystemExit(7)
    output.write_bytes(("rendered preview " + key).encode())
finally:
    change(False)
'''


class MemoryAudit:
    run_id = "concurrent-service-test"

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event, **fields):
        with self.lock:
            self.events.append((event, fields))


class ControlledMedia:
    """Keep production publication/reporting and use real controllable children."""

    def __init__(self, runner, control, child):
        self.runner, self.control, self.child = runner, control, child

    def source_metadata(self, source, **kwargs):
        target = metadata_path(source)
        target.write_text("{}\n", encoding="utf-8")
        return {"Insta360SDK": {"snapshot_file": str(target)}}

    def probe(self, path):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "mjpeg",
                             "width": 128, "height": 64, "pix_fmt": "yuvj420p"}], "format": {}}

    def convert(self, job, output, plan):
        self.runner.run([sys.executable, self.child, self.control, job.primary.stem, output],
                        cwd=output.parent)

    def validate(self, path, source_probe, plan):
        return self.probe(path)

    def preserve_metadata(self, source, output, original, plan):
        return {}


class ConcurrentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.photo, self.data, self.control = (self.root / name for name in ("photo", "data", "control"))
        for path in (self.photo, self.data, self.control):
            path.mkdir()
        self.event = self.photo / "20261107.并发 空格目录"
        self.event.mkdir()
        (self.control / "counter.json").write_text(json.dumps({
            "active": 0, "maximum": 0, "started": [], "finished": [],
        }))
        self.child = self.root / "controlled_sdk.py"
        self.child.write_text(CHILD, encoding="utf-8")
        self.audit, self.stop = MemoryAudit(), threading.Event()
        self.cfg = Config(photo_root=self.photo, data_root=self.data, settle_seconds=0,
                          command_timeout=30, max_concurrent_jobs=3)
        self.service = Service(self.cfg, self.audit, Runner(self.audit, self.stop, 30))
        self.service.media = ControlledMedia(self.service.runner, self.control, self.child)
        self.threads = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(15)
        self.service.close()
        self.temporary.cleanup()

    def sources(self, count):
        result = []
        for index in range(count):
            source = self.event / f"IMG_{index:03}.insp"
            source.write_bytes(("original " + str(index)).encode())
            result.append(source)
        return result

    def stats(self):
        with (self.control / "counter.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return json.loads((self.control / "counter.json").read_text())

    def start_scan(self):
        result = {}

        def scan():
            try:
                result["counts"] = self.service.scan_once()
            except BaseException as error:
                result["error"] = error

        thread = threading.Thread(target=scan, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, result

    def wait_ready(self, count, scan):
        thread, result = scan
        deadline = time.monotonic() + 10
        while len(list(self.control.glob("*.ready"))) < count:
            if not thread.is_alive():
                if "error" in result:
                    raise result["error"]
                self.fail(f"Scan ended before {count} SDK children started: {result}")
            if time.monotonic() > deadline:
                self.fail(f"Timed out waiting for {count} SDK children; stats={self.stats()}")
            time.sleep(0.01)

    def release(self, sources):
        for source in sources:
            (self.control / (source.stem + ".release")).touch()

    def finish(self, scan, cancelled=False):
        thread, result = scan
        thread.join(15)
        self.assertFalse(thread.is_alive(), "Scan failed to finish or stop its active children")
        if cancelled:
            self.assertIsInstance(result.get("error"), Cancelled)
            return
        if "error" in result:
            raise result["error"]
        return result["counts"]

    def report(self):
        return json.loads((self.event / REPORT_NAME).read_text(encoding="utf-8"))

    def test_three_children_overlap_and_fourth_waits_with_complete_directory_progress(self):
        sources = self.sources(4)
        scan = self.start_scan()
        self.wait_ready(3, scan)
        stats = self.stats()
        self.assertEqual(stats["active"], 3)
        self.assertEqual(stats["maximum"], 3)
        self.assertEqual({item["key"] for item in stats["started"]}, {p.stem for p in sources[:3]})
        self.assertFalse((self.control / (sources[3].stem + ".ready")).exists())
        directories = {Path(item["cwd"]) for item in stats["started"]}
        self.assertEqual(len(directories), 3)
        self.assertTrue(all(path.parent == self.event / TEMP_ROOT_NAME for path in directories))
        report = self.report()
        self.assertEqual(report["summary"]["processing"], 3)
        self.assertEqual(report["summary"]["pending"], 1)
        self.assertEqual(set(report["files"]), {p.name for p in sources})

        self.release(sources[:1])
        self.wait_ready(4, scan)
        self.assertEqual(self.stats()["active"], 3)
        self.release(sources)
        self.assertEqual(self.finish(scan), {"success": 4, "failed": 0, "skipped": 0, "deferred": 0})
        self.assertEqual(self.stats()["maximum"], 3)
        self.assertEqual(self.stats()["active"], 0)
        self.assertEqual(self.report()["summary"]["success"], 4)
        self.assertTrue(all(self.service.state.get(source)["status"] == "success" for source in sources))
        self.assertFalse((self.event / TEMP_ROOT_NAME).exists())
        health = json.loads((self.data / "health.json").read_text())
        self.assertEqual((health["status"], health["success"]), ("ok", 4))

    def test_one_failure_does_not_stop_peers_and_retry_skips_completed_previews(self):
        sources = self.sources(4)
        failure = self.control / (sources[0].stem + ".fail")
        failure.touch()
        self.release(sources)
        counts = self.finish(self.start_scan())
        self.assertEqual(counts, {"success": 3, "failed": 1, "skipped": 0, "deferred": 0})
        report = self.report()
        self.assertEqual((report["summary"]["success"], report["summary"]["failed"]), (3, 1))
        self.assertEqual(report["failures"][0]["file"], sources[0].name)
        self.assertFalse(Job((sources[0],)).output.exists())
        self.assertTrue(all(Job((source,)).output.exists() for source in sources[1:]))
        self.assertFalse((self.event / TEMP_ROOT_NAME).exists())

        before = {source: Job((source,)).output.read_bytes() for source in sources[1:]}
        failure.unlink()
        self.assertEqual(self.finish(self.start_scan()), {
            "success": 1, "failed": 0, "skipped": 3, "deferred": 0,
        })
        started = [item["key"] for item in self.stats()["started"]]
        self.assertEqual(started.count(sources[0].stem), 2)
        for source in sources[1:]:
            self.assertEqual(started.count(source.stem), 1)
            self.assertEqual(Job((source,)).output.read_bytes(), before[source])
        self.assertEqual(self.report()["summary"]["success"], 4)
        self.assertEqual(self.report()["failures"], [])

    def test_shutdown_stops_all_active_children_preserves_queue_and_can_resume(self):
        sources = self.sources(6)
        scan = self.start_scan()
        self.wait_ready(3, scan)
        pids = [item["pid"] for item in self.stats()["started"]]
        self.stop.set()
        self.finish(scan, cancelled=True)
        self.assertEqual(len(self.stats()["started"]), 3)
        self.assertEqual(self.stats()["active"], 0)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        terminated = {detail["pid"] for event, detail in self.audit.events if event == "command.terminate"}
        self.assertEqual(terminated, set(pids))
        self.assertTrue(all(not Job((source,)).output.exists() for source in sources))
        self.assertFalse((self.event / TEMP_ROOT_NAME).exists())
        self.assertEqual(self.report()["summary"]["pending"], 3)

        self.stop.clear()
        self.release(sources)
        self.assertEqual(self.finish(self.start_scan()), {
            "success": 6, "failed": 0, "skipped": 0, "deferred": 0,
        })
        self.assertEqual(self.report()["summary"]["success"], 6)
        self.assertEqual(self.report()["summary"]["processing"], 0)

    def test_configured_single_worker_stays_serial(self):
        sources = self.sources(4)
        self.cfg.max_concurrent_jobs = 1
        scan = self.start_scan()
        self.wait_ready(1, scan)
        self.assertEqual(self.stats()["active"], 1)
        self.assertEqual(self.report()["summary"]["pending"], 3)
        self.release(sources)
        self.assertEqual(self.finish(scan)["success"], 4)
        self.assertEqual(self.stats()["maximum"], 1)


if __name__ == "__main__":
    unittest.main()
