import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import threading
import traceback
import uuid

from .audit import Audit, Cancelled, Runner
from .config import Config
from .media import Media, capture_fields
from .scan import digest, snapshot
from .service import Service, atomic_text


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
                   config_fingerprint=cfg.fingerprint(), mode=cfg.mode)
        runner = Runner(audit, stop, cfg.command_timeout)
        if args.command == "metadata":
            source = args.source.absolute()
            before = snapshot(source)
            source_sha = digest(source, audit)
            token = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
            media = Media(cfg, runner, audit)
            media.metadata_sdk.doctor()
            metadata = media.source_metadata(source, token + "-inspect")
            if snapshot(source) != before:
                raise RuntimeError("Source changed during metadata extraction")
            output = cfg.data_root / "metadata" / f"{token}-inspect-{uuid.uuid4().hex}.json"
            audit.emit("file.write_metadata_snapshot", source=source, destination=output)
            atomic_text(output, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
            audit.emit("metadata.inspect.complete", source=source, source_sha256=source_sha,
                       snapshot=output, sdk_directory=metadata["Insta360SDK"]["snapshot_directory"],
                       capture_fields=capture_fields(metadata))
            return 0
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
