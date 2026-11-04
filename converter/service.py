from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import traceback

from .audit import Cancelled
from .media import Media
from .quality import build_plan, summarize
from .reports import DirectoryReports
from .scan import digest, scan, snapshot
from .sidecar import metadata_path
from .state import State


def atomic_text(path: Path, text: str):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".insta360-preview-",
                                     encoding="utf-8", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Service:
    def __init__(self, cfg, audit, runner):
        self.cfg, self.audit, self.runner = cfg, audit, runner
        self.lock = (cfg.data_root / "converter.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another converter is using this /data directory")
        self.state = State(cfg.data_root / "state.sqlite3")
        self.media = Media(cfg, runner, audit, state=self.state)
        self.reports = DirectoryReports(audit)

    def close(self):
        self.state.close()
        self.lock.close()

    def doctor(self):
        cfg = self.cfg
        if not cfg.photo_root.is_dir():
            raise ValueError(f"Archive directory does not exist: {cfg.photo_root}")
        if cfg.photo_root.is_symlink():
            raise ValueError("photo_root must not be a symlink")
        for name in (cfg.exiftool, cfg.ffprobe, cfg.executable, cfg.metadata_reader):
            if shutil.which(name) is None:
                raise ValueError(f"Required executable is missing or not executable: {name}")
        if not Path(cfg.executable).is_absolute():
            raise ValueError("sdk.executable must be an absolute path")
        if not Path(cfg.models).is_dir():
            raise ValueError(f"SDK models directory does not exist: {cfg.models}")
        self.runner.run([cfg.exiftool, "-ver"])
        self.runner.run([cfg.ffprobe, "-version"])
        self.media.metadata_sdk.doctor()
        # Run the actual SDK executable so unresolved .so files and GPU failures
        # are discovered before the daemon appears to be working.
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = ":".join(cfg.library_dirs + (
            [env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
        help_text = self.runner.run([cfg.executable, "-help"],
                                    cwd=cfg.data_root, env=env, capture=True,
                                    accepted_returncodes=(0, 1, 255))
        if "-inputs" not in help_text or "-output" not in help_text:
            raise ValueError("SDK command line did not report the expected -inputs / -output interface")
        self.media.sdk_help = help_text
        self.audit.emit("doctor.passed", mode="gpu", archive=cfg.photo_root,
                        note="Dependency check only; real stitching also needs a camera sample")

    def report(self, job, status, **detail):
        self.reports.write(job, status, **detail)

    def check_reports(self, job):
        self.reports.check(job)

    def extract_metadata(self, job, sources):
        hashes, paths, infos, originals = [], [], [], []
        for source, before in zip(job.sources, sources):
            sha = digest(source, self.audit)
            metadata = self.media.source_metadata(source, before=before, source_sha256=sha)
            target = metadata_path(source)
            hashes.append({"path": str(source), "sha256": sha})
            originals.append(metadata)
            paths.append(str(target))
            infos.append(snapshot(target))
        return hashes, paths, infos, originals

    def metadata_unchanged(self, job, detail):
        paths = [metadata_path(source) for source in job.sources]
        if detail.get("metadata_snapshots") != [str(path) for path in paths]:
            return False
        for path in paths:
            counterpart = path.with_suffix("")
            if counterpart.exists() or counterpart.is_symlink():
                return False
        try:
            return [snapshot(path) for path in paths] == detail.get("metadata_info")
        except (OSError, ValueError):
            return False

    def _owned(self, output):
        if output.is_symlink():
            raise RuntimeError(f"Preview path is a symlink: {output}")
        if output.exists():
            sha = digest(output, self.audit)
            if sha not in self.state.hashes(output):
                raise RuntimeError(f"Refusing to overwrite an unowned or externally changed preview: {output}")
            return snapshot(output)
        return None

    def process(self, job, fingerprint, sources):
        cfg = self.cfg
        self.audit.emit("job.start", sources=sources, output=job.output, fingerprint=fingerprint)
        # Check both destinations before invoking a potentially expensive SDK job.
        previous = self._owned(job.output)
        self.check_reports(job)
        hashes, metadata_paths, metadata_info, original_metadata = self.extract_metadata(job, sources)
        source_probes = [self.media.probe(path) for path in job.sources]
        plan = build_plan(cfg, source_probes, job.output.suffix == ".mp4")
        self.audit.emit("quality.plan", sources=[summarize(probe) for probe in source_probes], target=plan.record(),
                        notes=["Stitching requires reprojection and encoding; not lossless remuxing",
                               "JPEG quality and hidden SDK stitching-fusion defaults remain SDK-controlled"])
        source_probe = source_probes[0] if job.output.suffix == ".mp4" else None
        directory = Path(tempfile.mkdtemp(prefix=".insta360-preview-", dir=job.primary.parent))
        self.audit.emit("file.create_temporary_directory", path=directory)
        try:
            staged = directory / job.output.name
            self.media.convert(job, staged, plan)
            self.media.validate(staged, source_probe, plan)
            capture = self.media.preserve_metadata(job.primary, staged, original_metadata[0], plan)
            actual = self.media.validate(staged, source_probe, plan)
            quality = {"target": plan.record(), "sources": [summarize(probe) for probe in source_probes],
                       "actual": summarize(actual), "flowstate": cfg.flowstate,
                       "sdk_defaults": "JPEG quality and internal stitching fusion are SDK-controlled",
                       "encoding": "One SDK export; metadata changes do not re-encode pixels"}
            self.audit.emit("quality.verified", path=staged, **quality)
            if [snapshot(source) for source in job.sources] != sources:
                raise RuntimeError("Source changed during conversion; staged preview discarded")
            os.utime(staged, ns=(sources[0]["mtime_ns"], sources[0]["mtime_ns"]))
            self.audit.emit("file.set_mtime", path=staged, mtime_ns=sources[0]["mtime_ns"])
            sha = digest(staged, self.audit)
            with staged.open("rb") as file:
                os.fsync(file.fileno())
            if self.runner.stop.is_set():
                raise Cancelled("Shutdown requested before publication")
            current = self._owned(job.output)
            if current != previous:
                raise RuntimeError("Preview changed during conversion; refusing to overwrite")
            self.state.intend(job.output, sha)
            if previous is None:
                # Same-filesystem link fails atomically if somebody created the
                # destination after our check. No existing file is overwritten.
                self.audit.emit("file.publish_new", source=staged, destination=job.output, sha256=sha)
                os.link(staged, job.output)
                staged.unlink()
            else:
                self.audit.emit("file.replace_owned_preview", source=staged, destination=job.output, sha256=sha)
                os.replace(staged, job.output)
            fsync_dir(job.output.parent)
            self.state.published(job.output, sha)
            output_info = snapshot(job.output)
            detail = {"source_hashes": hashes, "metadata_snapshots": metadata_paths,
                      "metadata_info": metadata_info,
                      "capture_fields": capture, "preview_sha256": sha, "output_info": output_info,
                      "quality": quality}
            self.report(job, "success", **detail)
            self.state.put(job.primary, fingerprint, "success", **detail)
            for secondary in job.sources[1:]:
                self.state.put(secondary, fingerprint, "paired", primary=str(job.primary), output=str(job.output))
            self.audit.emit("job.success", output=job.output, **detail)
        finally:
            self.audit.emit("file.remove_temporary_directory", path=directory)
            shutil.rmtree(directory)

    def scan_once(self):
        if not self.cfg.photo_root.is_dir():
            raise ValueError(f"Archive mount is missing: {self.cfg.photo_root}")
        self.audit.emit("scan.start", root=self.cfg.photo_root)
        jobs = scan(self.cfg.photo_root, self.audit)
        counts = {"success": 0, "failed": 0, "skipped": 0, "deferred": 0}
        config_fingerprint = self.cfg.fingerprint()
        for job in jobs:
            if self.runner.stop.is_set():
                raise Cancelled("Shutdown requested between jobs")
            fingerprint = "unreadable"
            completed_detail = None
            try:
                fingerprint, sources = job.fingerprint(config_fingerprint)
                old = self.state.get(job.primary)
                if job.blocked or any(info["size"] == 0 or time.time_ns() - info["mtime_ns"] <
                                      self.cfg.settle_seconds * 1_000_000_000 for info in sources):
                    reason = job.blocked or "Source is empty or still settling after upload"
                    counts["deferred"] += 1
                    self.audit.emit("job.deferred", sources=job.sources, reason=reason)
                    # Avoid rewriting explanatory text on every polling cycle.
                    if (not old or old["fingerprint"] != fingerprint or old["status"] != "deferred"
                            or not self.reports.has(job, "deferred")):
                        self.report(job, "deferred", reason=reason)
                        self.state.put(job.primary, fingerprint, "deferred", reason=reason)
                    continue
                if old and old["fingerprint"] == fingerprint:
                    detail = json.loads(old["detail"])
                    if old["status"] == "failed":
                        completed_detail = detail.get("preview_detail")
                        if time.time() - old["updated"] < self.cfg.retry_seconds:
                            if not self.reports.has(job, "failed"):
                                self.report(job, "failed", error=detail.get("error"))
                            counts["failed"] += 1
                            self.audit.emit("job.retry_delayed", source=job.primary, last_error=detail.get("error"))
                            continue
                    elif old["status"] == "success":
                        completed_detail = detail
                    if completed_detail:
                        detail = dict(completed_detail)
                        if job.output.exists() and not job.output.is_symlink() and (
                                snapshot(job.output) == detail.get("output_info")):
                            self.check_reports(job)
                            repaired = not self.metadata_unchanged(job, detail)
                            if repaired:
                                hashes, paths, infos, _ = self.extract_metadata(job, sources)
                                if [snapshot(source) for source in job.sources] != sources:
                                    raise RuntimeError("Source changed while repairing metadata")
                                detail.update(source_hashes=hashes, metadata_snapshots=paths,
                                              metadata_info=infos)
                                self.audit.emit("job.metadata_repaired", sources=job.sources,
                                                metadata_snapshots=paths)
                            # Re-create an accidentally removed explanatory report.
                            if repaired or not self.reports.has(job, "success"):
                                self.report(job, "success", **detail)
                            if repaired or old["status"] != "success":
                                self.state.put(job.primary, fingerprint, "success", **detail)
                            self.audit.emit("job.skip_unchanged", source=job.primary, preview=job.output)
                            counts["skipped"] += 1
                            continue
                self.process(job, fingerprint, sources)
                counts["success"] += 1
            except Cancelled:
                raise
            except Exception as error:
                counts["failed"] += 1
                self.audit.emit("job.failed", source=job.primary, error=str(error),
                                error_type=type(error).__name__, traceback=traceback.format_exc())
                failure = {"error": str(error)}
                if completed_detail:
                    failure["preview_detail"] = completed_detail
                self.state.put(job.primary, fingerprint, "failed", **failure)
                try:
                    self.report(job, "failed", error=str(error))
                except Exception as report_error:
                    self.audit.emit("report.failed", path=job.report, error=str(report_error))
        self.audit.emit("scan.finish", **counts)
        atomic_text(self.cfg.data_root / "health.json", json.dumps({
            "last_scan": datetime.now().astimezone().isoformat(), "run_id": self.audit.run_id,
            "status": "degraded" if counts["failed"] else "ok", **counts}, indent=4) + "\n")
        return counts
