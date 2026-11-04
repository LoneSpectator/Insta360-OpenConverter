"""Build and verify container-local SDK runtimes from read-only user inputs."""
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

# These helpers are also usable as standalone tools. Importing them during a
# container startup must not create files beside their installed source.
sys.dont_write_bytecode = True
from scripts.prepare_metadata_sdk import prepare_directory as prepare_metadata
from scripts.prepare_sdk import prepare_directory as prepare_media
from scripts.prepare_sdk import temporary_environment, unpack, validate_tree

from .audit import Cancelled


PROJECT = "Insta360-OpenConverter"
MARKER = ".insta360-managed"
MARKER_TEXT = "Insta360-OpenConverter SDK runtime v1\n"
MANIFEST = ".insta360-bootstrap.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_SOURCE = PROJECT_ROOT / "native/metadata_reader.cc"


def file_sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def tree_sha256(root, *, runtime=False):
    """Hash content, paths, symlink destinations and permission bits."""
    root = Path(root)
    validate_tree(root)
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if runtime and relative in (MARKER, MANIFEST):
            continue
        mode = path.lstat().st_mode
        if path.is_symlink():
            identity = [relative, "symlink", os.readlink(path)]
        elif path.is_dir():
            identity = [relative, "directory", stat.S_IMODE(mode)]
        else:
            identity = [relative, "file", stat.S_IMODE(mode), file_sha256(path)]
        value.update(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode())
        value.update(b"\n")
    return value.hexdigest()


def _version(path):
    versions = re.findall(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", path.name)
    return tuple(map(int, versions[0])) if len(versions) == 1 else None


def _archive_component(path):
    name = path.name.lower()
    if not ((".tar" in name and name.endswith((".gz", ".bz2", ".xz", ".tar")))
            or name.endswith(".tgz")):
        return None
    if name.startswith("insmetadatasdk"):
        return "metadata"
    if name.startswith("mediasdk"):
        return "media"
    return None


def discover_source(source_root, component):
    """Find SDKs by family and layout, without fixing package version/date names."""
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"SDK source directory must be a real directory: {source_root}")
    candidates = {}
    for path in source_root.rglob("*"):
        if _archive_component(path) == component:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"SDK archive must be a regular file: {path}")
            candidates[path] = "archive"
    if component == "metadata":
        for header in source_root.rglob("metaData.h"):
            if header.parent.name == "include":
                candidates[header.parent.parent] = "directory"
    else:
        roots = set()
        for executable in source_root.rglob("MediaSDKTest"):
            if executable.parent.name == "bin":
                roots.add(executable.parent.parent)
        candidates.update({root: "directory" for root in roots})
        for package in source_root.rglob("*.deb"):
            if any(package.is_relative_to(root) for root in roots):
                continue
            if ("mediasdk" in package.name.lower()
                    or any(p.name.lower() == "media" or "mediasdk" in p.name.lower()
                           for p in package.parents if p.is_relative_to(source_root))):
                candidates[package.parent] = "directory"
    if not candidates:
        raise ValueError(f"No {component} SDK found in {source_root}; place its official archive there")
    # A package can be retained beside an unpacked copy. Multiple known
    # versions are ordered explicitly; uncertain or equal top versions fail.
    versions = {path: _version(path) for path in candidates}
    if len(candidates) > 1:
        if any(version is None for version in versions.values()):
            raise ValueError(f"Ambiguous {component} SDK inputs without unique version numbers: "
                             + ", ".join(str(path) for path in sorted(candidates)))
        newest = max(versions.values())
        selected = [path for path in candidates if versions[path] == newest]
        if len(selected) != 1:
            raise ValueError(f"Ambiguous {component} SDK inputs for version {newest}: "
                             + ", ".join(str(path) for path in sorted(selected)))
        path = selected[0]
    else:
        path = next(iter(candidates))
    if not path.resolve().is_relative_to(source_root.resolve()) or path.is_symlink():
        raise ValueError(f"SDK source escapes its source directory: {path}")
    kind = candidates[path]
    if kind == "directory":
        validate_tree(path)
    return path, kind, versions[path]


def _source_identity(source_root, path, kind, version):
    return {
        "path": path.relative_to(source_root).as_posix(),
        "kind": kind,
        "version": ".".join(map(str, version)) if version else None,
        "sha256": file_sha256(path) if kind == "archive" else tree_sha256(path),
    }


def _builder_identity(component):
    paths = [Path(__file__), PROJECT_ROOT / "scripts/prepare_sdk.py"]
    if component == "metadata":
        paths.extend([PROJECT_ROOT / "scripts/prepare_metadata_sdk.py", ADAPTER_SOURCE])
    value = hashlib.sha256()
    for path in paths:
        value.update(path.name.encode())
        value.update(bytes.fromhex(file_sha256(path)))
    identity = {"sha256": value.hexdigest()}
    if component == "metadata":
        identity["adapter_source_sha256"] = file_sha256(ADAPTER_SOURCE)
    return identity


def _assert_owned(destination):
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError(f"Managed SDK destination must be a real directory: {destination}")
    if not any(destination.iterdir()):
        # Previous Compose versions could leave empty bind-mount targets.
        return
    marker = destination / MARKER
    if (marker.is_symlink() or not marker.is_file()
            or marker.read_text(encoding="utf-8") != MARKER_TEXT):
        raise ValueError(f"Refusing to overwrite an SDK directory without a project ownership marker: "
                         f"{destination}; move user-supplied SDKs to the configured source directory")


def _cache_valid(destination, component, source, builder):
    if not destination.exists():
        return False
    _assert_owned(destination)
    manifest = destination / MANIFEST
    try:
        if manifest.is_symlink():
            return False
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return (data.get("project") == PROJECT and data.get("schema_version") == 1
                and data.get("component") == component and data.get("source") == source
                and data.get("builder") == builder
                and data.get("runtime_sha256") == tree_sha256(destination, runtime=True))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return False


def _publish(ready, destination):
    if not destination.exists():
        if destination.is_symlink():
            raise ValueError(f"SDK destination is a symlink: {destination}")
        os.rename(ready, destination)
        return
    _assert_owned(destination)
    # Linux rename exchange swaps non-empty directory trees in one operation.
    # The old runtime remains in staging until the temporary context removes it.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("Atomic SDK replacement requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(ready), -100, os.fsencode(destination), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"Atomic SDK runtime replacement failed: {os.strerror(error)}")


def _verify_media(ready, work_root, runner):
    executable = ready / "bin/MediaSDKTest"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Original MediaSDK executable is missing or not executable; replace its input")
    env = temporary_environment(work_root)
    env["LD_LIBRARY_PATH"] = ":".join([str(ready / "lib"), str(ready / "bin")]
                                       + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    text = runner.run([executable, "-help"], cwd=work_root, env=env, capture=True,
                      accepted_returncodes=(0, 1, 255))
    if "-inputs" not in text or "-output" not in text:
        raise ValueError("Original MediaSDK did not report its expected command interface; "
                         "check the SDK input, libraries and NVIDIA container runtime")


def _prepare_component(cfg, audit, runner, base, component):
    destination = base / component
    if destination.exists() or destination.is_symlink():
        _assert_owned(destination)
    source_root = Path(cfg.source_root)
    path, kind, version = discover_source(source_root, component)
    source = _source_identity(source_root, path, kind, version)
    builder = _builder_identity(component)
    audit.emit("sdk.source.selected", component=component, **source)
    if _cache_valid(destination, component, source, builder):
        audit.emit("sdk.cache.hit", component=component, path=destination,
                   source_sha256=source["sha256"])
        return
    audit.emit("sdk.prepare.start", component=component, source=path, destination=destination,
               reason="first_start_or_changed_or_damaged", builder=builder)
    with tempfile.TemporaryDirectory(prefix=f".insta360-{component}-", dir=base) as directory:
        temporary = Path(directory)
        ready = temporary / "ready"
        try:
            raw = path
            if kind == "archive":
                raw = temporary / "archive"
                raw.mkdir()
                audit.emit("sdk.archive.extract", component=component, source=path, destination=raw)
                unpack(path, raw)
            if component == "metadata":
                prepare_metadata(raw, ready, work_root=temporary, runner=runner,
                                 adapter_source=ADAPTER_SOURCE)
            else:
                prepare_media(raw, ready, work_root=temporary, runner=runner)
                _verify_media(ready, temporary, runner)
            if source != _source_identity(source_root, path, kind, version):
                raise ValueError("SDK input changed while preparing it; retry with stable SDK inputs")
            # Copying a read-only SDK can also copy a read-only root mode.
            ready.chmod(stat.S_IMODE(ready.stat().st_mode) | 0o700)
            (ready / MARKER).write_text(MARKER_TEXT, encoding="utf-8")
            manifest = {"project": PROJECT, "schema_version": 1, "component": component,
                        "source": source, "builder": builder,
                        "runtime_sha256": tree_sha256(ready, runtime=True)}
            (ready / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=4)
                                           + "\n", encoding="utf-8")
            _publish(ready, destination)
            descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            audit.emit("sdk.prepare.complete", component=component, destination=destination,
                       runtime_sha256=manifest["runtime_sha256"], source=source)
        except Cancelled:
            raise
        except Exception as error:
            audit.emit("sdk.prepare.failed", component=component, error=str(error),
                       previous_runtime_preserved=destination.exists())
            raise RuntimeError(f"{component} SDK preparation failed: {error}. "
                               "The official SDK is precompiled; damaged original libraries must be "
                               "replaced with an intact official SDK input.") from error


def prepare_sdks(cfg, audit, runner, metadata_only=False):
    """Prepare default managed paths; custom SDK paths remain user-managed."""
    base = Path(cfg.data_root) / "sdk"
    managed = []
    if not metadata_only and Path(cfg.executable) == base / "media/bin/MediaSDKTest":
        managed.append("media")
    if Path(cfg.metadata_reader) == base / "metadata/bin/insta360-metadata":
        managed.append("metadata")
    if not managed:
        return
    data_root = Path(cfg.data_root)
    if not data_root.is_dir() or data_root.is_symlink():
        raise ValueError(f"Data directory must be a real directory: {data_root}")
    if base.is_symlink():
        raise ValueError(f"SDK cache directory must not be a symlink: {base}")
    source_root = Path(cfg.source_root).resolve()
    if source_root == base.resolve() or source_root.is_relative_to(base.resolve()) or (
            base.resolve().is_relative_to(source_root)):
        raise ValueError("SDK source and generated cache directories must not overlap")
    base.mkdir(exist_ok=True)
    lock_path = base / ".insta360-bootstrap.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        started = time.monotonic()
        while True:
            if runner.stop.is_set():
                raise Cancelled("Shutdown requested while waiting for SDK preparation")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started > runner.timeout:
                    raise TimeoutError("Timed out waiting for another SDK preparation")
                runner.stop.wait(0.1)
        audit.emit("sdk.prepare.locked", path=lock_path, components=managed)
        for component in managed:
            _prepare_component(cfg, audit, runner, base, component)
    finally:
        os.close(descriptor)
