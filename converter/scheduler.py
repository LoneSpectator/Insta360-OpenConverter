"""Run scans serially at fixed intervals, dropping ticks spent processing."""
import math
import time
import traceback

from .audit import Cancelled


def watch_scans(service, stop, audit, interval_minutes, *, clock=time.monotonic):
    """Scan immediately, then only at idle scheduled ticks; never catch up."""
    interval = interval_minutes * 60
    next_due = clock()
    while not stop.is_set():
        if stop.wait(max(0, next_due - clock())) or stop.is_set():
            return
        audit.emit("scan.scheduled", scheduled_monotonic=next_due,
                   started_monotonic=clock(), interval_minutes=interval_minutes)
        try:
            service.scan_once()
        except Cancelled:
            raise
        except Exception as error:
            audit.emit("scan.failed", error=str(error), error_type=type(error).__name__,
                       traceback=traceback.format_exc())
        next_due += interval
        finished = clock()
        # A scan finishing exactly at the next tick is already idle at that
        # tick. Only earlier scheduled ticks are dropped as busy.
        if finished > next_due:
            skipped = math.ceil((finished - next_due) / interval)
            first_skipped = next_due
            next_due += skipped * interval
            audit.emit("scan.skipped_busy", skipped_ticks=skipped,
                       first_skipped_monotonic=first_skipped,
                       finished_monotonic=finished, next_scheduled_monotonic=next_due)
        audit.emit("scan.next_scheduled", scheduled_monotonic=next_due,
                   in_seconds=max(0, next_due - clock()))
