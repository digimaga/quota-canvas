"""Inter-process lock for cooperating config writers."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def config_lock(target: Path):
    target = Path(target).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Lock a stable sibling, not the config inode replaced by os.replace.
    with target.with_name('.' + target.name + '.lock').open('a+b') as handle:
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class OVERLAPPED(ctypes.Structure):
                _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                            ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                            ('hEvent', wintypes.HANDLE)]

            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
            native = msvcrt.get_osfhandle(handle.fileno())
            overlap = OVERLAPPED()
            if not kernel.LockFileEx(native, 2, 0, 1, 0, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                yield
            finally:
                if not kernel.UnlockFileEx(native, 0, 1, 0, ctypes.byref(overlap)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
