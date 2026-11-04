"""Keep one latest-status report inside each source directory."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import tempfile


REPORT_NAME = "insta360-preview-report.txt"
REPORT_HEADER = "Insta360 OpenConverter directory report v1\n"
DESCRIPTION = (
    "本文件汇总本目录内原文件的最新处理状态，每个文件对应一条记录。\n"
    "原片保持不变；预览使用 _preview 后缀，完整元数据保存在不含原扩展名的主名_metadata.json。\n"
    "双镜头原片分别列出状态，共用一份全景预览。\n"
    "status 为 success（成功）、failed（失败）或 deferred（等待处理）。\n\n"
)


def report_paths(job):
    """Reports live inside source parents, including separate parent directories."""
    return tuple(dict.fromkeys(source.parent / REPORT_NAME for source in job.sources))


def _snapshot(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"Refusing to replace a non-regular directory report: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _load(path):
    before = _snapshot(path)
    if before is None:
        return {"files": {}}, None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as file:
        text = file.read()
    if _snapshot(path) != before:
        raise RuntimeError(f"Directory report changed while reading: {path}")
    if not text.startswith(REPORT_HEADER):
        raise RuntimeError(f"Refusing to overwrite an unrelated text file: {path}")
    _, separator, document = text.partition("\n\n")
    try:
        data = json.loads(document) if separator else None
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Directory report contains invalid JSON: {path}") from error
    if (not isinstance(data, dict) or set(data) != {"files"}
            or not isinstance(data["files"], dict)
            or any(not isinstance(name, str) or not name or name in (".", "..")
                   or Path(name).name != name or not isinstance(entry, dict)
                   for name, entry in data["files"].items())):
        raise RuntimeError(f"Directory report has an unsupported format: {path}")
    return data, before


class DirectoryReports:
    def __init__(self, audit):
        self.audit = audit

    def check(self, job):
        for path in report_paths(job):
            _load(path)

    def has(self, job, status):
        """Every source needs its own current-status entry after report repair."""
        complete = True
        for path in report_paths(job):
            data, _ = _load(path)
            for source in job.sources:
                if source.parent == path.parent:
                    entry = data["files"].get(source.name)
                    if not entry or entry.get("status") != status:
                        complete = False
        return complete

    def write(self, job, status, **detail):
        now = datetime.now().astimezone().isoformat()
        for path in report_paths(job):
            data, before = _load(path)
            for source in job.sources:
                if source.parent != path.parent:
                    continue
                event = next((parent.name for parent in source.parents
                              if re.match(r"^\d{8}\..+", parent.name)), None)
                data["files"][source.name] = {
                    "status": status, "time": now, "run_id": self.audit.run_id,
                    "sources": job.sources, "preview": job.output, "event": event, **detail,
                }
            self.audit.emit("file.write_report", path=path, status=status,
                            files=[source.name for source in job.sources if source.parent == path.parent])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                                                 prefix=".insta360-preview-report-",
                                                 encoding="utf-8", delete=False) as file:
                    temporary = Path(file.name)
                    file.write(REPORT_HEADER + DESCRIPTION)
                    json.dump(data, file, ensure_ascii=False, indent=4, default=str)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                if _snapshot(path) != before:
                    raise RuntimeError(f"Directory report changed before publication: {path}")
                if before is None:
                    # A new unrelated file created after the check must survive.
                    os.link(temporary, path)
                    temporary.unlink()
                else:
                    os.replace(temporary, path)
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
