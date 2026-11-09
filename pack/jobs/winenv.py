# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Read-only Windows packaging inventory: the launcher's single start-of-job check (bundled packing items from oneclick_win.env_items + Impactor + system components). See WINDOWS_ENV.md."""
from __future__ import annotations

import argparse
import ast
import ctypes
import functools
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 65536
PYTHON_PROBE_SECONDS = 90
RUNTIME = "launchers/macos/requirements-runtime.txt"
PREFLIGHT = "pack/jobs/toolchain.py"
METAL_LOCK = "translator/dmsh_chain.lock"
LIBRARY = "pack/jobs/library.py"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")
PINS = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)")
REQUEST_FIELDS = {"schema", "python", "targetDir", "requiredFreeBytes", "metal", "shell", "target",
                  "anisette", "offline", "catalog", "gameId", "library", "resources"}
PACK = "pack/orch/oneclick_win.py:env_items"   # 出包件自检的唯一来源（随包运行时 / 预编件 / 预编壳…），同 oneclick_win --check
PACK_OWN = {"python", "metal"}
# 只有签名装机才用：不是导入和出包的前提，缺了或认不出都只提示，装机时真失败再说具体原因
SIGNING_ONLY = {"impactor", "anisette", "apple_driver", "apple_usb"}
OFFICIAL = {"metal": "https://developer.apple.com/metal/tools/",                 # 苹果 Metal Developer Tools for Windows
            "apple_driver": "https://apps.microsoft.com/detail/9np83lwlpz9k",    # 微软商店 Apple Devices
            "anisette": "https://apps.microsoft.com/detail/9pktq5699m62"}        # 微软商店 iCloud（本机设备信息，不用公共服务器）   # 这两项本模块核得更细（钉版依赖、Metal 版本对锁），出包件那份不重复列


class ProbeError(Exception):
    """Bounded command failure. detail is one short, path-free sentence for the player; the rest of the output is never exposed."""
    def __init__(self, message, *, missing=False, nonzero=False, detail=""):
        super().__init__(message)
        self.missing = missing
        self.nonzero = nonzero
        self.detail = detail


def _read_file(path, limit=524288):
    path = Path(path)
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
        raise ValueError("需要普通非链接文件")
    if meta.st_size > limit:
        raise ValueError("文件过大")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("文件过大")
    return data.decode("utf-8-sig")


def _text(relative):
    return _read_file(ROOT / relative)


@functools.lru_cache(maxsize=1)
def _redact_module():
    spec = importlib.util.spec_from_file_location("_s2a_winenv_redact", ROOT / "tools/redact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe(value):
    return _redact_module().redact_text(str(value), key_values=())


def _public(value):
    if isinstance(value, str):
        return _safe(value)
    if isinstance(value, list):
        return [_public(x) for x in value]
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()}
    return value


def _json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复JSON字段")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def _function(relative, name):
    tree = ast.parse(_text(relative))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(found) != 1:
        raise ValueError("共用定义缺失或不唯一")
    return found[0]


def _python_minimum():
    # Read the existing definition rather than invent a second Python version table.
    fn = _function(PREFLIGHT, "preflight")
    found = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "add" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "Python"):
            continue
        compare = node.args[1]
        if (isinstance(compare, ast.Compare) and len(compare.ops) == 1
                and isinstance(compare.ops[0], ast.GtE)
                and ast.unparse(compare.left) == "sys.version_info"):
            value = ast.literal_eval(compare.comparators[0])
            if isinstance(value, tuple) and value and all(type(x) is int and x >= 0 for x in value):
                found.append(value)
    if len(found) != 1:
        raise ValueError("共用Python最低版本定义无法核实")
    return found[0]


def _requirements():
    pins = {}
    for line in _text(RUNTIME).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = PINS.fullmatch(line)
        if not match or match[1] in pins:
            raise ValueError("共用钉版清单格式错误")
        pins[match[1]] = match[2]
    if not pins:
        raise ValueError("共用钉版清单为空")
    return pins


def _validate(request):
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise ValueError("未知请求格式")
    if type(request.get("schema", 1)) is not int or request.get("schema", 1) != 1:
        raise ValueError("请求schema必须为1")
    for key in ("python", "targetDir", "metal", "shell", "catalog", "gameId", "library", "resources"):
        if key in request and (not isinstance(request[key], str) or not request[key]
                               or len(request[key]) > 32768 or any(c in request[key] for c in "\0\r\n")):
            raise ValueError("路径字段格式错误")
    if "requiredFreeBytes" in request and (type(request["requiredFreeBytes"]) is not int
                                          or request["requiredFreeBytes"] < 0):
        raise ValueError("空间预算必须为非负整数字节数")
    if "target" in request and request["target"] not in ("ios", "mac"):
        raise ValueError("target只认ios / mac")
    if "offline" in request and type(request["offline"]) is not bool:
        raise ValueError("offline必须为布尔值")
    if "anisette" in request:
        value = request["anisette"]
        if not isinstance(value, dict) or set(value) - {"source", "endpoint", "allowPublic"}:
            raise ValueError("anisette字段格式错误")
        if any(k in value and (not isinstance(value[k], str) or len(value[k]) > 2048)
               for k in ("source", "endpoint")):
            raise ValueError("anisette来源格式错误")
        if "allowPublic" in value and type(value["allowPublic"]) is not bool:
            raise ValueError("公共备选必须显式为布尔值")


def _exit_reason(code, stderr):
    """子进程失败的真实原因：系统加载器的错误码翻成人话，其余取 stderr 最后一行（去掉家目录，限长）。"""
    unsigned = code & 0xFFFFFFFF
    if unsigned in (0xC0000135, 0xC000007B, 0xC0000139):
        return f"缺少系统运行库（退出码 0x{unsigned:08X}）；请安装 Microsoft Visual C++ 运行库后重新检查"
    try:
        text = stderr.decode()
    except UnicodeDecodeError:   # 非 Python 程序的报错按系统代码页（中文 Windows 是 GBK）
        text = stderr.decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last = lines[-1].replace(str(Path.home()), "~")[:200] if lines else "没有错误输出"
    return f"退出码 {code}：{last}"


class NativeProbe:
    """OS/command observations only, never a caller-supplied ready verdict."""
    def __init__(self, timeout=15):
        self.timeout = timeout

    def run(self, args, *, merge_stderr=False, timeout=None):
        limit = timeout or self.timeout
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", DOTNET_CLI_TELEMETRY_OPTOUT="1",
                   DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1", DOTNET_NOLOGO="1")
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                                       env=env, creationflags=0x08000000 if os.name == "nt" else 0)
        except OSError:
            # PATH lookup can yield EACCES even for an absent command. Missing,
            # denied and invalid-image launches share one bounded public error.
            raise ProbeError("找不到或无法执行所选工具", missing=True, detail="文件不在或被系统拦住，常见原因是没有完整解压或被杀毒软件隔离") from None
        data, errors, overflow = bytearray(), bytearray(), threading.Event()

        def drain():
            try:
                while block := process.stdout.read(4096):
                    if len(data) + len(block) > LIMIT:
                        overflow.set()
                        process.kill()
                        return
                    data.extend(block)
            except OSError:
                overflow.set()

        def drain_errors():
            try:
                while block := process.stderr.read(4096):
                    errors.extend(block)
                    del errors[:-8192]
            except OSError:
                pass

        readers = [threading.Thread(target=drain, daemon=True)]
        if not merge_stderr:
            readers.append(threading.Thread(target=drain_errors, daemon=True))
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise ProbeError("只读探测超时", detail=f"{limit:.0f} 秒内没有运行完，常见原因是杀毒软件在扫描第一次运行的文件") from None
        finally:
            for reader in readers:
                reader.join(timeout=1)
            if not readers[0].is_alive():
                process.stdout.close()
            if not merge_stderr and not readers[-1].is_alive():
                process.stderr.close()
        if readers[0].is_alive() or overflow.is_set():
            raise ProbeError("只读探测失败或输出超限")
        if process.returncode:
            raise ProbeError("只读探测失败退出", nonzero=True, detail=_exit_reason(process.returncode, bytes(errors)))
        return bytes(data)

    def __call__(self, kind, **options):
        if kind == "host":
            return {"system": platform.system(), "bits": struct.calcsize("P") * 8}
        if kind == "python":
            code = (
                "import importlib,json,sys\n"
                "out={'version':list(sys.version_info[:3]),'packages':{}}\n"
                "for dist,name in json.loads(sys.argv[1]).items():\n"
                " try:\n"
                "  module=importlib.import_module(name)\n"
                "  out['packages'][dist]={'version':getattr(module,'__version__',None)}\n"
                " except Exception as exc: out['packages'][dist]={'error':type(exc).__name__,"
                "'missing':isinstance(exc,ModuleNotFoundError) and exc.name==name}\n"
                "print(json.dumps(out,ensure_ascii=True))\n"
            )
            # 冷启动要加载 numpy 等原生库，杀毒软件第一次扫描时几十秒都正常
            return json.loads(self.run([options["executable"], "-I", "-B", "-c", code,
                                       json.dumps(options["modules"])], timeout=PYTHON_PROBE_SECONDS))
        if kind == "pack":
            sys.path.insert(0, str(ROOT))
            from pack.orch import oneclick_win
            return [list(row) for row in oneclick_win.env_items(options["target"], options.get("shell"))]
        if kind == "impactor":
            sys.path.insert(0, str(ROOT / "pack/orch"))
            import install_win
            # 启动器目录在就只认随包那份；不传（源码树 / 命令行）才找玩家自己放的
            resources = options.get("resources")
            st = install_win.impactor.status(resources=resources)
            bundled = install_win.impactor.bundled(resources) or None   # 随包那份不在就认玩家自己装的
            return {"verdict": st["verdict"], "version": st.get("version"), "tested": st["tested"], "download": st["download"],
                    "plumesign": bool(bundled.get("plumesign")) if bundled is not None else bool(install_win.plumesign_path()),
                    "bundled": bundled is not None}
        if kind == "file":
            path = options.get("path")
            if not path:
                return {"exists": False, "regular": False}
            try:
                meta = Path(path).lstat()
            except FileNotFoundError:
                return {"exists": False, "regular": False}
            return {"exists": True, "regular": stat.S_ISREG(meta.st_mode)
                    and not bool(getattr(meta, "st_file_attributes", 0) & 0x400)}
        if kind == "volume":   # 游戏库所在卷的格式 + 判云盘要的家目录与 OneDrive 环境变量（libloc 纯函数判，不在探针里判）
            libloc = _libloc()
            path = Path(options["path"]).absolute()
            while not path.exists() and path != path.parent:
                path = path.parent
            fmt = ""
            if os.name == "nt":
                root, fs = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(64)
                k32 = ctypes.windll.kernel32
                if k32.GetVolumePathNameW(ctypes.c_wchar_p(_long_path()(str(path))), root, 1024) and k32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 64):
                    fmt = fs.value.strip().lower()
            return {"format": fmt, "home": str(Path.home()), "env": {v: os.environ[v] for _, v in libloc.WIN_CLOUD_ENV if os.environ.get(v)}}
        if kind == "disk":
            path = Path(options["path"]).absolute()
            while not path.exists() and path != path.parent:
                path = path.parent
            return {"free": shutil.disk_usage(path).free}
        if kind == "service":
            return self.service()
        if kind == "metal":
            exe = (options.get("executable") or os.environ.get("S2A_METAL_EXE") or shutil.which("metal.exe")
                   or str(Path(os.environ.get("S2A_METAL_BIN") or r"C:\Program Files\Metal Developer Tools\bin") / "metal.exe"))   # 同 oneclick_win.metal_bin()：官方安装器缺省装这里、不进 PATH
            if not exe:
                raise FileNotFoundError()
            meta = self("file", path=exe)
            if not meta["exists"]:
                raise FileNotFoundError()
            if not meta["regular"]:
                raise ProbeError("Metal路径不是普通非链接文件")
            return {"output": self.run([exe, "--version"], merge_stderr=True).decode("utf-8", "strict")}
        if kind in ("usbmux_port", "usb_devices"):
            if sys.platform != "win32":
                raise ProbeError("此宿主不能核Windows USB服务")
            if kind == "usbmux_port":
                try:
                    with socket.create_connection(("127.0.0.1", 27015), timeout=self.timeout):
                        return {"connected": True}  # No request on this driver-only connection.
                except ConnectionRefusedError:
                    return {"connected": False}
            return self.usb_devices()
        if kind == "icloud":
            return self.icloud()
        raise ValueError("未知只读探测")

    def usb_devices(self):
        # plist protocol v1 ListDevices only; no Listen/Connect/pair records.
        deadline = time.monotonic() + self.timeout
        payload = plistlib.dumps({"MessageType": "ListDevices", "ClientVersionString": "S2Arm-winenv",
                                  "ProgName": "S2Arm-winenv", "kLibUSBMuxVersion": 3}, fmt=plistlib.FMT_XML)
        def receive(stream, size):
            data = bytearray()
            while len(data) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("USB清单探测超时")
                stream.settimeout(remaining)
                block = stream.recv(size - len(data))
                if not block:
                    raise ProbeError("USB清单帧不完整")
                data.extend(block)
            return bytes(data)
        try:
            with socket.create_connection(("127.0.0.1", 27015), timeout=self.timeout) as stream:
                stream.settimeout(max(0.001, deadline - time.monotonic()))
                stream.sendall(struct.pack("<IIII", len(payload) + 16, 1, 8, 1) + payload)
                length, version, message, tag = struct.unpack("<IIII", receive(stream, 16))
                if not 16 < length <= LIMIT or (version, message, tag) != (1, 8, 1):
                    raise ProbeError("USB清单帧头不可核实")
                data = plistlib.loads(receive(stream, length - 16))
            records = data.get("DeviceList") if isinstance(data, dict) else None
            if not isinstance(records, list) or len(records) > 4096:
                raise ValueError()
            counts = {"USB": 0, "Network": 0}
            for record in records:
                props = record.get("Properties") if isinstance(record, dict) else None
                connection = props.get("ConnectionType") if isinstance(props, dict) else None
                if connection not in counts:
                    raise ValueError()
                counts[connection] += 1
            return counts  # Never return serials, handles, network addresses or raw payload.
        except Exception:
            raise ProbeError("USB清单只读探测未完成（超时、权限或协议）") from None

    def icloud(self):
        if sys.platform != "win32":
            raise ProbeError("此宿主不能核Windows安装登记")
        # Fixed script; no caller text, account fields, profiles or package contents.
        exe = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                  "@(Get-AppxPackage -Name '*iCloud*' -ErrorAction Stop | ForEach-Object { "
                  "[ordered]@{Name=$_.Name;Publisher=$_.Publisher;SignatureKind=[string]$_.SignatureKind;"
                  "Status=[string]$_.Status;PackageFamilyName=$_.PackageFamilyName;Version=[string]$_.Version} "
                  "}) | ConvertTo-Json -Compress -Depth 3")
        raw = self.run([exe, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", script]).decode("utf-8-sig")
        packages = json.loads(raw) if raw.strip() else []
        if isinstance(packages, dict):
            packages = [packages]
        if not isinstance(packages, list) or len(packages) > 128:
            raise ProbeError("安装清单格式不可核实")
        import winreg
        legacy = []
        deadline = time.monotonic() + self.timeout
        for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                           (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                           (winreg.HKEY_CURRENT_USER, 0)):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                     0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with key:
                total = winreg.QueryInfoKey(key)[0]
                if total > 10000:
                    raise ProbeError("安装登记超限")
                for i in range(total):
                    if time.monotonic() >= deadline:
                        raise ProbeError("安装登记只读探测超时")
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                        try:
                            name = winreg.QueryValueEx(entry, "DisplayName")[0]
                            if name != "iCloud":
                                continue
                            publisher = winreg.QueryValueEx(entry, "Publisher")[0]
                        except FileNotFoundError:
                            continue
                        legacy.append({"DisplayName": name, "Publisher": publisher})
        return {"packages": packages, "legacy": legacy}

    @staticmethod
    def service():
        if sys.platform != "win32":
            raise ProbeError("此宿主不能核Windows SCM")
        from ctypes import wintypes
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenSCManagerW.restype = wintypes.HANDLE
        api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenServiceW.restype = wintypes.HANDLE
        api.QueryServiceStatusEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        api.QueryServiceStatusEx.restype = wintypes.BOOL
        api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        api.CloseServiceHandle.restype = wintypes.BOOL

        class Status(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in (
                "serviceType", "state", "controls", "winExit", "serviceExit",
                "checkpoint", "waitHint", "pid", "flags")]

        manager = api.OpenSCManagerW(None, None, 1)
        if not manager:
            raise ctypes.WinError(ctypes.get_last_error())
        service = None
        try:
            service = api.OpenServiceW(manager, "Apple Mobile Device Service", 4)
            if not service:
                error = ctypes.get_last_error()
                if error == 1060:
                    raise FileNotFoundError()
                raise ctypes.WinError(error)
            status, needed = Status(), wintypes.DWORD()
            if not api.QueryServiceStatusEx(service, 0, ctypes.byref(status),
                                            ctypes.sizeof(status), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {"state": int(status.state)}
        finally:
            if service:
                api.CloseServiceHandle(service)
            api.CloseServiceHandle(manager)


class RecoveryContractError(ValueError):
    """An inventory/recovery contract defect, never a guessed job failure."""


@functools.lru_cache(maxsize=1)
@functools.lru_cache(maxsize=1)
def _long_path():
    """pack/orch/winlong.long_path：>240 字符的路径给 Win32 前先加 \\\\?\\（按路径加载，不依赖 sys.path）"""
    spec = importlib.util.spec_from_file_location("_s2a_winenv_winlong", ROOT / "pack/orch/winlong.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.long_path


def _libloc():
    """游戏库位置判法（pack/jobs/libloc.py，与 Mac 预检同一份纯函数）；按路径加载，不依赖 sys.path"""
    spec = importlib.util.spec_from_file_location("_s2a_winenv_libloc", ROOT / "pack/jobs/libloc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _recovery_module():
    spec = importlib.util.spec_from_file_location("_s2a_winenv_errors", ROOT / "pack/jobs/errors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recovery_code(ident, status):
    if not isinstance(ident, str) or not isinstance(status, str):
        raise RecoveryContractError("winenv_recovery_contract: invalid check/status")
    known = {"host", "python", "python_requirements", "metal", "apple_driver",
             "apple_usb", "anisette", "impactor", "disk", "library_cloud", "library_format"}
    python = isinstance(ident, str) and (ident in ("python", "python_requirements") or ident.startswith("python.") and len(ident) > 7)   # 钉版清单缺 = 安装不完整
    bundled = ident.startswith("pack.") and len(ident) > 5   # 出包件：发版随包带（运行时 / 预编件 / 壳），缺或不符 = 安装不完整
    if ident not in known and not python and not bundled:
        raise RecoveryContractError("winenv_recovery_contract: unknown check")
    if status == "ok":
        return None
    if status == "error":
        return "win_check_failed"
    if (python or bundled) and status in ("missing", "mismatch", "unverified"):
        return "tool_missing"
    mapping = {
        ("host", "mismatch"): "win_host_unsupported",
        ("metal", "missing"): "win_metal_missing",
        ("apple_driver", "missing"): "win_apple_devices_missing",
        ("apple_driver", "mismatch"): "win_apple_service_stopped",
        ("apple_usb", "unverified"): "device_not_connected",
        ("anisette", "missing"): "win_icloud_missing",
        ("anisette", "mismatch"): "win_anisette_source",
        ("anisette", "unverified"): "win_anisette_source",
        ("anisette", "user-selected"): "win_anisette_source",
        ("impactor", "missing"): "impactor_missing",
        ("impactor", "mismatch"): "impactor_version_untested",
        ("disk", "mismatch"): "disk_full",
        ("disk", "unverified"): "win_disk_budget_unknown",
        ("library_cloud", "mismatch"): "library_in_cloud",
        ("library_format", "mismatch"): "library_bad_format",
    }
    try:
        return mapping[ident, status]
    except (KeyError, TypeError):
        raise RecoveryContractError("winenv_recovery_contract: unsupported check/status") from None


def _item(ident, name, status, message, source, detected=None, url=None):
    text = _safe(message)
    code, action = _recovery_code(ident, status), None
    if code is not None:
        try:
            module = _recovery_module()
            if code not in module.RECOVERY or not isinstance(module.RECOVERY[code], str) or not module.RECOVERY[code].strip():
                raise ValueError()
            action = module.recovery(code, packaged=True)
        except Exception:
            raise RecoveryContractError("winenv_recovery_contract: recovery source unavailable") from None
    return dict(id=ident, name=name, status=status, ok=status == "ok", message=text,
                source=source, requiredFor=["prepare", "ipa"],
                group="bundled" if name.startswith(("Python", "S2Arm")) else "system",
                code=code, action=action, help=text, detected=_public(detected),
                url=url if status != "ok" else None)   # 玩家自己装的件（许可不许随包）缺 / 不符时的官方下载页，客户端渲染成按钮


def _failure(ident, name, exc, source):
    if isinstance(exc, RecoveryContractError):
        raise exc
    absent = isinstance(exc, FileNotFoundError) or isinstance(exc, ProbeError) and exc.missing
    status = "missing" if absent else "error"
    return _item(ident, name, status, "找不到或无法执行所需工具；请人工检查安装、执行权限与系统策略后重查。" if absent
                 else "只读探测未完成（权限、超时、输出或要求格式）；不能判为可用。", source)


def _pack(request, probe):
    """出包件（随包运行时 / 预编件 / 预编壳 / 字体 / 时区…）：直接用 oneclick_win --check 那一份判定，不另写第二套规则"""
    try:
        rows = probe("pack", target=request.get("target", "ios"), shell=request.get("shell"))
        if not isinstance(rows, list):
            raise ValueError()
        out = []
        for row in rows:
            name, ok, why, status, url = (list(row) + [None, None])[:5]
            if not isinstance(name, str) or name in PACK_OWN:
                continue
            status = status or ("ok" if ok else "missing")
            if status not in ("ok", "missing", "mismatch"):
                raise ValueError()
            out.append(_item("pack." + name, name, status, str(why), PACK, url=url))
        return out
    except RecoveryContractError:
        raise
    except Exception as exc:
        return [_failure("pack.items", "出包件", exc, PACK)]


def _impactor(probe, request):
    """签名装机：玩家自装的官方 Impactor（实测版本）+ 同版本命令行 plumesign（pack/orch/install_win.py 用它）"""
    source = "pack/jobs/impactor.py:status + pack/orch/install_win.py:plumesign_path"
    try:
        st = probe("impactor", resources=request.get("resources"))
        if st["verdict"] == "missing":
            return _item("impactor", "Impactor（签名装机）", "missing", "没找到 Impactor。", source, url=st["download"])
        if st["verdict"] != "ok":
            return _item("impactor", "Impactor（签名装机）", "mismatch",
                         f"现装 {st.get('version')}，实测过的是 {st['tested']}。", source, {"version": st.get("version")}, url=st["download"])
        if not st["plumesign"]:
            return _item("impactor", "Impactor（签名装机）", "missing",
                         f"有 Impactor {st['tested']}，缺同版本命令行 plumesign（同一发布页单独下载，放到 Impactor 同目录）。", source, url=st["download"])
        return _item("impactor", "Impactor（签名装机）", "ok", f"Impactor {st['tested']} + plumesign。", source, {"version": st.get("version")})
    except Exception as exc:
        return _failure("impactor", "Impactor（签名装机）", exc, source)


def _metal(request, probe):
    hint = " 由用户在 https://developer.apple.com/download/ 人工取得官方工具；本工具不下载。"
    try:
        rows = [line.split("\t", 1)[1].strip() for line in _text(METAL_LOCK).splitlines()
                if line.startswith("metal_iphoneos\t")]
        if len(rows) != 1 or not rows[0].startswith("Apple metal version "):
            raise ValueError()
        tested = rows[0]
    except Exception as exc:
        return _failure("metal", "官方Windows Metal编译器", exc, METAL_LOCK)
    try:
        output = probe("metal", executable=request.get("metal"))["output"]
        if not isinstance(output, str) or len(output.encode("utf-8")) > LIMIT:
            raise ValueError()
        versions = re.findall(r"(?m)^Apple metal version ([0-9][A-Za-z0-9_.+() -]{0,240})$", output.strip())
        if len(versions) != 1 or re.search(r"error:", output, re.IGNORECASE):
            return _item("metal", "官方Windows Metal编译器", "missing", "未取得有效Apple metal version。" + hint, METAL_LOCK)
        actual = "Apple metal version " + versions[0]
        message = actual
        if actual != tested:
            message += "；与测试版本 " + tested + " 不同，仅需重编。"
        return _item("metal", "官方Windows Metal编译器", "ok", message, METAL_LOCK,
                     {"version": actual, "testedVersion": tested})
    except Exception as exc:
        if isinstance(exc, ProbeError) and exc.nonzero:
            exc = ProbeError("Metal版本命令失败退出", missing=True)
        c = _failure("metal", "官方Windows Metal编译器", exc, METAL_LOCK)
        c["message"] += hint
        c["help"] = c["message"]
        return c


def _apple(probe):
    source = "Windows SCM + 127.0.0.1:27015"
    ready = False
    try:
        state = probe("service")["state"]
        if type(state) is not int or not 1 <= state <= 7:
            raise ValueError()
        if state == 4:
            connected = probe("usbmux_port")["connected"]
            if type(connected) is not bool:
                raise ValueError()
            ready = connected
            driver = _item("apple_driver", "Apple移动设备支持", "ok" if ready else "missing",
                           "服务运行且本机端口可连接；只证明驱动就绪，不证明USB设备能力。" if ready else
                           "服务运行但本机usbmux端口不通；请人工检查官方Apple设备支持。", source)
        else:
            driver = _item("apple_driver", "Apple移动设备支持", "mismatch",
                           "Apple服务未运行；请人工检查官方安装与服务。", source)
    except Exception as exc:
        driver = _failure("apple_driver", "Apple移动设备支持", exc, source)
    usb = _item("apple_usb", "Apple USB设备能力", "unverified",
                "驱动尚未就绪，未列举USB设备；不能判为可用。", "usbmux v1 ListDevices")
    if ready:
        try:
            counts = probe("usb_devices")
            if (not isinstance(counts, dict) or set(counts) != {"USB", "Network"}
                    or any(type(n) is not int or not 0 <= n <= 4096 for n in counts.values())
                    or sum(counts.values()) > 4096):
                raise ValueError()
            usb = _item("apple_usb", "Apple USB设备能力", "ok" if counts["USB"] else "unverified",
                        "只读清单已发现USB设备；不等配对、信任、签名或装机成功。" if counts["USB"] else
                        "只读清单没有USB设备；网络设备不替代USB，能力仍未核实。",
                        "usbmux v1 ListDevices", counts)
        except Exception as exc:
            usb = _failure("apple_usb", "Apple USB设备能力", exc, "usbmux v1 ListDevices")
    return [driver, usb]


def _icloud(probe):
    observed = probe("icloud")
    if (not isinstance(observed, dict) or not isinstance(observed.get("packages"), list)
            or not isinstance(observed.get("legacy"), list) or len(observed["packages"]) > 128):
        raise ValueError()
    for package in observed["packages"]:
        if not isinstance(package, dict):
            raise ValueError()
        name, publisher = package.get("Name", ""), package.get("Publisher", "")
        family, version = package.get("PackageFamilyName", ""), package.get("Version", "")
        if (isinstance(name, str) and name.endswith(".iCloud")
                and isinstance(publisher, str)
                and re.search(r"(?:^|,)\s*CN=Apple Inc\.(?:,|$)", publisher)
                and re.search(r"(?:^|,)\s*O=Apple Inc\.(?:,|$)", publisher)
                and package.get("SignatureKind") == "Store" and package.get("Status") == "Ok"
                and isinstance(family, str) and family.startswith(name + "_") and len(family) > len(name) + 1
                and isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version)):
            return "ok", {"source": "local", "evidence": "current-user Store package", "version": version}
    # 官方传统安装版（support.apple.com 下载的 iCloud 安装器，非商店版）：卸载登记名称和发布者都对就算装了
    for entry in observed["legacy"]:
        if isinstance(entry, dict) and entry.get("DisplayName") == "iCloud" and entry.get("Publisher") == "Apple Inc.":
            return "ok", {"source": "local", "evidence": "official classic installer registration"}
    if observed["packages"] or observed["legacy"]:
        return "unverified", {"source": "local", "evidence": "installation identity incomplete"}
    return "missing", {"source": "local", "evidence": "no match in queried current-user Appx and uninstall views"}


def _anisette(request, probe):
    config = request.get("anisette", {})
    source, endpoint = config.get("source"), config.get("endpoint", "")
    status, detected = "unverified", None
    message = "自建来源协议尚未交付；不登录、不生成、不发网络健康请求或自动公共回退。"
    if source not in (None, "local", "self-hosted", "public"):
        status, message = "mismatch", "未知anisette来源。"
    elif source == "public" and config.get("allowPublic") is not True:
        status, message = "mismatch", "公共来源必须由用户显式选择。"
    elif source in ("public", "self-hosted"):
        try:
            url = urlsplit(endpoint)
            if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query
                    or url.fragment or not url.netloc or url.port is not None and not 0 < url.port < 65536):
                raise ValueError()
            if source == "public":
                status, message = "user-selected", "用户显选公共备选；不等可信/健康/生成验证。"
                detected = {"source": "public", "selection": "user-selected"}
        except ValueError:
            status, message = "mismatch", "anisette端点格式不可核实，不得含凭据或隐式选择服务。"
    else:
        try:
            status, detected = _icloud(probe)
            message = ("已检测到官方iCloud安装，来源local；不等登录或anisette生成能力验证。"
                       if status == "ok" else "未检出足够的官方iCloud安装身份；仅代表已查询范围，不代表整机不存在。")
            if status == "missing" and source is None:
                status = "unverified"
                message += " 自建来源协议未交付，不自动公共回退。"
        except Exception as exc:
            return _failure("anisette", "anisette来源", exc, "Windows installed-package metadata + #1098")
    return _item("anisette", "anisette来源", status, message, "Windows installed-package metadata + #1098", detected)


def _budget_rules():
    # library imports Mac core/fcntl. Reuse only the fixed repository's pure
    # definitions and original constants, without importing Mac Context/jobs.
    tree = ast.parse(_text(LIBRARY))
    names = {"selected_packages", "_pack_input_bytes", "_pack_direct_bytes", "pack_need_bytes"}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if any(sum(n.name == name for n in functions) != 1 for name in names):
        raise ValueError("共用空间规则缺失或不唯一")
    constants = {"PACK_SMALL_BYTES", "DIRECT_SMALL_BYTES", "DIRECT_LARGE_BYTES"}
    assignments = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)}
            if targets & constants:
                if targets != constants:
                    raise ValueError("共用空间常量格式无法核实")
                assignments.append(node)
    if len(assignments) != 1:
        raise ValueError("共用空间常量缺失或不唯一")
    namespace = {"os": os}
    exec(compile(ast.Module(body=assignments + functions, type_ignores=[]), LIBRARY, "exec"), namespace)
    return namespace


def _catalog_budget(request):
    catalog, gid = request.get("catalog"), request.get("gameId")
    if not catalog or not gid:
        reason = "缺catalog，无法按gameId选包。" if gid else "缺gameId，无法从catalog选包。" if catalog else "未提供出包计划预算。"
        return None, "unverified", reason
    data = _json(_read_file(catalog))
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1
            or not isinstance(data.get("games"), list)):
        raise ValueError("catalog格式无法核实")
    games = [g for g in data["games"] if isinstance(g, dict) and g.get("id") == gid]
    if not games:
        return None, "mismatch", "catalog中未找到gameId，不能计算该游戏的出包预算。"
    if len(games) != 1 or not isinstance(games[0].get("packages"), list):
        raise ValueError("游戏记录重复或包清单不可核实")
    game = games[0]
    for package in game["packages"]:
        if (not isinstance(package, dict) or package.get("type") not in ("base", "update", "dlc")
                or type(package.get("version")) is not int or package["version"] < 0
                or not isinstance(package.get("titleId"), str) or not package["titleId"]
                or not isinstance(package.get("sha256"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", package["sha256"])
                or any(type(package.get(k, 0)) is not int or package.get(k, 0) < 0
                       for k in ("size", "contentBytes"))):
            raise ValueError("包元数据不可核实")
    rules = _budget_rules()
    packages = rules["selected_packages"](game)
    if not packages:
        return None, "unverified", "gameId没有可选包，尚无完整出包预算。"
    need = rules["pack_need_bytes"](packages, windows=True)
    if type(need) is not int or need < 0:
        raise ValueError("共用空间规则结果不可核实")
    return need, None, "按共用选包与出包计划规则计算；压缩包按已登记内容大小，不读取包正文。"


def _disk(request, probe):
    source = "caller:requiredFreeBytes + shutil.disk_usage"
    try:
        target = request.get("targetDir") or (str(Path(request["catalog"]).parent)
                 if request.get("catalog") else str(Path.cwd()))
        free = probe("disk", path=target)["free"]
        if type(free) is not int or free < 0:
            raise ValueError()
        budget = request.get("requiredFreeBytes")
        status, message = None, "按调用者给定的出包计划预算比较；不足时请腾出目标卷空间或选择其它目录。"
        if budget is None:
            if request.get("catalog") or request.get("gameId"):
                source = LIBRARY + ":selected_packages + pack_need_bytes; shutil.disk_usage"
            budget, status, message = _catalog_budget(request)
        if status is None:
            status = "ok" if free >= budget else "mismatch"
        return _item("disk", "目标卷空间", status, message, source,
                     {"freeBytes": free, "requiredFreeBytes": budget})
    except Exception as exc:
        return _failure("disk", "目标卷空间", exc, source)


def _library(request, probe):
    """游戏库位置（11-07 启动器，libloc 与 Mac 同一套判法）：不在云盘同步目录里、所在卷是 NTFS / ReFS。不合格给「换游戏库」（错误码 → 恢复动作）"""
    libloc = _libloc()
    source = "pack/jobs/libloc.py:cloud_folder + format_ok"
    path = request["library"]
    try:
        v = probe("volume", path=path)
        cloud = libloc.cloud_folder(path, system="Windows", home=v["home"], env=v["env"])
        fmt = v["format"]
        out = [_item("library_cloud", "游戏库所在位置", "mismatch" if cloud else "ok",
                     libloc.CLOUD_MESSAGE.format(cloud=cloud) if cloud else "游戏库不在云盘同步目录里。", source, {"cloud": cloud}),
               _item("library_format", "游戏库所在磁盘", "ok" if libloc.format_ok(fmt, system="Windows") else "mismatch",
                     "游戏库所在磁盘格式可用。" if libloc.format_ok(fmt, system="Windows") else libloc.WIN_FORMAT_MESSAGE.format(fmt=fmt.upper()),
                     source, {"format": fmt})]
        return out
    except Exception as exc:
        return [_failure("library_cloud", "游戏库所在位置", exc, source)]


def check(request, *, probe=None):
    """Return inventory only. Caller owns configuration; this function writes no files."""
    _validate(request)
    probe = NativeProbe() if probe is None else probe
    checks = []
    try:
        host = probe("host")
        good = host["system"] == "Windows" and host["bits"] == 64
        checks.append(_item("host", "Windows 64位宿主", "ok" if good else "mismatch",
                            "Windows宿主已识别。" if good else "此宿主不是64位Windows；WSL夹具不等Windows实测。", "#1100"))
    except Exception as exc:
        checks.append(_failure("host", "Windows 64位宿主", exc, "#1100"))
    pins = {}
    try:
        pins = _requirements()
    except Exception as exc:
        checks.append(_failure("python_requirements", "Python钉版清单", exc, RUNTIME))
    try:
        minimum = _python_minimum()
        modules = {name: "PIL" if name == "Pillow" else name for name in pins}
        observed = probe("python", executable=request.get("python", sys.executable), modules=modules)
        version, packages = observed["version"], observed["packages"]
        if (not isinstance(version, list) or len(version) != 3
                or any(type(n) is not int or n < 0 for n in version) or not isinstance(packages, dict)):
            raise ValueError()
        good = tuple(version) >= minimum
        checks.append(_item("python", "Python", "ok" if good else "mismatch",
                            "所选解释器版本 " + ".".join(map(str, version)) + "；要求至少 " + ".".join(map(str, minimum)) +
                            "，不符时请人工安装满足版本的Python并明确选择该解释器。", PREFLIGHT + ":preflight"))
        for dist, want in pins.items():
            data = packages.get(dist, {})
            have = data.get("version") if isinstance(data, dict) else None
            if isinstance(data, dict) and data.get("error"):
                absent = data["error"] == "ModuleNotFoundError" and data.get("missing", True)
                status, detail = ("missing" if absent else "error"), "无法导入"
            elif not isinstance(have, str) or not re.fullmatch(r"[A-Za-z0-9_.+!-]{1,80}", have):
                status, detail = "error", "版本未能核实"
            else:
                status, detail = ("ok" if have == want else "mismatch"), "实际 " + have
            checks.append(_item("python." + dist, "Python / " + dist, status,
                                detail + "；要求 " + want + "。缺失/不符时对所选解释器人工执行 pip install -r " + RUNTIME + "。", RUNTIME))
    except Exception as exc:
        # 依赖都靠这个解释器核实：解释器没起来只报一条，并说清真实原因，不列一串「未能核实」
        reason = getattr(exc, "detail", "")
        item = _failure("python", "Python", exc, PREFLIGHT + " + " + RUNTIME)
        item["probeUnknown"] = True   # 探测没做成不等于坏：不挡流程，真坏时出包会报具体原因
        if reason:
            item["message"] = _safe("S2Arm 自带的 Python 没能运行：" + reason + "。")
        checks.append(item)
    checks.extend(_pack(request, probe))
    checks.append(_impactor(probe, request))
    checks.append(_metal(request, probe))
    checks.extend(_apple(probe))
    checks.append(_anisette(request, probe))
    if request.get("library"):
        checks.extend(_library(request, probe))
    if "requiredFreeBytes" in request or request.get("catalog") or request.get("gameId"):   # 没选游戏 = 预算无从谈起：本项不列，选好游戏开始前带上 gameId 再查
        checks.append(_disk(request, probe))
    for c in checks:   # 玩家自己装的件（许可不许随包）没就绪时给官方下载页
        if not c["ok"] and c["id"] in OFFICIAL and not c.get("url"):
            c["url"] = OFFICIAL[c["id"]]
    for c in checks:
        signing = c["id"] in SIGNING_ONLY
        c["requiredFor"] = ["install"] if signing else ["prepare", "ipa"]
        # 只有「确实缺 / 确实不符」才挡；认不出（unverified / error）和探测失败只提示
        c["blocking"] = not signing and c["status"] in ("missing", "mismatch") and not c.pop("probeUnknown", False)
    return {"schema": 1, "ready": bool(checks) and not any(c["blocking"] for c in checks), "checks": checks}


def render(report):
    heading = "环境就绪" if report["ready"] else "环境未就绪"
    return heading + "\n" + "\n".join(
        f"[{c['status']}] {c['name']}：{c['message']}（要求来源：{c['source']}）" for c in report["checks"])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Windows出包环境只读自检；不下载安装")
    parser.add_argument("--request", help="启动器提供的普通JSON配置文件")
    parser.add_argument("--json", action="store_true", help="输出UTF-8 JSON而非人类摘要")
    args = parser.parse_args(argv)
    try:
        request = _json(_read_file(args.request, LIMIT)) if args.request else {}
        report = check(request)
    except RecoveryContractError:
        report = {"schema": 1, "ready": False, "checks": [],
                  "error": {"code": "winenv_recovery_contract", "message": "环境检查错误码合同无法核实，未回落到通用任务错误。"}}
        print(json.dumps(report, ensure_ascii=False) if args.json else report["error"]["message"])
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        report = {"schema": 1, "ready": False, "checks": [],
                  "error": {"code": "invalid_request", "message": "请求配置不可读或格式错误，未执行出包操作。"}}
        print(json.dumps(report, ensure_ascii=False) if args.json else report["error"]["message"])
        return 2
    print(json.dumps(report, ensure_ascii=False) if args.json else render(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
