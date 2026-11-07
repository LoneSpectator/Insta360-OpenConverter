"""Keep temporary work under one hidden directory and safely recover leftovers."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading


TEMP_ROOT_NAME = ".insta360-openconverter"
_MARKER = ".workspace.json"
_LOCK = ".workspace.lock"
_FORMAT = "Insta360_OpenConverter_workspace"
_KIND = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_LEGACY_EMPTY = re.compile(
    r"(?:report|preview|metadata-read|metadata-json|atomic|sdk-media|sdk-metadata)-[0-9a-f]{16}\Z")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# A finishing task closes its lock before unlinking it for NFS compatibility.
# Serialize that short transition with in-process recovery, which otherwise
# can acquire the released lock and delete the same workspace simultaneously.
_CONTROLS = threading.RLock()


def _emit(audit, event, **detail):
    if audit is not None:
        audit.emit(event, **detail)


def _same_entry(directory, name, descriptor):
    try:
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (entry.st_dev, entry.st_ino) == (opened.st_dev, opened.st_ino)


def _open_root(parent, create):
    parent_fd = os.open(parent, _DIRECTORY)
    try:
        for _ in range(100):
            if create:
                try:
                    os.mkdir(TEMP_ROOT_NAME, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                root_fd = os.open(TEMP_ROOT_NAME, _DIRECTORY, dir_fd=parent_fd)
                return parent_fd, root_fd
            except FileNotFoundError:
                if not create:
                    raise
        raise RuntimeError("Temporary workspace root kept changing during creation")
    except BaseException:
        os.close(parent_fd)
        raise


def _remove_empty_root(parent_fd, root_fd):
    if not _same_entry(parent_fd, TEMP_ROOT_NAME, root_fd):
        return False
    try:
        os.rmdir(TEMP_ROOT_NAME, dir_fd=parent_fd)
        return True
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
            raise
        return False


def _clear(directory_fd, keep=()):
    # All removals are relative to already-open directories. A link inside a
    # workspace is unlinked, never followed, including during crash recovery.
    mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
    if mode & 0o700 != 0o700:
        os.fchmod(directory_fd, mode | 0o700)
    # Finish directory enumeration before deleting entries. Shared filesystems
    # can invalidate readdir cookies when the listing changes underneath it.
    with os.scandir(directory_fd) as listing:
        entries = list(listing)
    for entry in entries:
        if entry.name in keep:
            continue
        if entry.is_dir(follow_symlinks=False):
            try:
                child_fd = os.open(entry.name, _DIRECTORY, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            try:
                _clear(child_fd)
                if _same_entry(directory_fd, entry.name, child_fd):
                    os.rmdir(entry.name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
        else:
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _write_marker(child_fd, kind, *, cleanup=False):
    name = f".workspace-marker-{secrets.token_hex(8)}.tmp"
    descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=child_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            data = {"format": _FORMAT, "schema_version": 1, "kind": kind}
            if cleanup:
                data["phase"] = "cleanup"
            json.dump(data, marker, indent=4)
            marker.write("\n")
            marker.flush()
            os.fsync(marker.fileno())
        os.replace(name, _MARKER, src_dir_fd=child_fd, dst_dir_fd=child_fd)
        os.fsync(child_fd)
    finally:
        try:
            os.unlink(name, dir_fd=child_fd)
        except FileNotFoundError:
            pass


def _restore_cleanup_controls(child_fd, kind):
    # A failed rmdir must leave enough ownership information for the next
    # scan. The lock is closed before any later cleanup can unlink it.
    descriptor = os.open(_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=child_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Workspace lock is not a regular file", _LOCK)
    finally:
        os.close(descriptor)
    _write_marker(child_fd, kind, cleanup=True)


def _remove_child(root_fd, name, child_fd):
    if not _same_entry(root_fd, name, child_fd):
        return False
    marker = _read_marker(child_fd)
    kind = marker["kind"] if marker and marker.get("phase") == "cleanup" else None
    try:
        _clear(child_fd, keep=(_LOCK, _MARKER) if kind else ())
        if not _same_entry(root_fd, name, child_fd):
            return False
        if kind:
            for control in (_LOCK, _MARKER):
                try:
                    os.unlink(control, dir_fd=child_fd)
                except FileNotFoundError:
                    pass
        os.rmdir(name, dir_fd=root_fd)
        return True
    except OSError:
        if kind and _same_entry(root_fd, name, child_fd):
            try:
                _restore_cleanup_controls(child_fd, kind)
            except OSError:
                pass
        raise


def _read_marker(child_fd):
    try:
        descriptor = os.open(_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=child_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            data = json.loads(os.read(descriptor, 4097))
        finally:
            os.close(descriptor)
        valid = (isinstance(data, dict)
                and set(data) in ({"format", "schema_version", "kind"},
                                  {"format", "schema_version", "kind", "phase"})
                and data["format"] == _FORMAT and type(data["schema_version"]) is int
                and data["schema_version"] == 1 and isinstance(data["kind"], str)
                and _KIND.fullmatch(data["kind"]) is not None
                and ("phase" not in data or data["phase"] == "cleanup"))
        return data if valid else None
    except (OSError, ValueError, TypeError):
        return None


def _owned_marker(child_fd):
    return _read_marker(child_fd) is not None


def _cleanup_warning(audit, path, error, *, phase="cleanup"):
    _emit(audit, "workspace.cleanup_failed", path=path, phase=phase, level="warning",
          error_code="workspace_cleanup_failed", error=str(error),
          message="临时工作目录清理未完成，已保留待后续清理。")


def _prepare_cleanup(child_fd, kind):
    mode = stat.S_IMODE(os.fstat(child_fd).st_mode)
    if mode & 0o700 != 0o700:
        os.fchmod(child_fd, mode | 0o700)
    _write_marker(child_fd, kind, cleanup=True)


def cleanup_stale_workspaces(parent, audit=None):
    """Remove only marked, unlocked workspaces; preserve everything unknown."""
    with _CONTROLS:
        return _cleanup_stale_workspaces(parent, audit)


def _cleanup_stale_workspaces(parent, audit=None):
    parent = Path(parent)
    root = parent / TEMP_ROOT_NAME
    try:
        parent_fd, root_fd = _open_root(parent, create=False)
    except FileNotFoundError:
        return 0
    except OSError as error:
        _emit(audit, "workspace.cleanup_skipped", path=root, error=str(error))
        return 0
    removed = 0
    try:
        with os.scandir(root_fd) as listing:
            entries = list(listing)
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            child_fd = lock_fd = None
            path = root / entry.name
            try:
                child_fd = os.open(entry.name, _DIRECTORY, dir_fd=root_fd)
                marker = _read_marker(child_fd)
                if marker is None:
                    if _LEGACY_EMPTY.fullmatch(entry.name) and _same_entry(root_fd, entry.name, child_fd):
                        try:
                            # The previous open-lock cleanup bug can leave an
                            # empty directory with both controls already gone.
                            # Never recursively delete an unmarked directory.
                            os.rmdir(entry.name, dir_fd=root_fd)
                            removed += 1
                            _emit(audit, "file.remove_empty_legacy_workspace", path=path)
                        except OSError as error:
                            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                                _cleanup_warning(audit, path, error)
                    continue
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                if marker.get("phase") == "cleanup":
                    flags |= os.O_CREAT
                lock_fd = os.open(_LOCK, flags, 0o600,
                                  dir_fd=child_fd)
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    continue
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if not _same_entry(child_fd, _LOCK, lock_fd) or not _owned_marker(child_fd):
                    continue
                _prepare_cleanup(child_fd, marker["kind"])
                os.close(lock_fd)
                lock_fd = None
                _emit(audit, "file.remove_stale_workspace", path=path)
                if _remove_child(root_fd, entry.name, child_fd):
                    removed += 1
            except FileNotFoundError:
                pass
            except OSError as error:
                _cleanup_warning(audit, path, error)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                if child_fd is not None:
                    os.close(child_fd)
        try:
            if _remove_empty_root(parent_fd, root_fd):
                _emit(audit, "file.remove_temporary_directory", path=root)
        except OSError as error:
            _cleanup_warning(audit, root, error, phase="root_cleanup")
    finally:
        os.close(root_fd)
        os.close(parent_fd)
    return removed


@contextmanager
def temporary_workspace(parent, audit=None, kind="work"):
    """Yield a private child, hold its lock, and remove it on every normal exit."""
    parent = Path(parent)
    if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
        raise ValueError("Temporary workspace kind must be a simple name")
    _CONTROLS.acquire()
    try:
        cleanup_stale_workspaces(parent, audit)
        try:
            parent_fd, root_fd = _open_root(parent, create=True)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise RuntimeError(f"Temporary workspace root is not a real directory: {parent / TEMP_ROOT_NAME}") from error
            raise
    except BaseException:
        _CONTROLS.release()
        raise
    name = None
    final_name = None
    child_fd = lock_fd = None
    try:
        for _ in range(100):
            token = secrets.token_hex(8)
            candidate = f".alloc-{kind}-{token}"
            published = f"{kind}-{token}"
            try:
                try:
                    os.stat(published, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    continue
                os.mkdir(candidate, 0o700, dir_fd=root_fd)
                name = candidate
                final_name = published
                break
            except FileExistsError:
                continue
            except FileNotFoundError:
                # Another finisher can remove an empty shared root between
                # our opening it and allocating the first child. Reopen it.
                new_parent_fd, new_root_fd = _open_root(parent, create=True)
                os.close(root_fd)
                os.close(parent_fd)
                parent_fd, root_fd = new_parent_fd, new_root_fd
                continue
        if name is None:
            raise RuntimeError("Could not allocate a unique temporary workspace")
        child_fd = os.open(name, _DIRECTORY, dir_fd=root_fd)
        lock_fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                          0o600, dir_fd=child_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_marker(child_fd, kind)
        if not _same_entry(parent_fd, TEMP_ROOT_NAME, root_fd):
            raise RuntimeError("Temporary workspace root changed during creation")
        # Markerless legacy names are eligible for empty-directory recovery.
        # Publish that name only after the marker and held lock exist, so a
        # concurrent scanner cannot mistake an allocation for an old remnant.
        os.rename(name, final_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        name = final_name
        path = parent / TEMP_ROOT_NAME / name
        _emit(audit, "file.create_temporary_directory", path=path)
        _CONTROLS.release()
        try:
            yield path
        finally:
            _CONTROLS.acquire()
    finally:
        try:
            if name is not None and child_fd is not None:
                if _same_entry(root_fd, name, child_fd):
                    # NFS and some other mounts delay deletion of open files.
                    # Complete the work, record recovery ownership, and close
                    # our lock before unlinking it or removing its directory.
                    _prepare_cleanup(child_fd, kind)
                if lock_fd is not None:
                    os.close(lock_fd)
                    lock_fd = None
                _emit(audit, "file.remove_temporary_directory", path=parent / TEMP_ROOT_NAME / name)
                _remove_child(root_fd, name, child_fd)
            elif name is not None:
                # Creation failed before the new empty child could be opened.
                os.rmdir(name, dir_fd=root_fd)
            if _remove_empty_root(parent_fd, root_fd):
                _emit(audit, "file.remove_temporary_directory", path=parent / TEMP_ROOT_NAME)
        except OSError as error:
            _cleanup_warning(audit, parent / TEMP_ROOT_NAME / name if name else parent / TEMP_ROOT_NAME,
                             error)
        finally:
            try:
                if lock_fd is not None:
                    os.close(lock_fd)
                if child_fd is not None:
                    os.close(child_fd)
                os.close(root_fd)
                os.close(parent_fd)
            finally:
                _CONTROLS.release()
