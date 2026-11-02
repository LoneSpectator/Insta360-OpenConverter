import argparse
from pathlib import Path
import signal
import sys
import threading
import traceback

from .audit import Audit, Cancelled, Runner
from .config import Config
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description="Insta360 recursive offline preview converter")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--config", type=Path, help="Default: <data-dir>/config.toml")
    parser.add_argument("command", choices=["watch", "once", "doctor"], nargs="?", default="watch")
    args = parser.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    audit = Audit(args.data_dir)
    service = None
    stop = threading.Event()
    previous_signals = {}

    def shutdown(signum, frame):
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_signals[signum] = signal.signal(signum, shutdown)
    try:
        config_path = args.config or args.data_dir / "config.toml"
        cfg = Config.load(config_path, args.data_dir)
        audit.emit("service.start", command=args.command, config_path=config_path,
                   config_fingerprint=cfg.fingerprint(), mode=cfg.mode)
        runner = Runner(audit, stop, cfg.command_timeout)
        service = Service(cfg, audit, runner)
        service.doctor()
        if args.command == "doctor":
            return 0
        while not stop.is_set():
            try:
                counts = service.scan_once()
                if args.command == "once":
                    return 1 if counts["failed"] or counts["deferred"] else 0
            except Cancelled:
                raise
            except Exception as error:
                audit.emit("scan.failed", error=str(error), error_type=type(error).__name__,
                           traceback=traceback.format_exc())
                if args.command == "once":
                    return 1
            stop.wait(cfg.scan_interval)
        return 0
    except Cancelled as error:
        audit.emit("service.cancelled", reason=str(error))
        return 130
    except Exception as error:
        audit.emit("service.failed", error=str(error), error_type=type(error).__name__,
                   traceback=traceback.format_exc())
        return 1
    finally:
        if service:
            service.close()
        audit.emit("service.stop")
        audit.close()
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
