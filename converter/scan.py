from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


VIDEO = {".insv", ".lrv"}
PAIR = re.compile(r"^(.*)_(00|10)_(\d+)$", re.IGNORECASE)
EVENT = re.compile(r"^\d{8}\..+")


def snapshot(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    return {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino}


def digest(path, audit=None):
    if audit:
        audit.emit("file.read_hash", path=path)
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


@dataclass
class Job:
    sources: tuple[Path, ...]
    blocked: str = ""

    @property
    def primary(self):
        return self.sources[0]

    @property
    def output(self):
        extension = ".jpg" if self.primary.suffix.lower() == ".insp" else ".mp4"
        return self.primary.with_name(self.primary.stem + "_preview" + extension)

    @property
    def report(self):
        # Include the original extension to avoid INSP/INSV report collisions.
        return self.primary.with_name(self.primary.name + "_preview.txt")

    @property
    def reports(self):
        return tuple(path.with_name(path.name + "_preview.txt") for path in self.sources)

    def fingerprint(self, config_fingerprint):
        sources = [snapshot(path) for path in self.sources]
        value = json.dumps([sources, config_fingerprint], sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest(), sources


def scan(root: Path, audit):
    files = []

    def failed(error):
        # A missing archive mount must never look like a successful empty scan.
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        retained = []
        for name in sorted(dirs):
            path = base / name
            if path.is_symlink() or name.startswith(".insta360-preview-"):
                audit.emit("scan.skip_directory", path=path)
            else:
                retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            path = base / name
            if path.suffix.lower() not in VIDEO | {".insp"} or path.stem.lower().endswith("_preview"):
                continue
            if path.is_symlink():
                audit.emit("scan.skip_symlink", path=path)
                continue
            if path.is_file():
                files.append(path)
    pairs = {}
    for path in files:
        match = PAIR.match(path.stem)
        if path.suffix.lower() in VIDEO and match:
            key = (path.parent, match[1].lower(), match[3], path.suffix.lower())
            pairs.setdefault(key, {})[match[2]] = path
    consumed, jobs = set(), []
    for tracks in pairs.values():
        if "00" in tracks and "10" in tracks:
            jobs.append(Job((tracks["00"], tracks["10"])))
            consumed.update(tracks.values())
        elif "10" in tracks:
            path = tracks["10"]
            jobs.append(Job((path,), "Missing _00_ lens file; waiting for the matching source"))
            consumed.add(path)
    jobs.extend(Job((path,)) for path in files if path not in consumed)
    outputs = {}
    for job in jobs:
        outputs.setdefault(job.output, []).append(job)
    for output, owners in outputs.items():
        if len(owners) > 1:
            for job in owners:
                job.blocked = f"Multiple sources would write the same preview: {output}"
    return sorted(jobs, key=lambda job: str(job.primary))
