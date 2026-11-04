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
from .scan import EVENT, digest, scan, snapshot
from .state import State


REPORT_HEADER = "Insta360 OpenConverter report v1\n"


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
        self.media = Media(cfg, runner, audit)

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
        self.audit.emit("doctor.passed", mode=cfg.mode, archive=cfg.photo_root,
                        note="Dependency check only; real stitching also needs a camera sample")

    def report(self, job, status, **detail):
        self.check_reports(job)
        event = next((p.name for p in job.primary.parents if EVENT.match(p.name)), None)
        text = REPORT_HEADER + (
            "本文件说明预览处理情况；原始文件未删除或改写。\n"
            "成功时生成 2:1 等距柱状全景，JPG 使用 GPano，MP4 使用 GSpherical 标记。\n"
            "仅复制目标格式支持的元数据；专有防抖/逐帧/GPS轨迹等不保证嵌入预览。\n"
            "官方 InsMetaDataSDK 读取的相机、GPS、陀螺仪、曝光及延时数据保存在 /data/metadata。\n"
            "拍摄日期由标准元数据补充，陀螺仪时间基准不作为拍摄日期。\n"
            "完整原始元数据仍在原片中。\n\n"
        ) + json.dumps({"status": status, "time": datetime.now().astimezone().isoformat(),
                        "run_id": self.audit.run_id, "sources": job.sources, "preview": job.output,
                        "event": event, **detail}, ensure_ascii=False, indent=4, default=str) + "\n"
        for path in job.reports:
            self.audit.emit("file.write_report", path=path, status=status)
            atomic_text(path, text)

    def check_reports(self, job):
        for path in job.reports:
            if path.is_symlink():
                raise RuntimeError(f"Refusing to replace report symlink: {path}")
            if path.exists():
                with path.open(encoding="utf-8") as file:
                    if file.readline() != REPORT_HEADER:
                        raise RuntimeError(f"Refusing to overwrite an unrelated text file: {path}")

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
        hashes, metadata_paths, original_metadata = [], [], []
        metadata_dir = cfg.data_root / "metadata"
        metadata_dir.mkdir(exist_ok=True)
        for index, source in enumerate(job.sources):
            hashes.append({"path": str(source), "sha256": digest(source, self.audit)})
            metadata = self.media.source_metadata(source, f"{fingerprint}-{index}")
            original_metadata.append(metadata)
            target = metadata_dir / f"{fingerprint}-{index}.json"
            self.audit.emit("file.write_metadata_snapshot", source=source, destination=target)
            atomic_text(target, json.dumps(metadata, ensure_ascii=False, indent=4) + "\n")
            metadata_paths.append(str(target))
        source_probe = self.media.probe(job.primary) if job.output.suffix == ".mp4" else None
        directory = Path(tempfile.mkdtemp(prefix=".insta360-preview-", dir=job.primary.parent))
        self.audit.emit("file.create_temporary_directory", path=directory)
        try:
            staged = directory / job.output.name
            self.media.convert(job, staged)
            self.media.validate(staged, source_probe)
            capture = self.media.preserve_metadata(job.primary, staged, original_metadata[0])
            self.media.validate(staged, source_probe)
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
                      "capture_fields": capture, "preview_sha256": sha, "output_info": output_info}
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
            try:
                fingerprint, sources = job.fingerprint(config_fingerprint)
                old = self.state.get(job.primary)
                if job.blocked or any(info["size"] == 0 or time.time_ns() - info["mtime_ns"] <
                                      self.cfg.settle_seconds * 1_000_000_000 for info in sources):
                    reason = job.blocked or "Source is empty or still settling after upload"
                    counts["deferred"] += 1
                    self.audit.emit("job.deferred", sources=job.sources, reason=reason)
                    # Avoid rewriting explanatory text on every polling cycle.
                    if not old or old["fingerprint"] != fingerprint or old["status"] != "deferred":
                        self.report(job, "deferred", reason=reason)
                        self.state.put(job.primary, fingerprint, "deferred", reason=reason)
                    continue
                if old and old["fingerprint"] == fingerprint:
                    if old["status"] == "success":
                        detail = json.loads(old["detail"])
                        if job.output.exists() and not job.output.is_symlink() and (
                                snapshot(job.output) == detail.get("output_info")):
                            # Re-create an accidentally removed explanatory report.
                            if any(not path.exists() for path in job.reports):
                                self.report(job, "success", **detail)
                            self.audit.emit("job.skip_unchanged", source=job.primary, preview=job.output)
                            counts["skipped"] += 1
                            continue
                    if old["status"] == "failed" and time.time() - old["updated"] < self.cfg.retry_seconds:
                        counts["failed"] += 1
                        self.audit.emit("job.retry_delayed", source=job.primary, last_error=json.loads(old["detail"]))
                        continue
                self.process(job, fingerprint, sources)
                counts["success"] += 1
            except Cancelled:
                raise
            except Exception as error:
                counts["failed"] += 1
                self.audit.emit("job.failed", source=job.primary, error=str(error),
                                error_type=type(error).__name__, traceback=traceback.format_exc())
                self.state.put(job.primary, fingerprint, "failed", error=str(error))
                try:
                    self.report(job, "failed", error=str(error))
                except Exception as report_error:
                    self.audit.emit("report.failed", path=job.report, error=str(report_error))
        self.audit.emit("scan.finish", **counts)
        atomic_text(self.cfg.data_root / "health.json", json.dumps({
            "last_scan": datetime.now().astimezone().isoformat(), "run_id": self.audit.run_id,
            "status": "degraded" if counts["failed"] else "ok", **counts}, indent=4) + "\n")
        return counts
