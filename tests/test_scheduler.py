"""Minute-based scheduling with a fake clock; no real timer sleeps."""
from pathlib import Path
import tempfile
import unittest

from converter.audit import Cancelled
from converter.config import Config
from converter.scheduler import watch_scans


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, event, **detail):
        self.events.append((event, detail))


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


class Stop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.waits = []
        self.stop_when_waiting = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.waits.append(delay)
        if delay > 0 and self.stop_when_waiting:
            self.set()
        if self.stopped:
            return True
        self.clock.now += delay
        return False


class Service:
    def __init__(self, clock, stop, actions):
        self.clock, self.stop, self.actions = clock, stop, list(actions)
        self.started = []
        self.active = False

    def scan_once(self):
        if self.active:
            raise AssertionError("Scans must not overlap")
        self.active = True
        self.started.append(self.clock())
        if not self.actions:
            raise AssertionError("Scheduler started an unexpected catch-up scan")
        duration, error = self.actions.pop(0)
        self.clock.now += duration
        self.active = False
        if not self.actions:
            self.stop.set()
        if error is not None:
            raise error
        return {"success": 0, "failed": 0, "deferred": 0, "skipped": 0}


class SchedulerConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "config.toml"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return Config.load(self.path, self.root / "data")

    def test_default_scan_interval_is_sixty_minutes(self):
        self.assertEqual(Config().scan_interval_minutes, 60)
        self.assertEqual(self.load("").scan_interval_minutes, 60)

    def test_scan_interval_must_be_a_positive_integer_number_of_minutes(self):
        for value in ("0", "-1", "1.5", "true", '"60"'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "scan_interval_minutes"):
                self.load("scan_interval_minutes = " + value)
        self.assertEqual(self.load("scan_interval_minutes = 15").scan_interval_minutes, 15)

    def test_old_scan_interval_key_is_a_minute_alias_and_conflicting_keys_fail(self):
        self.assertEqual(self.load("scan_interval = 60").scan_interval_minutes, 60)
        with self.assertRaisesRegex(ValueError, "legacy alias"):
            self.load("scan_interval = 1\nscan_interval_minutes = 60")

    def test_scheduling_changes_and_old_retry_setting_do_not_invalidate_previews(self):
        original = self.load("scan_interval = 60\nretry_seconds = 300")
        changed = self.load("scan_interval_minutes = 5\nretry_seconds = 1")
        self.assertEqual(original.fingerprint(), changed.fingerprint())
        self.assertFalse(hasattr(changed, "retry_seconds"))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stop = Stop(self.clock)
        self.audit = Audit()

    def run_scans(self, actions, interval=1):
        service = Service(self.clock, self.stop, actions)
        watch_scans(service, self.stop, self.audit, interval, clock=self.clock)
        return service

    def test_startup_scan_is_immediate_then_default_hourly_tick(self):
        self.clock.now = 37
        service = self.run_scans([(5, None), (0, None)], Config().scan_interval_minutes)
        self.assertEqual(service.started, [37, 3637])
        self.assertEqual(self.stop.waits, [0, 3595])

    def test_slow_scan_drops_busy_ticks_and_waits_for_next_future_tick(self):
        service = self.run_scans([(130, None), (0, None)])
        self.assertEqual(service.started, [0, 180])
        self.assertEqual(self.stop.waits, [0, 50])
        skipped = [detail for event, detail in self.audit.events if event == "scan.skipped_busy"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["skipped_ticks"], 2)
        self.assertEqual(skipped[0]["first_skipped_monotonic"], 60)
        self.assertEqual(skipped[0]["next_scheduled_monotonic"], 180)

    def test_scan_finishing_exactly_on_tick_can_start_at_that_idle_tick(self):
        for duration, expected_skips in ((60, 0), (120, 1)):
            with self.subTest(duration=duration):
                clock = Clock()
                stop = Stop(clock)
                audit = Audit()
                service = Service(clock, stop, [(duration, None), (0, None)])
                watch_scans(service, stop, audit, 1, clock=clock)
                self.assertEqual(service.started, [0, duration])
                skipped = [detail["skipped_ticks"] for event, detail in audit.events
                           if event == "scan.skipped_busy"]
                self.assertEqual(sum(skipped), expected_skips)

    def test_failed_scan_waits_for_next_tick_and_does_not_spin(self):
        service = self.run_scans([(2, RuntimeError("Scan failed")), (0, None)])
        self.assertEqual(service.started, [0, 60])
        self.assertEqual(self.stop.waits, [0, 58])
        failed = [detail for event, detail in self.audit.events if event == "scan.failed"]
        self.assertEqual(failed[0]["error"], "Scan failed")
        self.assertEqual(failed[0]["error_type"], "RuntimeError")

    def test_slow_failed_scan_also_drops_busy_ticks(self):
        service = self.run_scans([(130, RuntimeError("Scan failed")), (0, None)])
        self.assertEqual(service.started, [0, 180])
        self.assertTrue(any(event == "scan.skipped_busy" for event, _ in self.audit.events))

    def test_shutdown_interrupts_wait_and_prevents_a_new_scan(self):
        self.stop.stop_when_waiting = True
        service = Service(self.clock, self.stop, [(5, None), (0, None)])
        watch_scans(service, self.stop, self.audit, 60, clock=self.clock)
        self.assertEqual(service.started, [0])
        self.assertEqual(self.clock.now, 5)

    def test_cancelled_scan_propagates_to_normal_shutdown_handler(self):
        service = Service(self.clock, self.stop, [(2, Cancelled("Service stopping"))])
        with self.assertRaises(Cancelled):
            watch_scans(service, self.stop, self.audit, 60, clock=self.clock)
        self.assertEqual(service.started, [0])

    def test_already_stopped_service_does_not_scan(self):
        self.stop.set()
        service = self.run_scans([])
        self.assertFalse(service.started)
        self.assertFalse(self.stop.waits)


if __name__ == "__main__":
    unittest.main()
