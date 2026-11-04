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

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.prepare_sdk import command, temporary_environment, unpack, validate_tree


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def prepare_directory(sdk, destination, *, work_root, compiler="g++", runner=None,
                      adapter_source=None):
    """Build only this project's adapter; the official shared library is copied."""
    sdk, destination, work_root = Path(sdk), Path(destination), Path(work_root)
    if platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("This SDK package requires Linux x86_64")
    validate_tree(sdk)
    headers = list(sdk.rglob("include/metaData.h"))
    if len(headers) != 1:
        raise ValueError("Expected one SDK include/metaData.h")
    sdk = headers[0].parent.parent
    library = sdk / "lib/libInsMetaDataSDK.so"
    if not library.is_file() or not (sdk / "include/common.h").is_file():
        raise ValueError("SDK library or common.h is missing; replace the original SDK input")
    validate_tree(sdk)
    source = Path(adapter_source) if adapter_source is not None else (
        Path(__file__).resolve().parent.parent / "native/metadata_reader.cc")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    (destination / "bin").mkdir(parents=True)
    for name in ("include", "lib"):
        shutil.copytree(sdk / name, destination / name, symlinks=True)
    reader = destination / "bin/insta360-metadata"
    env = temporary_environment(work_root)
    command([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
             "-I", str(destination / "include"), str(source), "-L", str(destination / "lib"),
             "-lInsMetaDataSDK", "-Wl,-rpath,$ORIGIN/../lib", "-o", str(reader)],
            cwd=work_root, env=env, runner=runner)
    schema = json.loads(command([reader, "--schema"], cwd=work_root, env=env,
                                capture=True, runner=runner))
    if (schema.get("schema_version") != 1 or schema.get("sdk") != "InsMetaDataSDK"
            or schema.get("reader") != "insta360-metadata"):
        raise ValueError("Compiled adapter did not report the official metadata SDK schema")
    (destination / "build.json").write_text(json.dumps({
        "adapter_source_sha256": sha256(source), "reader_sha256": sha256(reader),
        "library_sha256": sha256(library), "schema": schema,
    }, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


def prepare(archive, destination, expected_hash=None, compiler="g++"):
    if platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("This SDK package requires Linux x86_64")
    actual = sha256(archive)
    if expected_hash and actual.lower() != expected_hash.lower():
        raise ValueError("Archive SHA256 does not match the supplied checksum")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".insta360-metadata-sdk-", dir=destination.parent) as directory:
        temporary = Path(directory)
        raw = temporary / "archive"
        raw.mkdir()
        unpack(archive, raw)
        ready = temporary / "ready"
        prepare_directory(raw, ready, work_root=temporary, compiler=compiler)
        build = json.loads((ready / "build.json").read_text(encoding="utf-8"))
        build.update(archive=archive.name, archive_sha256=actual)
        (ready / "build.json").write_text(json.dumps(build, ensure_ascii=False, indent=4)
                                          + "\n", encoding="utf-8")
        os.rename(ready, destination)
    print(f"Prepared metadata SDK: {destination}")
    print(f"Archive SHA256: {actual}")
    print("Place this complete directory under sdk/ (mounted read-only at /sdk).")
    print("The container creates and manages its runtime copy at /data/sdk/metadata.")


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
