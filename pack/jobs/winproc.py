# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""#1100: Windows-only process/security/handle adapters; no external tools.

POSIX callers retain their original code paths. Native DLLs are loaded lazily.
Handles are private, owned by this task, and closed on every failure path.
"""
from __future__ import annotations

import contextlib
import ctypes as C
from ctypes import wintypes as W
import errno
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

_jobs = {}
_jobs_lock = threading.RLock()


def win_path(path):
    """直接调 Win32 的路径参数一律过这里：>240 字符加 \\\\?\\ 前缀（规则同 pack/orch/winlong.long_path；注册表 LongPathsEnabled 关着、
    进程清单又没声明 longPathAware 时，裸长路径会得 WinError 3 / 123，v0.1.6 玩家 C:\\tool\\game 播种报 roots.py _win_acl 123）。
    作业层按文件路径装 winlong，不依赖 pack 包在 sys.path 上。"""
    global _long_path
    if _long_path is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("s2a_winlong_jobs", Path(__file__).resolve().parents[1] / "orch" / "winlong.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _long_path = module.long_path
    return _long_path(path)


_long_path = None


def _api(dll, name, result, *args):
    library = C.WinDLL(dll, use_last_error=True)
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(args)
    return function


def _checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def _close(handle):
    if handle is not None:
        _checked(_api('kernel32', 'CloseHandle', W.BOOL, W.HANDLE)(handle))


@contextlib.contextmanager
def _process(pid, access=0x1000):
    handle = _api('kernel32', 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)(access, False, int(pid))
    if not handle:
        error = C.get_last_error()
        if error == 87:
            raise ProcessLookupError(errno.ESRCH, 'process exited')
        raise C.WinError(error)
    try:
        yield handle
    finally:
        _close(handle)


def _creation_time(handle):
    times = (C.c_ulonglong * 4)()
    _checked(_api('kernel32', 'GetProcessTimes', W.BOOL, W.HANDLE, C.c_void_p,
                  C.c_void_p, C.c_void_p, C.c_void_p)(handle, *[C.byref(times, i * 8) for i in range(4)]))
    return str(times[0])


def pid_start(pid):
    with _process(pid) as handle:
        return _creation_time(handle)


def alive(pid):
    """进程还在跑？（Windows 上 os.kill(pid, 0) 的 0 = CTRL_C_EVENT，会对整个进程组发 Ctrl+C，不能拿来探活）。
    别人的进程（拒绝访问）算活着，取消时另由 cancel_verified 拒；已退出但句柄还被人持有的 → 退出码不是 STILL_ACTIVE（259）= 死"""
    try:
        with _process(pid) as handle:
            code = W.DWORD()
            _checked(_api('kernel32', 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))(handle, C.byref(code)))
            return code.value == 259
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cancel_verified(pid, started, sig):
    """Pin the verified process object until the signal has been delivered."""
    from errors import Failure
    if not isinstance(started, str) or not started:
        raise Failure('记录的进程号已不是这个任务，未发送信号。', code='job_pid_reused')
    try:
        with _process(pid, 0x1001) as handle:
            code = W.DWORD()
            _checked(_api('kernel32', 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.c_void_p)(handle, C.byref(code)))
            if code.value != 259:
                raise ProcessLookupError(errno.ESRCH, 'process exited')
            if _creation_time(handle) != started:
                raise Failure('记录的进程号已不是这个任务，未发送信号。', code='job_pid_reused')
            # Deliver to the exact verified object, without another numeric
            # PID lookup. SIGKILL is not defined by Python on Windows.
            if sig not in ('TERM', 'KILL'):
                raise ValueError('unsupported task signal')
            _checked(_api('kernel32', 'TerminateProcess', W.BOOL, W.HANDLE, W.UINT)(
                handle, 15 if sig == 'TERM' else 9))
    except PermissionError:
        raise Failure('这个进程不属于当前用户，未发送信号。', code='job_not_ours') from None


class _ThreadEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('tid', W.DWORD), ('pid', W.DWORD),
                ('base', W.LONG), ('delta', W.LONG), ('flags', W.DWORD)]


def _resume_initial_thread(process):
    # Popen closes CreateProcess's initial thread handle. Because creation was
    # suspended, no application code has run; locate that sole initial thread.
    snap = _api('kernel32', 'CreateToolhelp32Snapshot', W.HANDLE, W.DWORD, W.DWORD)(4, 0)
    if snap == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = _ThreadEntry(); entry.size = C.sizeof(entry)
        first = _api('kernel32', 'Thread32First', W.BOOL, W.HANDLE, C.c_void_p)
        next_thread = _api('kernel32', 'Thread32Next', W.BOOL, W.HANDLE, C.c_void_p)
        ids = []
        present = first(snap, C.byref(entry))
        while present:
            if entry.pid == process.pid:
                ids.append(entry.tid)
            present = next_thread(snap, C.byref(entry))
        if len(ids) != 1:
            raise OSError('suspended child does not have one initial thread')
        thread = _checked(_api('kernel32', 'OpenThread', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)(2, False, ids[0]))
        try:
            previous = _api('kernel32', 'ResumeThread', W.DWORD, W.HANDLE)(thread)
            if previous != 1:
                raise OSError('initial thread was not suspended exactly once')
        finally:
            _close(thread)
    finally:
        _close(snap)


class _Job:
    def __init__(self):
        self.handle = _checked(_api('kernel32', 'CreateJobObjectW', W.HANDLE, C.c_void_p, W.LPCWSTR)(None, None))
        self.lock = threading.RLock()
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: basic limit flags at byte16,
        # rest zero; structure is 144 bytes on Win64, 112 on Win32.
        size = 144 if C.sizeof(C.c_void_p) == 8 else 112
        limits = C.create_string_buffer(size)
        C.c_uint32.from_buffer(limits, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            _checked(_api('kernel32', 'SetInformationJobObject', W.BOOL, W.HANDLE,
                          C.c_int, C.c_void_p, W.DWORD)(self.handle, 9, limits, size))
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.handle is not None:
                handle, self.handle = self.handle, None
                _close(handle)

    def terminate(self):
        with self.lock:
            if self.handle is not None:
                _checked(_api('kernel32', 'TerminateJobObject', W.BOOL, W.HANDLE, W.UINT)(self.handle, 1))

    def members(self):
        with self.lock:
            if self.handle is None:
                return []
            size = 4096
            while size <= 1 << 20:
                data = C.create_string_buffer(size)
                ok = _api('kernel32', 'QueryInformationJobObject', W.BOOL, W.HANDLE,
                          C.c_int, C.c_void_p, W.DWORD, C.c_void_p)(self.handle, 3, data, size, None)
                if ok:
                    count = C.c_uint32.from_buffer(data, 4).value
                    return list((C.c_size_t * count).from_buffer(data, 8))
                if C.get_last_error() != 234:
                    raise C.WinError(C.get_last_error())
                size *= 2
            raise OSError('job membership exceeds safety bound')


def popen(args, **kwargs):
    """Create suspended, attach private job, then run. No breakaway window."""
    # Windows CreateProcess does not use env['PATH'] to locate its image.
    # Respect Context.env's task/runtime tool selection before creating it.
    if kwargs.get('env') is not None and not kwargs.get('executable') and not os.path.dirname(str(args[0])):
        executable = shutil.which(str(args[0]), path=kwargs['env'].get('PATH', os.defpath))
        if executable is None:
            raise FileNotFoundError(errno.ENOENT, 'task tool not found', str(args[0]))
        kwargs['executable'] = executable
    job, process = _Job(), None
    try:
        kwargs.pop('start_new_session', None)
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | 0x200 | 4
        process = subprocess.Popen(args, **kwargs)
        _checked(_api('kernel32', 'AssignProcessToJobObject', W.BOOL, W.HANDLE, W.HANDLE)(job.handle, int(process._handle)))
        process._s2a_job = job
        with _jobs_lock:
            _jobs[process.pid] = job
        _resume_initial_thread(process)
        return process
    except BaseException:
        if process is not None:
            # This is the exact Popen-owned child, even if assignment failed.
            with contextlib.suppress(OSError):
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            with _jobs_lock:
                _jobs.pop(process.pid, None)
        job.close()
        raise


def progress(process):
    """(CPU 100ns, I/O bytes) summed over the whole private job, exited members included."""
    job = getattr(process, '_s2a_job', None)
    with job.lock:
        if job.handle is None:
            return None
        # JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION (class 8): basic accounting
        # (48 bytes: total user / kernel time first) followed by IO_COUNTERS (48 bytes).
        data = C.create_string_buffer(96)
        _checked(_api('kernel32', 'QueryInformationJobObject', W.BOOL, W.HANDLE,
                      C.c_int, C.c_void_p, W.DWORD, C.c_void_p)(job.handle, 8, data, 96, None))
        user, kernel = struct.unpack_from('<qq', data, 0)
        io = struct.unpack_from('<6Q', data, 48)
        return user + kernel, io[3] + io[4] + io[5]


def communicate_nostall(process, stall, poll=None):
    """communicate() that ends only on exit or on `stall` seconds with no progress.

    Progress is any growth of the job's CPU time or I/O bytes (tools/nostall.py
    criteria ② / ④, job-wide). A fixed wall clock killed slow but working tools
    (11-05 Tianyan census Trine 5: direct_profile scanning a large original).
    Raises subprocess.TimeoutExpired on a stall; the caller reaps the job.
    """
    poll = poll or float(os.environ.get('S2A_NOSTALL_POLL', '30'))
    last, since = progress(process), time.monotonic()
    while True:
        try:
            return process.communicate(timeout=min(poll, stall))
        except subprocess.TimeoutExpired:
            now = progress(process)
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= stall:
                raise subprocess.TimeoutExpired(process.args, stall) from None


def signal_child(process):
    from errors import Failure
    if process.poll() is not None:
        return
    job = getattr(process, '_s2a_job', None)
    if job is None:
        raise Failure('无法停止本任务进程组：缺少专属作业句柄。')
    if job.handle is None:
        return
    try:
        job.terminate()
    except OSError as error:
        if process.poll() is None:
            raise Failure('无法停止本任务进程组：作业终止失败。') from error


def _member_name(job, member):
    """Image name of a live job member, or None if it exited meanwhile.

    The member list and the per-process queries race with short-lived
    grandchildren: a process listed alive can exit before OpenProcess or
    QueryFullProcessImageNameW, which then fail with 5 or 31 instead of 87.
    Only a process that is still running counts as a leftover; one whose
    name cannot be read is reported as '?'. The job terminates it either way.
    """
    try:
        with _process(member) as handle:
            size = W.DWORD(32768); name = C.create_unicode_buffer(size.value)
            if _api('kernel32', 'QueryFullProcessImageNameW', W.BOOL, W.HANDLE,
                    W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))(handle, 0, name, C.byref(size)):
                return Path(name.value).name
            code = W.DWORD()
            if _api('kernel32', 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.c_void_p)(handle, C.byref(code)) and code.value != 259:
                return None
            return '?'
    except ProcessLookupError:
        return None
    except OSError:
        return '?' if member in job.members() else None


def reap_session(pid):
    with _jobs_lock:
        job = _jobs.pop(pid, None)
    if job is None:
        return []
    names = []
    try:
        for member in job.members():
            name = _member_name(job, member)
            if name is not None:
                names.append(name)
        job.terminate()
    finally:
        job.close()
    return names


class _FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME), ('accessed', W.FILETIME),
                ('written', W.FILETIME), ('volume', W.DWORD), ('size_high', W.DWORD),
                ('size_low', W.DWORD), ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


def _info(handle):
    info = _FileInfo()
    _checked(_api('kernel32', 'GetFileInformationByHandle', W.BOOL, W.HANDLE, C.c_void_p)(handle, C.byref(info)))
    mode = stat.S_IFDIR if info.attributes & 0x10 else stat.S_IFREG
    if info.attributes & 0x400:
        mode = stat.S_IFLNK
    return SimpleNamespace(st_mode=mode, st_dev=info.volume, st_ino=(info.index_high << 32) | info.index_low,
                           st_size=(info.size_high << 32) | info.size_low, attributes=info.attributes)


def _sid_bytes(sid):
    if not _api('advapi32', 'IsValidSid', W.BOOL, C.c_void_p)(sid):
        raise OSError('invalid security identifier')
    length = _api('advapi32', 'GetLengthSid', W.DWORD, C.c_void_p)(sid)
    return C.string_at(sid, length)


def _current_sid():
    token = W.HANDLE()
    current = _api('kernel32', 'GetCurrentProcess', W.HANDLE)()
    _checked(_api('advapi32', 'OpenProcessToken', W.BOOL, W.HANDLE, W.DWORD, C.c_void_p)(current, 8, C.byref(token)))
    try:
        size = W.DWORD()
        query = _api('advapi32', 'GetTokenInformation', W.BOOL, W.HANDLE, C.c_int,
                     C.c_void_p, W.DWORD, C.c_void_p)
        query(token, 1, None, 0, C.byref(size))
        data = C.create_string_buffer(size.value)
        _checked(query(token, 1, data, size, C.byref(size)))
        return _sid_bytes(C.c_void_p.from_buffer(data).value)
    finally:
        _close(token)


def _well_known_sid(kind):
    size = W.DWORD(68); data = C.create_string_buffer(size.value)
    _checked(_api('advapi32', 'CreateWellKnownSid', W.BOOL, C.c_int, C.c_void_p,
                  C.c_void_p, C.c_void_p)(kind, None, data, C.byref(size)))
    return _sid_bytes(data)


def _private_security(handle):
    owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
    result = _api('advapi32', 'GetSecurityInfo', W.DWORD, W.HANDLE, C.c_int, W.DWORD,
                  C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)(
                      handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor))
    if result:
        raise C.WinError(result)
    try:
        current = _current_sid()
        if _sid_bytes(owner) != current or not dacl.value:
            return False
        allowed = {current, _well_known_sid(22), _well_known_sid(26)}  # SYSTEM, Administrators
        # ACL header: revision, reserved, size WORD, ace count WORD, reserved.
        count = C.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = C.c_void_p()
            _checked(_api('advapi32', 'GetAce', W.BOOL, C.c_void_p, W.DWORD, C.c_void_p)(dacl, index, C.byref(ace)))
            kind, flags, size = struct.unpack('<BBH', C.string_at(ace, 4))
            if flags & 8:  # INHERIT_ONLY_ACE does not grant access to this file
                continue
            if kind == 1:  # A deny ACE grants no rights
                continue
            if kind != 0 or size < 16:
                return False  # Unknown/object/callback ACEs are not proof of privacy
            mask = C.c_uint32.from_address(ace.value + 4).value
            if mask & 0x90020089 and _sid_bytes(ace.value + 8) not in allowed:
                return False
        return True
    finally:
        if descriptor.value:
            _api('kernel32', 'LocalFree', C.c_void_p, C.c_void_p)(descriptor)


def request_is_private(path):
    if os.name != 'nt':
        path = Path(path)
        return not (path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid())
    try:
        # Pin the inspected leaf for security queries; never query an unchecked
        # path again (GetNamedSecurityInfo would follow a replaced link).
        with open_dir_nofollow(path, directory=False, security=True) as opened:
            return stat.S_ISREG(opened.info.st_mode) and _private_security(opened.handle)
    except (OSError, ValueError):
        return False


class _UnicodeString(C.Structure):
    _fields_ = [('length', W.WORD), ('maximum', W.WORD), ('buffer', W.LPWSTR)]


class _ObjectAttributes(C.Structure):
    _fields_ = [('length', W.ULONG), ('root', W.HANDLE), ('name', C.POINTER(_UnicodeString)),
                ('attributes', W.ULONG), ('security', C.c_void_p), ('qos', C.c_void_p)]


class _IOStatus(C.Structure):
    _fields_ = [('status', C.c_void_p), ('information', C.c_size_t)]


class Opened:
    """Pinned non-reparse object. Relative opens never reconstruct a path."""
    def __init__(self, handle, directory=None):
        self.handle = handle
        try:
            self.info = _info(handle)
            if stat.S_ISLNK(self.info.st_mode):
                raise OSError(errno.ELOOP, 'reparse point refused')
            if directory is True and not stat.S_ISDIR(self.info.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, 'directory required')
            if directory is False and not stat.S_ISREG(self.info.st_mode):
                raise OSError(errno.EINVAL, 'regular file required')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            _close(handle)

    def names(self):
        names, restart = [], True
        with _directory_lock:
            while True:
                data = C.create_string_buffer(65536)
                ok = _api('kernel32', 'GetFileInformationByHandleEx', W.BOOL, W.HANDLE,
                          C.c_int, C.c_void_p, W.DWORD)(self.handle, 15 if restart else 14, data, len(data))
                if not ok:
                    if C.get_last_error() == 18:
                        return names
                    raise C.WinError(C.get_last_error())
                restart = False
                offset = 0
                while True:
                    next_offset = C.c_uint32.from_buffer(data, offset).value
                    length = C.c_uint32.from_buffer(data, offset + 60).value
                    if length % 2 or offset + 68 + length > len(data):
                        raise OSError('malformed directory enumeration')
                    name = bytes(data[offset + 68:offset + 68 + length]).decode('utf-16-le')
                    if name not in ('.', '..'):
                        names.append(name)
                    if not next_offset:
                        break
                    if next_offset < 68 or offset + next_offset >= len(data):
                        raise OSError('malformed directory enumeration offset')
                    offset += next_offset

    def delete(self):
        delete = W.BOOL(True)
        _checked(_api('kernel32', 'SetFileInformationByHandle', W.BOOL, W.HANDLE,
                      C.c_int, C.c_void_p, W.DWORD)(self.handle, 4, C.byref(delete), C.sizeof(delete)))


_directory_lock = threading.RLock()


def _relative(parent, name, directory=None, delete=False):
    if not name or name in ('.', '..') or any(c in name for c in '\\/:\0'):
        raise ValueError('relative access requires one plain component')
    text = C.create_unicode_buffer(name)
    encoded = name.encode('utf-16-le')
    string = _UnicodeString(len(encoded), len(encoded) + 2, C.cast(text, W.LPWSTR))
    attributes = _ObjectAttributes(C.sizeof(_ObjectAttributes), parent.handle, C.pointer(string), 0x40, None, None)
    handle, status = W.HANDLE(), _IOStatus()
    access = 0x100081 | (0x10000 if delete else 0)  # SYNCHRONIZE, READ_DATA, READ_ATTRIBUTES, DELETE
    options = 0x200000 | 0x20 | (1 if directory is True else 0x40 if directory is False else 0)
    result = _api('ntdll', 'NtCreateFile', W.LONG, C.c_void_p, W.DWORD, C.c_void_p,
                  C.c_void_p, C.c_void_p, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                  C.c_void_p, W.DWORD)(C.byref(handle), access, C.byref(attributes), C.byref(status),
                                      None, 0, 3, 1, options, None, 0)
    if result < 0:
        error = _api('ntdll', 'RtlNtStatusToDosError', W.ULONG, W.LONG)(result)
        raise C.WinError(error)
    return Opened(handle.value, directory)


def open_dir_nofollow(path, parent=None, *, directory=True, security=False):
    if parent is not None:
        return _relative(parent, str(path), directory)
    # Open and pin every lexical ancestor from the volume root. No resolve(),
    # which could hide reparse points, and no path-based child read/delete.
    absolute = Path(os.path.abspath(path))
    if not absolute.anchor or str(absolute).startswith('\\\\'):
        raise OSError(errno.EINVAL, 'UNC/device paths not accepted by this adapter')
    handle = _api('kernel32', 'CreateFileW', W.HANDLE, W.LPCWSTR, W.DWORD,   # lp-exempt: 只传卷根（absolute.anchor），其下逐级用相对句柄打开
                  W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)(
                      absolute.anchor, 0x100081, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    current = Opened(handle, True)
    try:
        for index, name in enumerate(absolute.parts[1:]):
            final = index == len(absolute.parts) - 2
            child = _relative(current, name, directory if final else True)
            current.close()
            current = child
        if security:
            # Relative handles need READ_CONTROL for GetSecurityInfo. Reopen
            # the *same object* with that additional access, not its path.
            handle = _api('kernel32', 'ReOpenFile', W.HANDLE, W.HANDLE, W.DWORD, W.DWORD, W.DWORD)(
                current.handle, 0x20081, 3, 0x02200000)
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            old, current = current, Opened(handle, directory)
            old.close()
        return current
    except BaseException:
        current.close()
        raise


def listdir(opened):
    return opened.names()


def stat_at(name, parent):
    with _relative(parent, name) as opened:
        return opened.info


def open_file(name, parent):
    import msvcrt
    opened = _relative(parent, name, False)
    try:
        fd = msvcrt.open_osfhandle(opened.handle, os.O_RDONLY | os.O_BINARY)
        opened.handle = None  # CRT owns it now
        return os.fdopen(fd, 'rb')
    finally:
        opened.close()


def file_info(source):
    import msvcrt
    return _info(msvcrt.get_osfhandle(source.fileno()))


def remove_at(parent, name):
    """Delete only objects opened relative to the pinned parent; refuse links."""
    with _relative(parent, name, delete=True) as opened:
        if stat.S_ISDIR(opened.info.st_mode):
            for child in opened.names():
                remove_at(opened, child)
        opened.delete()


def close(opened):
    opened.close()
