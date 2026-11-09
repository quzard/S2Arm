# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Versioned artifact transactions; layout and CAS rules mirror pack/roots.sh.

Callers own the root directory. Only the current link and the one approved
RomFS-store link may be reparse points. No bash, downloads or global settings.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from .winlong import long_path   # Win32 路径入口（CreateFileW / CopyFile2 / SetFileSecurityW / GetVolumePathNameW）不吃 Python 层的前缀补丁：>240 字符必须自己过 long_path
from ctypes import wintypes as W
import errno
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid

from pack.jobs import filelock
from graphics.nvn.tools import romfs_list

REPO = Path(__file__).resolve().parents[2]


class UnsafeRoot(ValueError):
    pass


class Conflict(RuntimeError):
    pass


def text(path, value):
    Path(path).write_text(value, encoding='utf-8', newline='\n')


def read(path, default=''):
    try:
        return Path(path).read_text(encoding='utf-8').rstrip('\n')
    except FileNotFoundError:
        return default


def component(value):
    if (not value or value in ('.', '..', '现行') or value.startswith('.')
            or any(c in value for c in '/\\@<>:"|?*[]\r\n\t')
            or value.endswith((' ', '.'))):
        raise UnsafeRoot(f'非法目录名：{value!r}')
    return value


def is_link(path):
    try:
        return bool(Path(path).lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == 'nt' else Path(path).is_symlink()
    except FileNotFoundError:
        return False


def _api(dll, name, result, *args):
    f = getattr(C.WinDLL(dll, use_last_error=True), name)
    f.argtypes, f.restype = args, result
    return f


@contextmanager
def _handle(path, access):
    f = _api('kernel32', 'CreateFileW', W.HANDLE, W.LPCWSTR, W.DWORD,
             W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
    h = f(long_path(str(Path(path).absolute())), access, 7, None, 3, 0x02200000, None)
    if h == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        yield h
    finally:
        _api('kernel32', 'CloseHandle', W.BOOL, W.HANDLE)(h)


def _junction(target, link):
    """Create a mount-point reparse record without cmd.exe interpolation."""
    raw = str(Path(target).absolute())
    if raw.startswith('\\\\'):
        raise UnsafeRoot('junction 不接受 UNC/设备路径')
    sub = ('\\??\\' + raw).encode('utf-16-le')
    display = raw.encode('utf-16-le')
    names = sub + b'\0\0' + display + b'\0\0'
    data = struct.pack('<IHHHHHH', 0xA0000003, len(names) + 8, 0,
                       0, len(sub), len(sub) + 2, len(display)) + names
    link.mkdir()
    try:
        with _handle(link, 0x40000000) as h:
            buf = C.create_string_buffer(data)
            done = W.DWORD()
            f = _api('kernel32', 'DeviceIoControl', W.BOOL, W.HANDLE,
                     W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD,
                     C.POINTER(W.DWORD), C.c_void_p)
            if not f(h, 0x900A4, buf, len(data), None, 0, C.byref(done), None):
                raise C.WinError(C.get_last_error())
    except BaseException:
        link.rmdir()
        raise


def directory_link(target, link, *, relative=None):
    target, link = Path(target), Path(link)
    try:
        # Windows stores the substitute name verbatim; '/' in a relative
        # reparse target can be created successfully but fails kernel traversal.
        value = relative if relative is not None else str(target)
        os.symlink(os.path.normpath(value), link, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        print(f'【黄】目录软链不可用，显式 junction 退路：{link.name}', file=sys.stderr)
        _junction(target, link)


def replace_link(source, dest):
    """One atomic link rename; never delete current before replacement."""
    source, dest = Path(source), Path(dest)
    if os.path.lexists(dest) and not is_link(dest):
        raise UnsafeRoot('现行目标是真目录，拒绝覆盖')
    if os.name != 'nt':
        os.replace(source, dest)
        return
    name = str(dest.absolute())

    class Rename(C.Structure):
        _fields_ = [('Flags', W.DWORD), ('RootDirectory', W.HANDLE),
                    ('FileNameLength', W.DWORD), ('FileName', W.WCHAR * (len(name.encode('utf-16-le')) // 2 + 1))]

    info = Rename(3, None, len(name.encode('utf-16-le')), name)
    with _handle(source, 0x10000) as h:
        f = _api('kernel32', 'SetFileInformationByHandle', W.BOOL,
                 W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
        if not f(h, 22, C.byref(info), C.sizeof(info)):
            raise C.WinError(C.get_last_error())


def refs_same_volume(source, dest):
    if os.name != 'nt':
        return False
    getpath = _api('kernel32', 'GetVolumePathNameW', W.BOOL, W.LPCWSTR, W.LPWSTR, W.DWORD)
    roots = []
    for p in (source, Path(dest).parent):
        root = C.create_unicode_buffer(1024)
        if not getpath(long_path(os.path.realpath(p)), root, len(root)):   # 先解开目录联接（同 tools/refs_clone.py：作业目录是指到别的卷的联接时，按字面路径判错卷）
            raise C.WinError(C.get_last_error())
        roots.append(root.value)
    if os.path.normcase(roots[0]) != os.path.normcase(roots[1]):
        return False
    fs = C.create_unicode_buffer(64)
    getinfo = _api('kernel32', 'GetVolumeInformationW', W.BOOL, W.LPCWSTR,
                  W.LPWSTR, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, W.LPWSTR, W.DWORD)
    if not getinfo(roots[0], None, 0, None, None, None, fs, len(fs)):
        raise C.WinError(C.get_last_error())
    return fs.value.upper() == 'REFS'


def copy_file(source, dest):
    source, dest = Path(source), Path(dest)
    if is_link(source) or not source.is_file():
        raise UnsafeRoot(f'不复制链接/特殊文件：{source}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = False
    if sys.platform == 'darwin':
        libc = C.CDLL(None, use_errno=True)
        clone = libc.clonefile
        clone.argtypes, clone.restype = (C.c_char_p, C.c_char_p, C.c_int), C.c_int
        if clone(os.fsencode(source), os.fsencode(dest), 0) == 0:
            cloned = True
        elif C.get_errno() not in (errno.EXDEV, errno.ENOTSUP):
            raise OSError(C.get_errno(), 'clonefile 失败')
    elif sys.platform.startswith('linux'):
        import fcntl
        with source.open('rb') as src, dest.open('xb') as dst:
            try:
                fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
                cloned = True
            except OSError as error:
                if error.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY):
                    raise
                print(f'【黄】块克隆不可用：实拷 {source.name}', file=sys.stderr)
                shutil.copyfileobj(src, dst, 1024 * 1024)
                cloned = True
    if not cloned and refs_same_volume(source, dest):
        class Params(C.Structure):
            _fields_ = [('size', W.DWORD), ('flags', W.DWORD),
                        ('cancel', C.c_void_p), ('progress', C.c_void_p), ('context', C.c_void_p)]
        params = Params(C.sizeof(Params), 1, None, None, None)
        f = _api('kernel32', 'CopyFile2', C.c_long, W.LPCWSTR, W.LPCWSTR, C.POINTER(Params))
        rc = f(long_path(str(source)), long_path(str(dest)), C.byref(params))
        if rc < 0:
            raise OSError(rc, 'ReFS CopyFile2 失败；不悄悄转实拷')
        _win_acl(dest, False)
    elif not cloned:
        print(f'【黄】非 ReFS 同卷克隆：实拷 {source.name}', file=sys.stderr)
        with source.open('rb') as src, dest.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    shutil.copystat(source, dest, follow_symlinks=False)
    # Version outputs must remain writable; RomFS is sealed separately.
    dest.chmod(stat.S_IMODE(dest.stat().st_mode) | stat.S_IWUSR)


_ACL_SD = {}   # 本进程的当前用户：封存 / 解封两个安全描述符只建一次（逐文件只调 SetFileSecurityW）


def _win_acl(path, sealed):
    """Private owned artifacts only; Windows directory readonly bits do not seal.
    11-06 出包速度线：原先每个文件都重转 SID、重建描述符（Windows 出包机微基准每文件 0.14 毫秒，缓存后 0.01）；王国之泪 30.5 万个文件逐个封存"""
    sd = _ACL_SD.get(sealed)
    if sd is None:
        from pack.jobs import winproc
        sid = winproc._current_sid()
        ptr = C.c_void_p()
        convert_sid = _api('advapi32', 'ConvertSidToStringSidW', W.BOOL, C.c_void_p, C.POINTER(C.c_void_p))
        if not convert_sid(C.cast(sid, C.c_void_p), C.byref(ptr)):
            raise C.WinError(C.get_last_error())
        free = _api('kernel32', 'LocalFree', C.c_void_p, C.c_void_p)
        try:
            owner = C.wstring_at(ptr)
        finally:
            free(ptr)
        # Deny only mutation, not GENERIC_WRITE (which also includes SYNCHRONIZE
        # and would reject ordinary read handles). Keep owner WRITE_DAC for GC.
        deny = f'(D;OICI;0x00010156;;;{owner})' if sealed else ''
        sddl = f'D:P{deny}(A;OICI;GA;;;{owner})(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)'
        sd = C.c_void_p()
        convert = _api('advapi32', 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
                       W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p)
        if not convert(sddl, 1, C.byref(sd), None):
            raise C.WinError(C.get_last_error())
        _ACL_SD[sealed] = sd   # 进程内常驻（两个，几百字节），不 LocalFree
    setsecurity = _ACL_SD.get('set') or _ACL_SD.setdefault('set', _api('advapi32', 'SetFileSecurityW', W.BOOL, W.LPCWSTR, W.DWORD, C.c_void_p))
    if not setsecurity(long_path(str(path)), 0x80000004, sd):
        raise C.WinError(C.get_last_error())


def seal(path, readonly=True):
    path = Path(path)
    if is_link(path):
        raise UnsafeRoot('不能沿链接更改只读权限')
    paths = [path]
    if path.is_dir():
        paths.extend(plain_entries(path))
    for p in paths:
        if os.name == 'nt':
            _win_acl(p, readonly)
        else:
            before = stat.S_IMODE(p.stat().st_mode)
            after = before & ~0o222 if readonly else before | 0o200
            if after != before:
                p.chmod(after)


def remove_owned(path):
    """Caller passes an exact owned path; unlink reparse objects without walking."""
    path = Path(path)
    if is_link(path):
        path.rmdir() if os.name == 'nt' and path.is_dir() else path.unlink()
    elif path.is_dir():
        if os.name == 'nt':
            _win_acl(path, False)
        else:
            path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
        for p in path.iterdir():
            remove_owned(p)
        path.rmdir()
    elif path.exists():
        if os.name == 'nt':
            _win_acl(path, False)
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        path.unlink()


def remove_patterns(directory, patterns):
    """Top-level stage-output selection, never a game-data directory walk."""
    for pattern in patterns:
        if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise UnsafeRoot('删除模式离开版本根')
        for p in Path(directory).glob(pattern):
            remove_owned(p)


def plain_entries(path):
    """Repository's sole directory inventory; validate before any mutation."""
    listing = romfs_list.scan(path)
    if listing.errors:
        raise next(iter(listing.errors.values()))
    paths = []
    for entry in listing.entries:
        if entry.lstat_error:
            raise entry.lstat_error
        p = Path(entry.path)
        if is_link(p) or not (p.is_file() or p.is_dir()):
            raise UnsafeRoot(f'不接受链接/特殊文件：{p}')
        paths.append(p)
    return paths


def git(*args, repo=REPO, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=check)
    return result.stdout.decode('utf-8').strip() if check else result.returncode == 0


def process_start(pid):
    if os.name == 'nt':
        from pack.jobs import winproc
        try:
            # A terminated process object can remain queryable while Popen
            # still owns its handle. Creation time alone is not liveness.
            with winproc._process(pid) as handle:
                code = W.DWORD()
                query = _api('kernel32', 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
                if not query(handle, C.byref(code)):
                    raise C.WinError(C.get_last_error())
                return winproc._creation_time(handle) if code.value == 259 else ''
        except OSError:
            return ''
    r = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True)
    return ' '.join(r.stdout.split())


class Roots:
    def __init__(self, path=None, *, shared=None, repo=REPO):
        run = Path(os.environ.get('S2A_RUN_ROOT', Path.home() / '.s2arm/run')) / 's2arm'
        self.shared_path = Path(os.environ.get('S2A_ROOTS_SHARED', run / 'roots')).resolve()
        self.private_path = Path(os.environ.get('S2A_ROOTS_PRIV', run / 'roots_priv')).resolve()
        configured = Path(path or os.environ.get('S2A_ROOTS', self.shared_path)).absolute()
        if is_link(configured):
            raise UnsafeRoot('产物根本身不能是重解析点')
        self.path = configured.resolve()
        self.repo = Path(repo)
        self.shared = self.path == self.shared_path if shared is None else shared
        self.path.mkdir(parents=True, exist_ok=True)

    def _guard(self, game):
        if not self.shared:
            return
        if not git('merge-base', '--is-ancestor', 'HEAD', 'origin/main', repo=self.repo, check=False):
            raise Conflict('共享根只代表 main；分支使用 roots_priv')
        if git('status', '--porcelain', '--', 'runtime', 'ios', 'graphics', 'translator', 'services', 'compat', 'pack', repo=self.repo):
            raise Conflict('共享根拒绝未提交的阶段源码')
        cur = self.cur(game, pin=False)
        if cur and read(cur / '.root_src') and not git('merge-base', '--is-ancestor', read(cur / '.root_src'), 'HEAD', repo=self.repo, check=False):
            raise Conflict('共享现行来源更晚，只许往前')

    def _romfs(self, version, game):
        p = version / 'romfs'
        if not is_link(p):
            return None
        target = p.resolve(strict=True)
        store = self.path / '.romfs_store' / game
        if target.parent != store or not target.is_dir() or is_link(target) or target.name.startswith('.'):
            raise UnsafeRoot('RomFS 只能指向本款存储中的单个真实身份目录')
        for ancestor in (store, store.parent):
            if is_link(ancestor):
                raise UnsafeRoot('RomFS 存储父目录不能是链接')
        if p.is_symlink():
            expected = f'../.romfs_store/{game}/{target.name}'
            if os.readlink(p).replace('\\', '/') != expected:
                raise UnsafeRoot('RomFS 必须使用约定相对链接')
        return target

    def _validate(self, version, game):
        if is_link(version) or version.parent != self.path:
            raise UnsafeRoot('版本必须是根下真实目录')
        target = self._romfs(version, game)
        for name in romfs_list.listdir(version):
            p = version / name
            if p == version / 'romfs' and target:
                continue  # The immutable store was validated and sealed on intern.
            if is_link(p):
                raise UnsafeRoot(f'版本中有非法链接：{p}')
            if p.is_dir():
                plain_entries(p)

    def cur(self, game, *, pin=True):
        component(game)
        override = os.environ.get('S2A_ROOT_CUR')
        if override:
            return Path(override).resolve(strict=True)
        link = self.path / f'{game}@现行'
        if not os.path.lexists(link):
            if not os.environ.get('S2A_ROOTS_NO_BOOT') and self.private_path in self.path.parents:
                shared = Roots(self.shared_path, shared=True, repo=self.repo)
                source = shared.cur(game)
                if source:
                    # Store links are local to a roots directory. A private
                    # bootstrap therefore clones the resolved RomFS once.
                    shared._validate(source, game)
                    tx = self.begin(game, self.tag(game, 'bootstrap'), _no_boot=True)
                    try:
                        for name in romfs_list.listdir(source):
                            p = source / name
                            if p.name in ('.root_base', '.root_src', '.root_pid', '.root_base_stages'):
                                continue
                            if p.is_dir():
                                self._copy_tree(p.resolve(), tx.path / p.name)
                            else:
                                copy_file(p, tx.path / p.name)
                        tx.commit()
                    except BaseException:
                        tx.abort()
                        raise
                    return self.cur(game, pin=pin)
            return None
        if not is_link(link):
            raise UnsafeRoot('现行必须是目录链接')
        current = link.resolve(strict=True)
        if current.parent != self.path or not current.name.startswith(game + '@') or is_link(current):
            raise UnsafeRoot('现行链接离开了本款版本目录')
        if pin:
            text(self.path / f'.pin.{current.name}.{os.getpid()}', f'{os.getpid()} {process_start(os.getpid())}\n')
        return current

    def tag(self, game, value):
        component(game); component(value)
        result, number = value, 1
        while any(os.path.lexists(self.path / f'{game}@{result}{suffix}') for suffix in ('', '.tmp', '.tmp.rb')):
            number += 1
            result = f'{value}-{number}'
        return result

    @contextmanager
    def locked(self, game, timeout=86400):
        component(game)
        lock = self.path / f'.{game}.commit.lock'
        with lock.open('a+b') as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b'\0'); stream.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    filelock.try_lock(stream)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('产物根提交锁超时')
                    time.sleep(0.05)
            try:
                yield
            finally:
                filelock.unlock(stream)

    def _clone(self, source, dest, game, *, own_romfs=False, env=None):
        self._validate(source, game)
        target = self._romfs(source, game)
        dest.mkdir()
        for name in romfs_list.listdir(source):
            p = source / name
            if p.name == 'romfs' and target:
                if own_romfs:
                    self._copy_tree(target, dest / 'romfs')
                else:
                    directory_link(target, dest / 'romfs', relative=f'../.romfs_store/{game}/{target.name}')
            elif p.is_dir():
                self._copy_tree(p, dest / p.name)
            else:
                copy_file(p, dest / p.name)
        if not own_romfs and (dest / 'romfs').is_dir() and not target:
            seal(dest / 'romfs')
        self._resource('--root-clone', source, dest, env=env)
        self._shards(dest, '克隆现行(root_begin)', env=env)

    def _copy_tree(self, source, dest):
        if is_link(source):
            raise UnsafeRoot('克隆拒绝未批准的链接')
        dest.mkdir(parents=True)
        for p in plain_entries(source):
            output = dest / p.relative_to(source)
            if p.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                copy_file(p, output)

    def _resource(self, mode, *paths, env=None):
        helper = self.repo / 'pack/resource_input_key.py'
        if helper.is_file():
            py = (env or {}).get('S2A_PYTHON') or (env or {}).get('S2A_PY') or sys.executable
            subprocess.run([py, '-X', 'utf8', str(helper), mode, *map(str, paths)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def _shards(self, path, label, *, env=None):
        helper = self.repo / 'tools/direct_shards.py'
        if helper.is_file():
            py = (env or {}).get('S2A_PYTHON') or (env or {}).get('S2A_PY') or sys.executable
            log = (env or os.environ).get('S2A_SHARD_LOG', str(self.path / '.shard_inventory.log'))
            subprocess.run([py, '-X', 'utf8', str(helper), 'inventory', str(path), '--label', label, '--log', log],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def begin(self, game, tag, *, own_romfs=False, _no_boot=False, env=None):
        component(game); component(tag)
        self._guard(game)
        dest = self.path / f'{game}@{tag}.tmp'
        if os.path.lexists(dest) or os.path.lexists(self.path / f'{game}@{tag}'):
            raise Conflict('版本名已存在，不覆写其它事务')
        current = None if _no_boot else self.cur(game)
        try:
            if current:
                self._clone(current, dest, game, own_romfs=own_romfs, env=env)
            else:
                dest.mkdir()
            text(dest / '.root_base', (current.name if current else '无') + '\n')
            text(dest / '.root_src', git('rev-parse', 'HEAD', repo=self.repo) + '\n')
            text(dest / '.root_pid', f'{os.getpid()} {process_start(os.getpid())}\n')
            stages = sorted(current.glob('.stage_*')) if current else ()
            text(dest / '.root_base_stages', ''.join(f'{p.name}\t{read(p)}\n' for p in stages if p.is_file()))
            return Transaction(self, game, tag, dest, env=env)
        except BaseException:
            remove_owned(dest)
            raise

    def _intern(self, version, game):
        p = version / 'romfs'
        if not p.is_dir() or is_link(p):
            return
        store = self.path / '.romfs_store' / game
        for directory in (store.parent, store):
            if is_link(directory):
                raise UnsafeRoot('RomFS 存储路径非法')
            directory.mkdir(exist_ok=True)
        identity = f'{version.name.split("@", 1)[1].split(".tmp", 1)[0]}-{time.time_ns()}'
        temp = version / '.romfs.intern'
        if os.name == 'nt':
            _win_acl(p, False)
        else:
            p.chmod(stat.S_IMODE(p.stat().st_mode) | 0o200)
        p.rename(temp)
        try:
            directory_link(store / identity, p, relative=f'../.romfs_store/{game}/{identity}')
            temp.rename(store / identity)
            seal(store / identity)
        except BaseException:
            if is_link(p):
                remove_owned(p)
            if temp.exists():
                temp.rename(p)
            raise

    def _pinned(self, version):
        for pin in self.path.glob(f'.pin.{version.name}.*'):
            pid, _, stamp = read(pin).partition(' ')
            if pid.isdecimal() and int(pid) == os.getpid():
                continue
            if pid.isdecimal() and stamp and process_start(int(pid)) == stamp:
                return True
            pin.unlink()
        return False

    def prune(self, game):
        current = self.cur(game, pin=False)
        others = sorted((p for p in self.path.glob(f'{game}@*') if p.is_dir()
                         and not is_link(p) and not p.name.endswith(('.tmp', '.tmp.rb'))
                         and p != current), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for p in others[1:]:
            if not self._pinned(p):
                remove_owned(p)
        store = self.path / '.romfs_store' / game
        if store.is_dir() and not is_link(store):
            used = {self._romfs(p, game) for p in self.path.glob(f'{game}@*')
                    if p.is_dir() and not is_link(p)}
            for name in romfs_list.listdir(store):
                p = store / name
                if p not in used and not is_link(p):
                    remove_owned(p)


class Transaction:
    def __init__(self, roots, game, tag, path, *, env=None):
        self.roots, self.game, self.tag, self.path = roots, game, tag, path
        self.closed = False
        self.env = env

    def abort(self):
        if not self.closed:
            remove_owned(self.path)
            self.closed = True

    def _replay(self, current, patterns):
        r = self.path.with_name(self.path.name + '.rb')
        own_romfs = any(p.lstrip('!') == 'romfs' or p.lstrip('!').startswith('romfs/') for p in patterns)
        self.roots._clone(current, r, self.game, own_romfs=own_romfs, env=self.env)
        try:
            for exclude in (False, True):
                source = current if exclude else self.path
                for raw in patterns:
                    if raw.startswith('!') != exclude:
                        continue
                    pattern = raw.lstrip('!')
                    if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
                        raise UnsafeRoot('重放路径离开版本目录')
                    for p in r.glob(pattern):
                        remove_owned(p)
                    for p in source.glob(pattern):
                        dst = r / p.relative_to(source)
                        if p.is_dir():
                            self.roots._copy_tree(p, dst)
                        else:
                            copy_file(p, dst)
            ms, ns = read(self.path / '.root_src'), read(current / '.root_src')
            if not ns or (ms and git('merge-base', '--is-ancestor', ns, ms, repo=self.roots.repo, check=False)):
                text(r / '.root_src', ms + '\n')
            text(r / '.root_base', current.name + '\n')
            self.roots._shards(r, 'replay·换完路径后', env=self.env)
            remove_owned(self.path)
            r.rename(self.path)
        except BaseException:
            remove_owned(r)
            raise

    def commit(self, *, replay=()):
        if self.closed or not self.path.is_dir():
            raise Conflict('事务已结束或缺失')
        roots = self.roots
        final = roots.path / f'{self.game}@{self.tag}'
        activation = roots.path / f'.{self.game}.current-{uuid.uuid4().hex}'
        try:
            with roots.locked(self.game):
                current = roots.cur(self.game, pin=False)
                base = read(self.path / '.root_base')
                now = current.name if current else '无'
                if base != now:
                    if not replay or not current:
                        raise Conflict('现行已前进；未声明重放路径，中止')
                    ns, ms = read(current / '.root_src'), read(self.path / '.root_src')
                    stages = read(self.path / '.root_base_stages').splitlines()
                    old = dict(line.split('\t', 1) for line in stages if '\t' in line)
                    touched = any(p.startswith('.stage_') and read(current / p) != old.get(p, '') for p in replay)
                    if roots.shared and touched and ns and ms and not git('merge-base', '--is-ancestor', ns, ms, repo=roots.repo, check=False):
                        raise Conflict('更新 main 已改过本阶段；共享现行不回退')
                    self._replay(current, tuple(replay))
                roots._validate(self.path, self.game)
                roots._intern(self.path, self.game)
                self.path.rename(final)
                directory_link(final, activation, relative=final.name)
                replace_link(activation, roots.path / f'{self.game}@现行')
                self.closed = True
                if os.environ.get('S2A_RESOURCE_KEY_ONCE_FILE'):
                    roots._resource('--root-publish', final, env=self.env)
                roots.prune(self.game)
                return final
        except BaseException:
            self.abort()
            raise
        finally:
            if os.path.lexists(activation):
                remove_owned(activation)
