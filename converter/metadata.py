"""Official SDK extraction and validation of temporary sensor tracks."""
import hashlib
import json
import math
import os
from pathlib import Path


TRACKS = ("gps", "gyro", "exposure", "timelapse")


def coordinate(value, hemisphere, positive, negative, limit):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or abs(value) > limit:
        return None
    if hemisphere == negative:
        return -abs(value)
    if hemisphere == positive and value >= 0:
        return value
    return None


class MetadataSDK:
    def __init__(self, cfg, runner, audit):
        self.cfg, self.runner, self.audit = cfg, runner, audit

    def environment(self):
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = ":".join(self.cfg.metadata_library_dirs + (
            [env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
        return env

    def doctor(self):
        schema = json.loads(self.runner.run([self.cfg.metadata_reader, "--schema"],
                                          env=self.environment(), capture=True))
        if schema != {"reader": "insta360-metadata", "schema_version": 1, "sdk": "InsMetaDataSDK"}:
            raise ValueError("Metadata adapter has an unsupported protocol")
        self.audit.emit("metadata_sdk.ready", reader=self.cfg.metadata_reader, schema=schema)

    def extract(self, source: Path, directory: Path):
        self.audit.emit("metadata_sdk.parse", source=source, destination=directory)
        self.runner.run([self.cfg.metadata_reader, source, directory], env=self.environment(), cwd=directory)
        summary_path = directory / "sdk.json"
        if summary_path.is_symlink() or not summary_path.is_file() or summary_path.stat().st_size > 1024 * 1024:
            raise ValueError("Metadata SDK did not produce a valid summary file")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("schema_version") != 1 or summary.get("sdk") != "InsMetaDataSDK":
            raise ValueError("Metadata SDK summary has an unsupported schema")
        for field in ("camera_type", "firmware_version", "serial_number"):
            if not isinstance(summary.get(field), str):
                raise ValueError(f"Invalid SDK field: {field}")
        if type(summary.get("gyro_first_timestamp_ms")) is not int:
            raise ValueError("Invalid SDK gyro time origin")
        tracks = summary.get("tracks", {})
        if set(tracks) != set(TRACKS):
            raise ValueError("Metadata SDK did not report all track statuses")
        for name in TRACKS:
            description = tracks[name]
            if (type(description.get("available")) is not bool or
                    type(description.get("count")) is not int or description["count"] < 0 or
                    description.get("file") != name + ".jsonl"):
                raise ValueError(f"Invalid SDK track descriptor: {name}")
            path = directory / description["file"]
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing SDK track: {name}")
            sha, lines = hashlib.sha256(), 0
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    sha.update(chunk)
                    lines += chunk.count(b"\n")
            if lines != description["count"]:
                raise ValueError(f"SDK track record count mismatch: {name}")
            description.update(sha256=sha.hexdigest(), bytes=path.stat().st_size)
            self.audit.emit("file.read_sdk_track", path=path, records=lines, sha256=sha.hexdigest())
        summary["first_valid_gps"] = (self.first_gps(directory / "gps.jsonl")
                                      if tracks["gps"]["available"] else None)
        build = Path(self.cfg.metadata_reader).parent.parent / "build.json"
        if build.is_file() and not build.is_symlink() and build.stat().st_size < 65536:
            summary["sdk_build"] = json.loads(build.read_text(encoding="utf-8"))
        self.audit.emit("metadata_sdk.extracted", source=source, camera_type=summary["camera_type"],
                        counts={name: tracks[name]["count"] for name in TRACKS})
        return summary

    def first_gps(self, path):
        with path.open(encoding="utf-8") as file:
            for line in file:
                item = json.loads(line)
                latitude = coordinate(item.get("latitude"), item.get("north_south"), "N", "S", 90)
                longitude = coordinate(item.get("longitude"), item.get("east_west"), "E", "W", 180)
                if latitude is not None and longitude is not None:
                    return {"latitude": latitude, "longitude": longitude,
                            "timestamp_raw": item.get("timestampMs"), "altitude": item.get("altitude")}
        return None
