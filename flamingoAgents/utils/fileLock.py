'''
Author: wilbur
Version: 1.0
Date: 2026-09-14
Description: Cross-platform exclusive file locking. POSIX uses fcntl.flock; Windows uses LockFileEx.
'''

from __future__ import annotations

import os
from typing import Final

IS_WINDOWS: Final[bool] = os.name == 'nt'

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCK_LENGTH = 0xFFFFFFFF
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_LOCK_FAILED = 167
    _ERROR_NOT_LOCKED = 158

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ('Internal', ctypes.c_void_p),
            ('InternalHigh', ctypes.c_void_p),
            ('Offset', wintypes.DWORD),
            ('OffsetHigh', wintypes.DWORD),
            ('hEvent', wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _lockFileEx = _kernel32.LockFileEx
    _lockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lockFileEx.restype = wintypes.BOOL
    _unlockFileEx = _kernel32.UnlockFileEx
    _unlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlockFileEx.restype = wintypes.BOOL

    def lockExclusive(fd: int, *, nonBlocking: bool = False) -> None:
        flags = _LOCKFILE_EXCLUSIVE_LOCK
        if nonBlocking:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        overlapped = _Overlapped()
        if _lockFileEx(
            msvcrt.get_osfhandle(fd),
            flags,
            0,
            _LOCK_LENGTH,
            _LOCK_LENGTH,
            ctypes.byref(overlapped),
        ):
            return
        error = ctypes.get_last_error()
        if nonBlocking and error in (_ERROR_LOCK_VIOLATION, _ERROR_LOCK_FAILED):
            raise BlockingIOError(error, 'LockFileEx would block')
        raise OSError(None, 'LockFileEx failed', None, error)

    def unlock(fd: int) -> None:
        overlapped = _Overlapped()
        if _unlockFileEx(
            msvcrt.get_osfhandle(fd),
            0,
            _LOCK_LENGTH,
            _LOCK_LENGTH,
            ctypes.byref(overlapped),
        ):
            return
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_LOCKED:
            return
        raise OSError(None, 'UnlockFileEx failed', None, error)

else:
    import fcntl

    def lockExclusive(fd: int, *, nonBlocking: bool = False) -> None:
        flags = fcntl.LOCK_EX
        if nonBlocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
