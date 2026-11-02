#!/usr/bin/env python3
"""Prepare a local InsMetaDataSDK and build the project-owned JSON adapter."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.prepare_sdk import unpack


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def prepare(archive, destination, expected_hash=None, compiler="g++"):
    if platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("This SDK package requires Linux x86_64")
    actual = sha256(archive)
    if expected_hash and actual.lower() != expected_hash.lower():
        raise ValueError("Archive SHA256 does not match the supplied checksum")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent.parent / "native/metadata_reader.cc"
    with tempfile.TemporaryDirectory(prefix=".insta360-metadata-sdk-", dir=destination.parent) as directory:
        temporary = Path(directory)
        raw = temporary / "archive"
        raw.mkdir()
        unpack(archive, raw)
        headers = list(raw.rglob("include/metaData.h"))
        if len(headers) != 1:
            raise ValueError("Expected one SDK include/metaData.h")
        sdk = headers[0].parent.parent
        library = sdk / "lib/libInsMetaDataSDK.so"
        if not library.is_file() or not (sdk / "include/common.h").is_file():
            raise ValueError("SDK library or common.h is missing")
        for path in sdk.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(sdk.resolve()):
                raise ValueError(f"SDK symlink escapes its root: {path}")
        ready = temporary / "ready"
        (ready / "bin").mkdir(parents=True)
        for name in ("include", "lib"):
            shutil.copytree(sdk / name, ready / name, symlinks=True)
        reader = ready / "bin/insta360-metadata"
        subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ready / "include"), str(source), "-L", str(ready / "lib"),
                        "-lInsMetaDataSDK", "-Wl,-rpath,$ORIGIN/../lib", "-o", str(reader)], check=True)
        schema = json.loads(subprocess.check_output([reader, "--schema"], text=True))
        if schema.get("schema_version") != 1:
            raise ValueError("Compiled adapter did not report schema version 1")
        (ready / "build.json").write_text(json.dumps({
            "archive": archive.name, "archive_sha256": actual,
            "adapter_source_sha256": sha256(source), "reader_sha256": sha256(reader),
            "library_sha256": sha256(library), "schema": schema,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(ready, destination)
    print(f"Prepared metadata SDK: {destination}")
    print(f"Archive SHA256: {actual}")
    print("Mount this directory read-only at /data/sdk/metadata.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--compiler", default="g++")
    args = parser.parse_args()
    try:
        prepare(args.archive.resolve(), args.destination.absolute(), args.sha256, args.compiler)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Metadata SDK preparation failed: {error}\n")


if __name__ == "__main__":
    main()
