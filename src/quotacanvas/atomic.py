"""Atomic writes using a unique temporary file in the destination directory."""

import os
import secrets
import stat
from pathlib import Path
from typing import Callable


def atomic_write(path: Path, write_body: Callable) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL ensures another writer cannot share this temporary file.  Creating
    # with 0666 lets the kernel apply umask without changing the process umask.
    while True:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as handle:
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            os.fchmod(handle.fileno(), mode)
            write_body(handle)
            handle.flush()
            # Writing may clear set-ID bits; restore the complete mode afterward.
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
