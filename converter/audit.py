from datetime import datetime
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time


class Cancelled(RuntimeError):
    pass


class Audit:
    def __init__(self, data_root: Path):
        logs = data_root / "log"
        logs.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z") + f"-{os.getpid()}"
        self.path = logs / (self.run_id + ".log")
        self.file = self.path.open("x", encoding="utf-8", buffering=1)

    def emit(self, event, **fields):
        record = {"time": datetime.now().astimezone().isoformat(), "run_id": self.run_id,
                  "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        self.file.write(line + "\n")
        print(line, file=sys.stdout, flush=True)

    def close(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


class Runner:
    """Drain stdout/stderr while enforcing timeouts and forwarding shutdown."""

    def __init__(self, audit, stop: threading.Event, timeout: int):
        self.audit, self.stop, self.timeout = audit, stop, timeout

    def run(self, argv, *, cwd=None, env=None, capture=False, accepted_returncodes=(0,)):
        if self.stop.is_set():
            raise Cancelled("Service is stopping")
        argv = [str(arg) for arg in argv]
        self.audit.emit("command.start", argv=argv, cwd=cwd, timeout=self.timeout)
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        started = time.monotonic()
        out = bytearray()
        tail = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                if self.stop.is_set():
                    raise Cancelled("Shutdown requested during command")
                if time.monotonic() - started > self.timeout:
                    raise TimeoutError(f"Command exceeded {self.timeout}s: {argv[0]}")
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    self.audit.emit("command.output", pid=process.pid, stream=key.data,
                                    text=chunk.decode("utf-8", errors="replace"))
                    tail.extend(chunk)
                    del tail[:-8192]
                    if capture and key.data == "stdout":
                        out.extend(chunk)
                        if len(out) > 64 * 1024 * 1024:
                            raise RuntimeError("Command JSON output exceeds 64 MiB")
            # A child can close its pipes and continue running; timeout still applies.
            while process.poll() is None:
                if self.stop.wait(0.1):
                    raise Cancelled("Shutdown requested during command")
                if time.monotonic() - started > self.timeout:
                    raise TimeoutError(f"Command exceeded {self.timeout}s: {argv[0]}")
            self.audit.emit("command.finish", pid=process.pid, returncode=process.returncode,
                            seconds=round(time.monotonic() - started, 3))
            if process.returncode not in accepted_returncodes:
                raise RuntimeError(f"{argv[0]} exited {process.returncode}: " +
                                   tail.decode("utf-8", errors="replace"))
            return out.decode("utf-8")
        finally:
            if process.poll() is None:
                self.audit.emit("command.terminate", pid=process.pid)
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            selector.close()
            process.stdout.close()
            process.stderr.close()
