#!/usr/bin/env python3
"""Extract a user-supplied official SDK locally, without running package scripts."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


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
    with tempfile.TemporaryDirectory(prefix=".insta360-sdk-", dir=destination.parent) as directory:
        temporary = Path(directory)
        raw = temporary / "archive"
        raw.mkdir()
        unpack(archive, raw)
        candidates = list(raw.rglob("bin/MediaSDKTest"))
        if not candidates:
            packages = list(raw.rglob("*.deb"))
            if len(packages) != 1:
                raise ValueError("Expected an SDK bin/MediaSDKTest or exactly one .deb; inspect README.txt")
            extracted = temporary / "package"
            extracted.mkdir()
            # dpkg-deb extracts data only; it does not execute maintainer scripts.
            subprocess.run(["dpkg-deb", "--extract", str(packages[0]), str(extracted)], check=True)
            candidates = list(extracted.rglob("bin/MediaSDKTest"))
        if len(candidates) != 1:
            raise ValueError(f"Expected one bin/MediaSDKTest; found {len(candidates)}")
        root = candidates[0].parent.parent
        if not (root / "lib").is_dir():
            raise ValueError("SDK lib directory is missing")
        if not (root / "bin/models").is_dir():
            raise ValueError("SDK bin/models directory is missing; inspect the actual SDK layout")
        # Preserve package-provided .so symlinks, but reject references outside
        # the SDK tree so the relocated installation remains self-contained.
        for path in root.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"SDK has an external symlink requiring manual installation: {path}")
        ready = temporary / "ready"
        shutil.copytree(root, ready, symlinks=True)
        os.rename(ready, destination)
    print(f"Prepared: {destination.resolve()}")
    print("Use the complete directory as a read-only mount at /data/sdk/media.")
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
