import argparse
from pathlib import Path
import signal
import sys
import threading
import traceback

from .audit import Audit, Cancelled, Runner
from .bootstrap import prepare_sdks
from .config import Config
from .media import capture_fields
from .scan import digest, snapshot
from .scheduler import watch_scans
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description="Insta360 recursive offline preview converter")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--config", type=Path, help="Default: <data-dir>/config.toml")
    parser.add_argument("command", choices=["watch", "once", "doctor", "metadata"], nargs="?", default="watch")
    parser.add_argument("source", type=Path, nargs="?", help="Source file for the metadata command")
    args = parser.parse_args(argv)
    if (args.command == "metadata") != (args.source is not None):
        parser.error("metadata requires a source file; other commands do not accept one")
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
                   config_fingerprint=cfg.fingerprint(), mode="gpu")
        runner = Runner(audit, stop, cfg.command_timeout)
        # Keep the operation lock for SDK preparation and every SDK user.
        # A second command must not replace a runtime used by an active watch.
        service = Service(cfg, audit, runner)
        previous_fingerprint = cfg.fingerprint()
        previous_runtime = cfg.managed_runtime_identity()
        prepare_sdks(cfg, audit, runner, metadata_only=args.command == "metadata")
        current_fingerprint = cfg.fingerprint()
        if (previous_runtime is not None and previous_runtime == cfg.managed_runtime_identity()
                and previous_fingerprint != current_fingerprint):
            service.state.register_config_alias(previous_fingerprint, current_fingerprint)
            audit.emit("config.fingerprint_compatible", previous=previous_fingerprint,
                       current=current_fingerprint, reason="SDK runtime content is unchanged")
        if args.command == "metadata":
            source = args.source.absolute()
            before = snapshot(source)
            source_sha = digest(source, audit, stop)
            media = service.media
            media.metadata_sdk.doctor()
            metadata = media.source_metadata(source, before=before, source_sha256=source_sha)
            output = metadata["Insta360SDK"]["snapshot_file"]
            audit.emit("metadata.inspect.complete", source=source, source_sha256=source_sha,
                       snapshot=output, capture_fields=capture_fields(metadata))
            return 0
        service.doctor()
        if args.command == "doctor":
            return 0
        if args.command == "once":
            try:
                counts = service.scan_once()
                return 1 if counts["failed"] or counts["deferred"] else 0
            except Cancelled:
                raise
            except Exception as error:
                audit.emit("scan.failed", error=str(error), error_type=type(error).__name__,
                           traceback=traceback.format_exc())
                return 1
        watch_scans(service, stop, audit, cfg.scan_interval_minutes)
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
