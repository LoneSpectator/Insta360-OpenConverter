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


TEMP_ROOT_NAME = ".insta360-openconverter"
_MARKER = ".workspace.json"
_LOCK = ".workspace.lock"
_FORMAT = "Insta360_OpenConverter_workspace"
_KIND = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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


def _clear(directory_fd):
    # All removals are relative to already-open directories. A link inside a
    # workspace is unlinked, never followed, including during crash recovery.
    mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
    if mode & 0o700 != 0o700:
        os.fchmod(directory_fd, mode | 0o700)
    for entry in os.scandir(directory_fd):
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


def _remove_child(root_fd, name, child_fd):
    if not _same_entry(root_fd, name, child_fd):
        return False
    _clear(child_fd)
    if _same_entry(root_fd, name, child_fd):
        os.rmdir(name, dir_fd=root_fd)
        return True
    return False


def _owned_marker(child_fd):
    try:
        descriptor = os.open(_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=child_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return False
            data = json.loads(os.read(descriptor, 4097))
        finally:
            os.close(descriptor)
        return (isinstance(data, dict) and set(data) == {"format", "schema_version", "kind"}
                and data["format"] == _FORMAT and type(data["schema_version"]) is int
                and data["schema_version"] == 1 and isinstance(data["kind"], str)
                and _KIND.fullmatch(data["kind"]) is not None)
    except (OSError, ValueError, TypeError):
        return False


def cleanup_stale_workspaces(parent, audit=None):
    """Remove only marked, unlocked workspaces; preserve everything unknown."""
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
        for entry in os.scandir(root_fd):
            if not entry.is_dir(follow_symlinks=False):
                continue
            child_fd = lock_fd = None
            path = root / entry.name
            try:
                child_fd = os.open(entry.name, _DIRECTORY, dir_fd=root_fd)
                if not _owned_marker(child_fd):
                    continue
                lock_fd = os.open(_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=child_fd)
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    continue
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if not _same_entry(child_fd, _LOCK, lock_fd) or not _owned_marker(child_fd):
                    continue
                _emit(audit, "file.remove_stale_workspace", path=path)
                if _remove_child(root_fd, entry.name, child_fd):
                    removed += 1
            except FileNotFoundError:
                pass
            except OSError as error:
                _emit(audit, "workspace.cleanup_failed", path=path, error=str(error))
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                if child_fd is not None:
                    os.close(child_fd)
        try:
            if _remove_empty_root(parent_fd, root_fd):
                _emit(audit, "file.remove_temporary_directory", path=root)
        except OSError as error:
            _emit(audit, "workspace.cleanup_failed", path=root, error=str(error))
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
    cleanup_stale_workspaces(parent, audit)
    try:
        parent_fd, root_fd = _open_root(parent, create=True)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RuntimeError(f"Temporary workspace root is not a real directory: {parent / TEMP_ROOT_NAME}") from error
        raise
    name = None
    child_fd = lock_fd = None
    original_error = None
    try:
        for _ in range(100):
            candidate = f"{kind}-{secrets.token_hex(8)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=root_fd)
                name = candidate
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
        path = parent / TEMP_ROOT_NAME / name
        child_fd = os.open(name, _DIRECTORY, dir_fd=root_fd)
        lock_fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                          0o600, dir_fd=child_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker_fd = os.open(_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                            0o600, dir_fd=child_fd)
        with os.fdopen(marker_fd, "w", encoding="utf-8") as marker:
            json.dump({"format": _FORMAT, "schema_version": 1, "kind": kind}, marker, indent=4)
            marker.write("\n")
            marker.flush()
            os.fsync(marker.fileno())
        if not _same_entry(parent_fd, TEMP_ROOT_NAME, root_fd):
            raise RuntimeError("Temporary workspace root changed during creation")
        _emit(audit, "file.create_temporary_directory", path=path)
        yield path
    except BaseException as error:
        original_error = error
        raise
    finally:
        try:
            if name is not None and child_fd is not None:
                _emit(audit, "file.remove_temporary_directory", path=parent / TEMP_ROOT_NAME / name)
                _remove_child(root_fd, name, child_fd)
            elif name is not None:
                # Creation failed before the new empty child could be opened.
                os.rmdir(name, dir_fd=root_fd)
            if _remove_empty_root(parent_fd, root_fd):
                _emit(audit, "file.remove_temporary_directory", path=parent / TEMP_ROOT_NAME)
        except OSError as error:
            _emit(audit, "workspace.cleanup_failed", path=parent / TEMP_ROOT_NAME,
                  error=str(error))
            if original_error is None:
                raise
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if child_fd is not None:
                os.close(child_fd)
            os.close(root_fd)
            os.close(parent_fd)
