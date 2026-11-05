"""Publish one self-contained metadata JSON beside each original file."""
from copy import deepcopy
import codecs
import hashlib
import json
import os
from pathlib import Path

from .audit import Cancelled
from .metadata import TRACKS
from .scan import snapshot
from .workspace import temporary_workspace


def metadata_path(source):
    source = Path(source)
    return source.with_name(source.stem + "_metadata.json")


class _Records:
    def __init__(self, path, description):
        self.path, self.description = path, description


class MetadataSidecars:
    def __init__(self, state, audit, stop):
        self.state, self.audit, self.stop = state, audit, stop

    def _check_stop(self):
        if self.stop.is_set():
            raise Cancelled("Shutdown requested while saving metadata")

    def _digest(self, path):
        self.audit.emit("file.read_hash", path=path)
        sha = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                self._check_stop()
                sha.update(chunk)
        return sha.hexdigest()

    def _source_header(self, path):
        # The ownership object is deliberately first. Sensor tracks can make
        # the remainder of the JSON very large; read only this bounded prefix.
        self.audit.emit("file.read_metadata_header", path=path)
        with path.open("rb") as file:
            prefix = codecs.getincrementaldecoder("utf-8")().decode(file.read(65536), final=False)

        def whitespace(index):
            while index < len(prefix) and prefix[index] in " \t\r\n":
                index += 1
            return index

        decoder = json.JSONDecoder()
        try:
            index = whitespace(0)
            if prefix[index] != "{":
                raise ValueError("Expected metadata object")
            key, index = decoder.raw_decode(prefix, whitespace(index + 1))
            if key != "OpenConverter":
                raise ValueError("Expected metadata ownership header first")
            index = whitespace(index)
            if prefix[index] != ":":
                raise ValueError("Expected metadata ownership header separator")
            header, _ = decoder.raw_decode(prefix, whitespace(index + 1))
            if not isinstance(header, dict) or header.get("schema_version") != 1:
                raise ValueError("Unsupported metadata ownership header")
            return header
        except (IndexError, ValueError) as error:
            raise RuntimeError(f"Invalid or oversized metadata ownership header: {path}") from error

    def _owned(self, path, source):
        if path.is_symlink():
            raise RuntimeError(f"Metadata path is a symlink: {path}")
        try:
            before = snapshot(path)
        except FileNotFoundError:
            return None
        sha = self._digest(path)
        if sha not in self.state.hashes(path):
            raise RuntimeError(f"Refusing to overwrite unowned or externally changed metadata: {path}")
        header = self._source_header(path)
        if header.get("source") != str(source):
            raise RuntimeError(f"Refusing to overwrite metadata belonging to another source: {path}")
        if snapshot(path) != before:
            raise RuntimeError(f"Metadata changed while checking ownership: {path}")
        return before

    def _check_layout(self, source):
        counterpart = source.with_name(source.stem + "_metadata")
        if counterpart.exists() or counterpart.is_symlink():
            raise RuntimeError(f"Metadata directory path already exists; refusing to create both "
                               f"metadata forms: {counterpart}")

    def _records(self, value):
        path, description = value.path, value.description
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing SDK track: {path.name}")
        sha, count, size = hashlib.sha256(), 0, 0
        self.audit.emit("file.merge_sdk_track", path=path, records=description["count"])
        with path.open("rb") as file:
            for line in file:
                self._check_stop()
                sha.update(line)
                size += len(line)
                if not line.endswith(b"\n"):
                    raise ValueError(f"Truncated SDK track: {path.name}")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"SDK track record is not an object: {path.name}")
                count += 1
                yield record
        if (count != description["count"] or size != description["bytes"]
                or sha.hexdigest() != description["sha256"]):
            raise ValueError(f"SDK track changed while merging metadata: {path.name}")

    def _write(self, file, value, level=0):
        """Use JSON's four-space layout while consuming track records lazily."""
        self._check_stop()
        indent, child = " " * (4 * level), " " * (4 * (level + 1))
        if isinstance(value, dict):
            if not value:
                file.write("{}")
                return
            file.write("{\n")
            for index, (key, item) in enumerate(value.items()):
                if index:
                    file.write(",\n")
                file.write(child + json.dumps(key, ensure_ascii=False) + ": ")
                self._write(file, item, level + 1)
            file.write("\n" + indent + "}")
        elif isinstance(value, (_Records, list, tuple)):
            iterator = self._records(value) if isinstance(value, _Records) else iter(value)
            file.write("[")
            count = 0
            for item in iterator:
                file.write(",\n" if count else "\n")
                file.write(child)
                self._write(file, item, level + 1)
                count += 1
            if count:
                file.write("\n" + indent)
            file.write("]")
        else:
            file.write(json.dumps(value, ensure_ascii=False, allow_nan=False))

    def save(self, source, metadata, directory, before, source_sha256):
        source, directory = Path(source), Path(directory)
        destination = metadata_path(source)
        self._check_stop()
        if snapshot(source) != before:
            raise RuntimeError("Source changed during metadata extraction")
        self._check_layout(source)
        previous = self._owned(destination, source)
        document = {"OpenConverter": {"schema_version": 1, "source": str(source),
                                       "source_sha256": source_sha256}}
        document.update({key: value for key, value in deepcopy(metadata).items()
                         if key != "OpenConverter"})
        tracks = document["Insta360SDK"]["tracks"]
        if set(tracks) != set(TRACKS):
            raise ValueError("Metadata SDK did not report all track statuses")
        for name in TRACKS:
            description = tracks[name]
            if description.pop("file", None) != name + ".jsonl":
                raise ValueError(f"Invalid SDK track descriptor: {name}")
            description["json_pointer"] = f"/Insta360SDK/tracks/{name}/records"
            description["records"] = _Records(directory / (name + ".jsonl"), description)
        with temporary_workspace(source.parent, self.audit, kind="metadata-json") as temporary:
            staged = temporary / destination.name
            self.audit.emit("file.write_metadata_snapshot", source=source, destination=staged)
            with staged.open("x", encoding="utf-8") as file:
                self._write(file, document)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            self._check_stop()
            if snapshot(source) != before:
                raise RuntimeError("Source changed during metadata extraction")
            sha = self._digest(staged)
            if self._owned(destination, source) != previous:
                raise RuntimeError("Metadata changed before publication; refusing to overwrite")
            self._check_stop()
            if snapshot(source) != before:
                raise RuntimeError("Source changed during metadata extraction")
            self._check_layout(source)
            self.state.intend(destination, sha)
            if previous is None:
                self.audit.emit("file.publish_new_metadata", source=staged,
                                destination=destination, sha256=sha)
                os.link(staged, destination)
                staged.unlink()
            else:
                self.audit.emit("file.replace_owned_metadata", source=staged,
                                destination=destination, sha256=sha)
                os.replace(staged, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.state.published(destination, sha)
            self.audit.emit("metadata.sidecar.complete", source=source, destination=destination,
                            sha256=sha, records={name: tracks[name]["count"] for name in TRACKS})
            return destination
