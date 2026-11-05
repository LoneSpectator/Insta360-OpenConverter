#!/usr/bin/env python3
"""Extract a user-supplied official SDK locally, without running package scripts."""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from converter.workspace import temporary_workspace


def validate_tree(root):
    """Require a self-contained SDK tree before copying or loading its files."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"SDK root must be a real directory: {root}")
    resolved = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"SDK has a broken or cyclic symlink: {path}") from error
            if not target.is_relative_to(resolved):
                raise ValueError(f"SDK symlink escapes its root: {path}")
        elif not (path.is_file() or path.is_dir()):
            raise ValueError(f"SDK contains a special file: {path}")


def command(argv, *, cwd, env, capture=False, runner=None):
    if runner is not None:
        return runner.run(argv, cwd=cwd, env=env, capture=capture)
    result = subprocess.run(argv, cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else None, text=True)
    return result.stdout if capture else ""


def temporary_environment(directory):
    env = os.environ.copy()
    for key in ("TMPDIR", "TMP", "TEMP"):
        env[key] = str(directory)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def decompress_zstd(source, destination):
    """Decode Debian's zstd payload with dpkg's existing system library."""
    # Ubuntu 22.04 dpkg-deb defaults to zstd. Python 3.10 tarfile has no zstd
    # reader, while dpkg already depends on this decoder. Stream into the owned
    # workspace so arbitrarily large SDK/model payloads do not fill memory.
    try:
        library = ctypes.CDLL("libzstd.so.1")
    except OSError as error:
        raise ValueError("Reading a zstd Debian SDK package requires libzstd.so.1") from error

    class Buffer(ctypes.Structure):
        _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]

    library.ZSTD_createDStream.argtypes = []
    library.ZSTD_createDStream.restype = ctypes.c_void_p
    library.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    library.ZSTD_freeDStream.restype = ctypes.c_size_t
    library.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    library.ZSTD_initDStream.restype = ctypes.c_size_t
    library.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(Buffer),
                                            ctypes.POINTER(Buffer)]
    library.ZSTD_decompressStream.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = [ctypes.c_size_t]
    library.ZSTD_isError.restype = ctypes.c_uint
    library.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    library.ZSTD_getErrorName.restype = ctypes.c_char_p

    def checked(result):
        if library.ZSTD_isError(result):
            message = library.ZSTD_getErrorName(result).decode("utf-8", errors="replace")
            raise ValueError(f"Invalid zstd Debian SDK payload: {message}")
        return result

    decoder = library.ZSTD_createDStream()
    if not decoder:
        raise MemoryError("Could not allocate zstd decoder")
    try:
        checked(library.ZSTD_initDStream(decoder))
        output = ctypes.create_string_buffer(128 * 1024)
        remaining = 1
        with Path(source).open("rb") as compressed, Path(destination).open("xb") as plain:
            for chunk in iter(lambda: compressed.read(128 * 1024), b""):
                data = ctypes.create_string_buffer(chunk, len(chunk))
                incoming = Buffer(ctypes.cast(data, ctypes.c_void_p), len(chunk), 0)
                while True:
                    outgoing = Buffer(ctypes.cast(output, ctypes.c_void_p), len(output), 0)
                    position = incoming.pos
                    remaining = checked(library.ZSTD_decompressStream(
                        decoder, ctypes.byref(outgoing), ctypes.byref(incoming)))
                    plain.write(output.raw[:outgoing.pos])
                    if incoming.pos == incoming.size and (not remaining or outgoing.pos < len(output)):
                        break
                    if incoming.pos == position and not outgoing.pos:
                        raise ValueError("zstd Debian SDK payload made no decoding progress")
            if remaining:
                raise ValueError("Truncated zstd Debian SDK payload")
    finally:
        library.ZSTD_freeDStream(decoder)


def validate_debian_payload(package, work_root):
    """Check .deb paths before dpkg-deb can write them, including link parents."""
    # Debian binary packages use a simple ar envelope. Copy the compressed
    # data member to the owned working directory without installing anything.
    payload = Path(work_root) / "debian-data.tar"
    found = False
    compression = None
    with Path(package).open("rb") as file:
        if file.read(8) != b"!<arch>\n":
            raise ValueError("Invalid Debian SDK package envelope")
        while header := file.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise ValueError("Invalid Debian SDK package member")
            name = header[:16].decode("ascii").strip().rstrip("/")
            try:
                size = int(header[48:58])
            except ValueError as error:
                raise ValueError("Invalid Debian SDK package member size") from error
            if size < 0:
                raise ValueError("Invalid Debian SDK package member size")
            if name.startswith("data.tar"):
                if found:
                    raise ValueError("Multiple Debian SDK data payloads")
                found = True
                compression = name
                remaining = size
                with payload.open("xb") as output:
                    while remaining:
                        chunk = file.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Truncated Debian SDK data payload")
                        output.write(chunk)
                        remaining -= len(chunk)
            else:
                file.seek(size, os.SEEK_CUR)
            if size % 2:
                file.seek(1, os.SEEK_CUR)
    if not found:
        raise ValueError("Debian SDK package has no data payload")
    if compression.endswith(".zst"):
        plain = Path(work_root) / "debian-data-uncompressed.tar"
        decompress_zstd(payload, plain)
        payload = plain
    root = (Path(work_root) / "package").resolve()
    with tarfile.open(payload, "r:*") as tar:
        members = tar.getmembers()
        links = set()
        for member in members:
            target = (root / member.name).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Debian SDK member escapes destination: {member.name}")
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError(f"Debian SDK device/special file not accepted: {member.name}")
            if member.issym() or member.islnk():
                link_root = target.parent if member.issym() else root
                link = (link_root / member.linkname).resolve()
                if not link.is_relative_to(root):
                    raise ValueError(f"Debian SDK link escapes destination: {member.name}")
                links.add(target)
        for member in members:
            target = (root / member.name).resolve()
            if any(parent in links for parent in target.parents):
                raise ValueError(f"Debian SDK member writes through a link: {member.name}")


def prepare_directory(source, destination, *, work_root, runner=None):
    """Normalize an extracted SDK without installing its Debian package."""
    source, destination, work_root = Path(source), Path(destination), Path(work_root)
    validate_tree(source)
    candidates = list(source.rglob("bin/MediaSDKTest"))
    if not candidates:
        packages = list(source.rglob("*.deb"))
        if len(packages) != 1:
            raise ValueError("Expected an SDK bin/MediaSDKTest or exactly one .deb")
        extracted = work_root / "package"
        validate_debian_payload(packages[0], work_root)
        extracted.mkdir()
        # Extract data only: no package installation or maintainer scripts.
        command(["dpkg-deb", "--extract", str(packages[0]), str(extracted)],
                cwd=work_root, env=temporary_environment(work_root), runner=runner)
        validate_tree(extracted)
        candidates = list(extracted.rglob("bin/MediaSDKTest"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one bin/MediaSDKTest; found {len(candidates)}")
    root = candidates[0].parent.parent
    if not (root / "lib").is_dir():
        raise ValueError("SDK lib directory is missing")
    if not (root / "bin/models").is_dir():
        raise ValueError("SDK bin/models directory is missing")
    validate_tree(root)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    shutil.copytree(root, destination, symlinks=True)


def unpack(archive, destination):
    # tarfile's data filter is unavailable in the Python 3.10 Ubuntu base.
    # Explicitly disallow archive links, devices and escaping member paths.
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f"Archive member escapes destination: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Archive link/device not accepted: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, filter="data")
        else:
            tar.extractall(destination)


def prepare(archive: Path, destination: Path, expected_hash=None):
    sha = hashlib.sha256()
    with archive.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    print(f"Archive SHA256: {actual}", flush=True)
    if expected_hash and actual.lower() != expected_hash.lower():
        raise ValueError("Archive SHA256 does not match the supplied official checksum")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists; choose a new directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with temporary_workspace(destination.parent, kind="sdk-media") as temporary:
        raw = temporary / "archive"
        raw.mkdir()
        unpack(archive, raw)
        ready = temporary / "ready"
        prepare_directory(raw, ready, work_root=temporary)
        os.rename(ready, destination)
    print(f"Prepared: {destination.resolve()}")
    print("Place this complete directory under sdk/ (mounted read-only at /sdk).")
    print("The container creates and manages its runtime copy at /data/sdk/media.")
    print("No installer scripts ran. Run the container doctor and a real INSP/INSV conversion next.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--sha256", help="Compare with the checksum obtained from the SDK publisher")
    args = parser.parse_args()
    try:
        prepare(args.archive.resolve(), args.destination.absolute(), args.sha256)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"SDK preparation failed: {error}\n")


if __name__ == "__main__":
    main()
