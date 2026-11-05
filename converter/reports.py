"""Keep a machine-readable progress report inside each source directory."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

from .report_errors import explain_error, explain_reason
from .workspace import temporary_workspace


REPORT_NAME = "Insta360_OpenConverter_convert_report.txt"
REPORT_FORMAT = "Insta360_OpenConverter_convert_report"
LEGACY_REPORT_NAME = "insta360-preview-report.txt"
LEGACY_REPORT_HEADER = "Insta360 OpenConverter directory report v1\n"
STATUS_TEXT = {"failed": "失败", "processing": "处理中", "pending": "待处理",
               "deferred": "等待处理", "success": "成功"}


def report_paths(job):
    return tuple(dict.fromkeys(source.parent / REPORT_NAME for source in job.sources))


def _snapshot(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"Refusing to replace a non-regular directory report: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _empty():
    return {"format": REPORT_FORMAT, "schema_version": 1, "updated_at": None,
            "summary": {}, "failures": [], "files": {}}


def _validate_files(files, path):
    if (not isinstance(files, dict)
            or any(not isinstance(name, str) or not name or name in (".", "..")
                   or Path(name).name != name or not isinstance(entry, dict)
                   or entry.get("status") not in STATUS_TEXT
                   or ("present" in entry and type(entry["present"]) is not bool)
                   for name, entry in files.items())):
        raise RuntimeError(f"Directory report has an unsupported format: {path}")


def _read(path, before):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as file:
        text = file.read()
    if _snapshot(path) != before:
        raise RuntimeError(f"Directory report changed while reading: {path}")
    return text


def _legacy(path):
    if path.is_symlink() or not path.is_file():
        return None
    try:
        before = _snapshot(path)
        text = _read(path, before)
    except (OSError, RuntimeError, UnicodeError):
        return None
    if not text.startswith(LEGACY_REPORT_HEADER):
        return None
    _, separator, document = text.partition("\n\n")
    try:
        data = json.loads(document) if separator else None
        if not isinstance(data, dict) or set(data) != {"files"}:
            return None
        _validate_files(data["files"], path)
    except (ValueError, TypeError, RuntimeError):
        return None
    result = _empty()
    result["files"] = data["files"]
    return result, (path, before, deepcopy(data["files"]))


def _legacy_covered(files, previous):
    """Only remove a legacy copy when its processing details still survive."""
    missing = object()
    for name, entry in previous.items():
        current = files.get(name)
        if current is None:
            return False
        for key, value in entry.items():
            if key in ("status_text", "present"):
                continue
            if key in ("error", "reason") and current.get(key + "_raw", missing) == value:
                continue
            if current.get(key, missing) != value:
                return False
    return True


def _load(path, audit=None):
    before = _snapshot(path)
    if before is None:
        legacy = _legacy(path.with_name(LEGACY_REPORT_NAME))
        if legacy is not None:
            data, previous = legacy
            return data, None, previous
        return _empty(), None, None
    try:
        data = json.loads(_read(path, before))
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Refusing to overwrite an unrelated text file or invalid JSON report: {path}") from error
    if (not isinstance(data, dict) or data.get("format") != REPORT_FORMAT
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1):
        raise RuntimeError(f"Refusing to overwrite an unrelated text file: {path}")
    if (set(data) != {"format", "schema_version", "updated_at", "summary", "failures", "files"}
            or not isinstance(data["updated_at"], str)
            or not isinstance(data["summary"], dict) or not isinstance(data["failures"], list)):
        raise RuntimeError(f"Directory report has an unsupported format: {path}")
    _validate_files(data.get("files"), path)
    legacy = _legacy(path.with_name(LEGACY_REPORT_NAME))
    if legacy is not None:
        previous_data, previous = legacy
        if _legacy_covered(data["files"], previous_data["files"]):
            return data, before, previous
        if audit is not None:
            audit.emit("report.legacy_cleanup_skipped", path=previous[0],
                       reason="Legacy report contains processing details absent from the current report")
    return data, before, None


def _entry(job, source, status, now, run_id, **detail):
    event = next((parent.name for parent in source.parents
                  if re.match(r"^\d{8}\..+", parent.name)), None)
    return {"status": status, "status_text": STATUS_TEXT[status], "present": True,
            "time": now, "run_id": run_id, "sources": job.sources,
            "preview": job.output, "event": event, **detail}


def _refresh(data):
    counts = {"failed": 0, "total": 0, "processing": 0, "pending": 0,
              "deferred": 0, "success": 0}
    failures = []
    for name, entry in data["files"].items():
        status = entry["status"]
        entry["status_text"] = STATUS_TEXT[status]
        entry.setdefault("present", True)
        if status == "failed":
            entry.update(explain_error(entry.get("error_raw", entry.get("error", ""))))
        elif status == "deferred" and ("reason" in entry or "reason_raw" in entry):
            entry.update(explain_reason(entry.get("reason_raw", entry.get("reason", ""))))
        if not entry["present"]:
            continue
        counts["total"] += 1
        counts[status] += 1
        if status == "failed":
            failures.append({"file": name, "status_text": entry["status_text"],
                             **{key: entry[key] for key in ("error", "error_code", "time", "run_id")
                                if key in entry}})
    rank = {status: index for index, status in enumerate(STATUS_TEXT)}
    data["summary"] = counts
    data["failures"] = sorted(failures, key=lambda item: item["file"])
    data["files"] = dict(sorted(data["files"].items(),
                                key=lambda item: (rank[item[1]["status"]], item[0])))
    return {key: data[key] for key in ("format", "schema_version", "updated_at", "summary", "failures", "files")}


class DirectoryReports:
    def __init__(self, audit):
        self.audit = audit

    def check(self, job):
        for path in report_paths(job):
            _load(path)

    def has(self, job, status):
        complete = True
        for path in report_paths(job):
            data, _, _ = _load(path)
            for source in job.sources:
                if source.parent == path.parent:
                    entry = data["files"].get(source.name)
                    if not entry or entry.get("status") != status or not entry.get("present", True):
                        complete = False
        return complete

    def _publish(self, path, data, before, legacy, *, status, names):
        self.audit.emit("file.write_report", path=path, status=status, files=names)
        with temporary_workspace(path.parent, self.audit, kind="report") as temporary:
            staged = temporary / path.name
            self.audit.emit("file.write_report_temporary", path=staged, destination=path)
            with staged.open("x", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=4, default=str, allow_nan=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if _snapshot(path) != before:
                raise RuntimeError(f"Directory report changed before publication: {path}")
            if before is None:
                os.link(staged, path)
                staged.unlink()
            else:
                os.replace(staged, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if legacy is not None:
                old, previous, original_files = legacy
                try:
                    unchanged = (_legacy_covered(data["files"], original_files)
                                 and _snapshot(old) == previous)
                except (OSError, RuntimeError):
                    unchanged = False
                if unchanged:
                    self.audit.emit("file.remove_legacy_report", path=old, replacement=path)
                    old.unlink()
                    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                else:
                    self.audit.emit("report.legacy_cleanup_skipped", path=old,
                                    reason="Legacy report changed or contains details absent from the current report")

    def begin_scan(self, jobs, directories=()):
        """Register pending files before work, isolating conflicts by directory."""
        grouped = {Path(parent): {} for parent in directories}
        for job in jobs:
            for source in job.sources:
                grouped.setdefault(source.parent, {})[source.name] = (source, job)
        errors = {}
        now = datetime.now().astimezone().isoformat()
        for parent, current in grouped.items():
            path = parent / REPORT_NAME
            try:
                data, before, legacy = _load(path, self.audit)
                if not current and before is None and legacy is None:
                    continue
                previous = deepcopy(data)
                for name, entry in data["files"].items():
                    entry["present"] = name in current
                    if name in current and entry["status"] == "processing":
                        entry.update(status="pending", time=now, run_id=self.audit.run_id)
                for name, (source, job) in current.items():
                    if name not in data["files"]:
                        data["files"][name] = _entry(job, source, "pending", now, self.audit.run_id)
                data = _refresh(data)
                if data != previous or legacy is not None:
                    data["updated_at"] = now
                    self._publish(path, data, before, legacy, status="scan", names=list(current))
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                errors[parent] = str(error)
                self.audit.emit("report.scan_failed", path=path, error=str(error))
        return errors

    def write(self, job, status, **detail):
        if status not in STATUS_TEXT:
            raise ValueError(f"Unknown report status: {status}")
        now = datetime.now().astimezone().isoformat()
        for path in report_paths(job):
            data, before, legacy = _load(path, self.audit)
            for source in job.sources:
                if source.parent == path.parent:
                    data["files"][source.name] = _entry(job, source, status, now, self.audit.run_id, **detail)
            data = _refresh(data)
            data["updated_at"] = now
            self._publish(path, data, before, legacy, status=status,
                          names=[source.name for source in job.sources if source.parent == path.parent])
