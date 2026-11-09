"""Managed game catalogue, immutable container imports and player data.

Storage/interaction helpers adapted from LumaDock under MIT.
"""
from __future__ import annotations
import collections
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from core import Failure, atomic, contained, digest, prune_jobs, read
import toolchain
import winproc

DIAGNOSTIC_LOG_EDGE = 16000          # 长日志保留的开头、结尾字节数
DIAGNOSTIC_LOG_BEATS = 64000         # 中间心跳行最多保留这么多字节（10 秒一行，约一小时）
DIAGNOSTIC_LOG_SCAN = 64 << 20       # 只在结尾前这么大的范围里找心跳行
DIAGNOSTIC_LOG_CHUNK = 1 << 20


def diagnostic_log_excerpt(source, size):
    """诊断包里的日志：短的整份；长的留开头（起局配置）+ 中间的心跳行（卡顿、内存走势）+ 结尾。总量有界。"""
    edge = DIAGNOSTIC_LOG_EDGE
    if size <= 2 * edge:
        return source.read(2 * edge)
    head = source.read(edge)
    start = max(edge, size - edge - DIAGNOSTIC_LOG_SCAN)
    source.seek(start)
    beats, used, partial, left = collections.deque(), 0, b"", size - edge - start
    while left > 0:
        chunk = source.read(min(DIAGNOSTIC_LOG_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        lines = (partial + chunk).split(b"\n")
        partial = lines.pop()
        if len(partial) > DIAGNOSTIC_LOG_CHUNK:   # 没有换行的超长段不是心跳行，也别让它占内存
            partial = b""
        for line in lines:
            if "[心跳]".encode() in line and len(line) < 4096:
                beats.append(line)
                used += len(line) + 1
                while used > DIAGNOSTIC_LOG_BEATS:
                    used -= len(beats.popleft()) + 1
    source.seek(size - edge)
    tail = source.read(edge)
    gap = lambda text: f"\n…（{text}）…\n".encode()
    return head + gap(f"日志共 {size} 字节，中间只保留 {len(beats)} 行心跳") + b"\n".join(beats) + gap("以下是日志结尾") + tail


DIAGNOSTIC_JOB_LIMIT = 4 << 20


# 游戏原始包：安装包 NSP 与卡带镜像 XCI，及它们的压缩版 NSZ / XCZ（nx_unpack 按内容认格式、还原压缩的 NCA）；
#   Switch 2 的 NUP 容器（.nup，一个文件装本体 + 更新 + DLC，不加密、不要 prod.keys）由 pack/ns2_nup.py 认与展开，不走 nx_unpack；
#   已解包的游戏目录（Switch 2 导出 dump / S2Arm 解包 extract / 通用解包 loose）也可直接导入，不要密钥
SOURCE_SUFFIXES = (".nsp", ".xci", ".nsz", ".xcz", ".nup")
NUP_SUFFIX = ".nup"


def engine_tool(c, name):
    """引擎里的单文件 Python 工具（pack/<name>.py）按路径载入：作业层与一键出包用同一份实现（Switch 2 的 ns2_nup / ns2_dump），不另写一套"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("s2a_" + name, c.engine / "pack" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_nup(c, path):
    """真 NUP（pack/ns2_nup.py --detect 同一判法 find_nup：按文件头魔数，不看扩展名）"""
    return engine_tool(c, "ns2_nup").find_nup(str(path)) is not None


def is_ns2_dump(c, path):
    """Switch 2 导出目录（pack/ns2_dump.py --detect：有 base/exefs/main.npdm）"""
    dump = engine_tool(c, "ns2_dump")
    return dump.find_dump(str(path)) is not None


def is_extracted_game(d):
    """这个目录本身是不是一款解包好的游戏：Switch 2 导出（base/exefs/main.npdm）、S2Arm 解包（exefs/main + unpack.tsv）、通用解包（exefs/main + main.npdm + romfs/）"""
    return ((d / "base" / "exefs" / "main.npdm").is_file()
            or ((d / "exefs" / "main").is_file() and (d / "unpack.tsv").is_file())
            or ((d / "exefs" / "main").is_file() and (d / "exefs" / "main.npdm").is_file() and (d / "romfs").is_dir()))


def extract_inspect(c, source, icon_dir):
    """解包目录 → 与 inspect / nup_inspect 同形的包元数据。三种 format（已在 import_games 按判据分开）：
    a. dump（Switch 2 导出）: 有 base/exefs/main.npdm
    b. extract（S2Arm 解包）: 有 exefs/main 与 unpack.tsv
    c. loose（通用解包）: 有 exefs/main、exefs/main.npdm 和 romfs/ 目录
    程序号：dump / loose 从 main.npdm 的 ACI0 读（复用 ns2_dump.read_tid），extract 取 unpack.tsv。
    名称：extract 取 unpack.tsv，其余有 control.nacp 用它（dump 取 ctl/，loose 取 exefs/），没有用目录名。
    只返回本体（type=base），version=0，不哈希整个目录、只哈希 exefs/main + exefs/main.npdm（dump 取 base/exefs 下同名文件）"""
    source = Path(source)
    dump = engine_tool(c, "ns2_dump")

    # 判定 format 与读程序号
    if (source / "unpack.tsv").is_file():
        # extract: S2Arm 解包目录，从 unpack.tsv 读
        fmt = "extract"
        try:
            tsv = (source / "unpack.tsv").read_text(encoding="utf-8")
            for line in tsv.splitlines():
                if line.startswith("title_id\t"):
                    tid = int(line.split("\t")[1], 16)
                    break
            else:
                raise ValueError("unpack.tsv 没有 title_id")
        except (OSError, ValueError, IndexError) as e:
            raise Failure(f"读不出解包目录的元数据（{source.name}）：{e}", code="content_invalid") from None
        main_path = source / "exefs" / "main"
        npdm_path = source / "exefs" / "main.npdm"
    elif (source / "base" / "exefs" / "main.npdm").is_file():
        # dump: Switch 2 导出目录
        fmt = "dump"
        try:
            tid = dump.read_tid(str(source / "base" / "exefs" / "main.npdm"))
        except (OSError, ValueError) as e:
            raise Failure(f"读不出 Switch 2 导出目录的程序号（{source.name}）：{e}", code="content_invalid") from None
        main_path = source / "base" / "exefs" / "main"
        npdm_path = source / "base" / "exefs" / "main.npdm"
    else:
        # loose: 通用解包目录
        fmt = "loose"
        try:
            tid = dump.read_tid(str(source / "exefs" / "main.npdm"))
        except (OSError, ValueError) as e:
            raise Failure(f"读不出解包目录的程序号（{source.name}）：{e}", code="content_invalid") from None
        main_path = source / "exefs" / "main"
        npdm_path = source / "exefs" / "main.npdm"

    # 读名称与显示版本
    if fmt == "extract" and (source / "unpack.tsv").is_file():
        try:
            tsv = (source / "unpack.tsv").read_text(encoding="utf-8")
            name, name_zh, display = "", "", ""
            for line in tsv.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    if parts[0] == "name":
                        name = parts[1]
                    elif parts[0] == "name_zh":
                        name_zh = parts[1]
                    elif parts[0] == "display_version":
                        display = parts[1]
            name = name_zh or name or source.name
        except (OSError, UnicodeDecodeError):
            name, display = source.name, ""
    else:
        # dump 取 ctl/control.nacp，loose 取 exefs/control.nacp
        nacp_path = source / "ctl" / "control.nacp" if fmt == "dump" else source / "exefs" / "control.nacp"
        if nacp_path.is_file():
            try:
                nacp = nacp_path.read_bytes()
                if len(nacp) == 0x4000:
                    name = dump.nacp_title(nacp, 0) or dump.nacp_title(nacp, 14) or dump.nacp_title(nacp, 13) or source.name
                    display = nacp[0x3060:0x3070].split(b"\0")[0].decode("ascii", "replace")
                else:
                    name, display = source.name, ""
            except (OSError, UnicodeDecodeError):
                name, display = source.name, ""
        else:
            name, display = source.name, ""

    # 封面：dump 取 ctl/icon.jpg，loose 取 exefs/icon.jpg
    icon_path = source / "ctl" / "icon.jpg" if fmt == "dump" else source / "exefs" / "icon.jpg"
    if icon_path.is_file():
        try:
            (Path(icon_dir) / f"{tid:016X}.jpg").write_bytes(icon_path.read_bytes())
        except OSError:
            pass

    # 哈希 exefs/main + exefs/main.npdm（不哈希整个 romfs）
    h = hashlib.sha256()
    for p in [main_path, npdm_path]:
        if p.is_file():
            h.update(p.read_bytes())
    sha = h.hexdigest()

    total = _part_bytes([source])   # 目录内文件的实占字节

    version = 0
    if fmt == "extract":   # 解包目录 unpack.tsv 的 update 列是合并进来的更新版本号；没有显示版本时界面用它
        cells = dict(l.split("\t", 1) for l in (source / "unpack.tsv").read_text(encoding="utf-8").splitlines() if "\t" in l)
        version = int(cells.get("update") or 0) if (cells.get("update") or "0").isdigit() else 0
    return [dict(titleId=f"{tid:016X}", baseId=f"{tid:016X}", type="base", version=version, name=name,
                 display=display, guestIsa="unknown", builds={}, contentBytes=total, format=fmt, sha256=sha)]


def nup_inspect(c, source, icon_dir):
    """NUP 容器 → 与读取器 --inspect 同形的包元数据（本体 / 最新更新 / 每个 DLC 各一条，同一原件；出包时 build.ensure_extract 整个容器一起展开）。
    程序号、更新版本、DLC 由 ns2_nup.scan 认（同一键出包）；标题与显示版本取容器里的控制分区（NACP，后面的版本覆盖前面的），没带就用文件名去掉「 [程序号][版本]」
    （同 pack/oneclick/oneclick.sh：ns2_dump 没有控制分区时拿它当标题）。程序位数不在这里判：出包解包后由 nx_unpack --npdm-isa 判（全仓唯一判法）"""
    nup, dump = engine_tool(c, "ns2_nup"), engine_tool(c, "ns2_dump")
    try:
        with nup.Nup(str(source)) as n:
            app, base, vers, dlc = nup.scan(n)
            ver = max(vers, default=0)
            meta = {}
            for k in sorted((k for k in n.packages if k.kind in (nup.BASE, nup.UPDATE)), key=lambda k: (k.kind, k.version)):
                meta.update(k.meta())
            content = sum(n.tree_bytes(k.root) for k in [base, vers.get(ver), *dlc.values()] if k is not None)
    except (nup.Error, OSError, ValueError) as e:
        raise Failure(f"读不出这个 NUP 容器（{Path(source).name}）：{e}", code="content_invalid") from None
    nacp = meta.get(nup.T_NACP) if len(meta.get(nup.T_NACP) or b"") == 0x4000 else None
    name = ((dump.nacp_title(nacp, 0) or dump.nacp_title(nacp, 14) or dump.nacp_title(nacp, 13)) if nacp else None) \
        or re.sub(r" \[.*$", "", Path(source).stem)
    display = nacp[0x3060:0x3070].split(b"\0")[0].decode("ascii", "replace") if nacp else ""
    icon = meta.get(nup.T_ICON) or nup.localized_icon(meta.get(nup.T_LICONS, b""))
    if icon:
        (Path(icon_dir) / f"{app:016X}.jpg").write_bytes(icon)
    common = dict(baseId=f"{app:016X}", display=display, guestIsa="unknown", builds={}, contentBytes=content, format="nup")
    out = [dict(common, titleId=f"{app:016X}", type="base", version=0, name=name)]
    if ver:
        out.append(dict(common, titleId=f"{app | 0x800:016X}", type="update", version=ver, name=name))
    out += [dict(common, titleId=f"{tid:016X}", type="dlc", version=k.version, name="") for tid, k in sorted(dlc.items())]
    return out


def volume_format(path):
    """游戏库所在卷的文件系统（apfs / exfat / msdos / hfs …，小写）；读不到给空串。路径还不存在时看最近一级已存在的上级目录。
    df 取挂载点，再从 mount 的列表里取这一挂载点的格式（diskutil info 只认挂载点 / 设备，不认普通文件夹）"""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    if os.name == "nt":   # Windows（11-07 启动器）：卷根 GetVolumePathNameW → 文件系统名 GetVolumeInformationW（ntfs / refs / exfat / fat32 …）
        return _win_volume_format(str(p))
    try:
        mount_point = subprocess.run(["/bin/df", "-P", str(p)], capture_output=True, text=True, timeout=20).stdout.splitlines()[-1].split(None, 5)[5]
        for line in subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=20).stdout.splitlines():
            m = re.match(r".+? on (.+) \(([^,)]+)", line)
            if m and m.group(1) == mount_point:
                return m.group(2).strip().lower()
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pass
    return ""


# 游戏原件放哪块盘都行（exFAT 外接盘也行）：导入只记原件的位置与内容哈希，不复制、不改、不删，出包时原地只读（11-02 维护者定）。
# 游戏库 = S2Arm 的工作数据（清单、解包与出包产物、缓存、存档），要放 APFS：播种、发布要原子的「不覆盖改名」和克隆，exFAT / FAT 不支持。
#   启动器选到非 APFS 的文件夹不拦：说明后游戏库放默认位置（内置盘），那块盘上的游戏照样原地导入；后端 setup 这里是最后一道（直接调接口的也拦）
from libloc import LIBRARY_FORMAT_MESSAGE   # 文案单一来源在 libloc（Mac / Windows 共用的游戏库位置检查）


def source_path(c, package):
    """包的原件位置：导入时记的绝对路径（原件原地只读）；相对路径是 0.1.0-beta.3 及以前复制进游戏库 sources/ 的托管副本（contained 防越界）"""
    path = Path(package["path"])
    return path if path.is_absolute() else contained(c.library, c.library / path)


def stage_sources(c, packages, stage):
    """出包要的原件目录：每个原件软链进来（按内容哈希命名，去重整合版一个 xci 列两次的情况），不复制——原件在外接 exFAT 盘上也不用整份拷。
    解包目录用目录名后缀"""
    stage.mkdir(parents=True, exist_ok=True)
    for p in {p["sha256"]: p for p in packages}.values():
        source = source_path(c, p)
        if source.is_dir():
            os.symlink(source, stage / (p["sha256"] + "_" + source.name))
        else:
            os.symlink(source, stage / (p["sha256"] + source.suffix.lower()))


def _win_volume_format(path):
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        root = ctypes.create_unicode_buffer(1024)
        if not k32.GetVolumePathNameW(ctypes.c_wchar_p(winproc.win_path(path)), root, 1024):
            return ""
        fs = ctypes.create_unicode_buffer(64)
        if not k32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 64):
            return ""
        return fs.value.strip().lower()
    except (OSError, AttributeError, ValueError):
        return ""


def require_apfs(path):
    """游戏库所在卷的格式：Mac 要 APFS（原错误码 library_not_apfs），Windows 要 NTFS / ReFS（library_bad_format，libloc.format_ok）；读不到格式不拦"""
    import libloc
    fmt = volume_format(path)
    if os.name == "nt":
        if not libloc.format_ok(fmt, system="Windows"):
            raise Failure(libloc.WIN_FORMAT_MESSAGE.format(fmt=fmt.upper()), code="library_bad_format")
        return fmt
    if fmt and fmt != "apfs":
        raise Failure(LIBRARY_FORMAT_MESSAGE.format(fmt=fmt.upper()), code="library_not_apfs")
    return fmt


def require_local(path):
    """游戏库不能在云盘同步目录里（11-07 启动器，libloc.cloud_folder）：iCloud Drive / OneDrive / Dropbox 等会上传几十 GB 中间产物、
    换成占位文件，出包中途读不到。预检已经提示（toolchain.preflight / winenv），这里是最后一道（直接调接口的也拦）"""
    import libloc
    system = "Windows" if os.name == "nt" else "Darwin"
    cloud = libloc.cloud_folder(str(path), system=system, home=str(Path.home()), env=dict(os.environ), exists=os.path.exists)
    if cloud:
        raise Failure(libloc.CLOUD_MESSAGE.format(cloud=cloud), code="library_in_cloud")


def setup(c):
    c.begin("创建游戏库")
    if c.library == Path(c.library.anchor) or c.library == Path.home() or c.library == c.engine:
        raise Failure("请选择独立的游戏库文件夹。")
    require_apfs(c.library)
    require_local(c.library)
    c.library.mkdir(parents=True, exist_ok=True)
    marker = c.library / ".s2arm-library.json"
    if marker.exists():
        info = read(marker)
        if info.get("schema") != 1 or not info.get("id"):
            raise Failure("游戏库标识不完整。")
    else:
        if any(p.name != ".DS_Store" for p in c.library.iterdir()):
            raise Failure("首次设置请选择空文件夹，已有文件保持原样。")
        info = dict(schema=1, id=str(uuid.uuid4()))
        atomic(marker, info)
        atomic(c.library / "catalog.json", dict(schema=1, games=[]))
    return dict(library=str(c.library), libraryId=info["id"])


def validate_keys(path):
    if Path(path).stat().st_size > 1 << 20:
        raise Failure("密钥文件过大。", code="keys_invalid")
    entries = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeError:
        raise Failure("密钥文件格式无效，请选择 prod.keys 文本文件。", code="keys_invalid") from None
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_]+)\s*=\s*((?:0x)?[a-fA-F0-9]{2,2048})", line)
        if not m or len(m[2].removeprefix("0x")) % 2:
            raise Failure("密钥文件格式无效，请选择 prod.keys 文本文件。", code="keys_invalid")
        if m[1] in entries and entries[m[1]] != m[2]:
            raise Failure("密钥文件存在冲突条目。", code="keys_invalid")
        entries[m[1]] = m[2].removeprefix("0x")
    if len(entries.get("header_key", "")) != 64:
        raise Failure("缺少有效的 header_key。", code="keys_invalid")
    return len(entries)


def keys(c):
    c.begin("导入玩家密钥")
    source = Path(c.r["path"])
    count = validate_keys(source)
    c.keys.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = c.keys.with_name(c.task + ".candidate")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(source.read_bytes())
        verified = False
        with c.lock("execution"), c.lock():
            candidate = next((source_path(c, p) for g in c.catalog()["games"] for p in selected_packages(g)
                              if p["type"] == "base" and p.get("format") not in ("nup", "dump", "extract", "loose") and source_path(c, p).is_file()), None)   # NUP 与解包目录不加密，验不了密钥
            if candidate:
                with tempfile.TemporaryDirectory(dir=c.private) as icons:
                    diagnostics_before = len(c.job["diagnostics"])
                    try:
                        inspect(c, candidate, icons, keyfile=temporary)
                    except Failure as error:
                        if error.code != "content_truncated":
                            raise
                        del c.job["diagnostics"][diagnostics_before:]
                    else:
                        verified = True
            c.check()
            os.replace(temporary, c.keys)
    finally:
        temporary.unlink(missing_ok=True)
    atomic(c.private / "keys/status.json", dict(ready=True, verified=verified, imported=time.time()))
    return dict(entries=count, verified=verified, message="已验证解密能力" if verified else "格式通过，首次读取游戏时验证解密能力")


KEYS_CANNOT_DECRYPT = ("无法读取游戏内容：可能是文件不完整、缺票据或密钥不匹配。请核对原始文件；"
                       "若已确认 Switch 密钥版本不足，再重新导出，到「设置 → 玩家密钥」重新导入。")


def inspect(c, source, icon_dir, keyfile=None):
    keyfile = keyfile or c.keys
    validate_keys(keyfile)
    lines = []
    try:
        c.run(["dotnet", toolchain.reader(c), keyfile, "--inspect", source, icon_dir],
              c.private / "logs" / (c.task + "-inspect.log"), on_line=lines.append)
    except Failure as error:
        if error.code not in ("tool_exit", "job_failed"):
            raise
        if "【内容截断】" in lines:
            raise Failure("游戏文件不完整，部分内容超出文件末尾。", code="content_truncated") from None
        if any(line.startswith("【原因 ticket_missing】") for line in lines):
            raise Failure("缺少内容对应的票据或标题密钥，请检查源文件是否完整。", code="ticket_missing") from None
        if any(line.startswith("【原因 keys_missing】") for line in lines):
            raise Failure("现有 prod.keys 缺少读取此文件所需的密钥。", code="keys_outdated") from None
        if any("could not be decrypted" in line or "Check keys" in line for line in lines):
            raise Failure(KEYS_CANNOT_DECRYPT, code="content_unreadable") from None
        raise
    if "【内容截断】" in lines:
        raise Failure("游戏文件不完整，部分内容超出文件末尾。", code="content_truncated")
    try:
        output = json.loads(next(line for line in reversed(lines) if line.startswith("{")))
        if output.get("schema") != 1 or not output["packages"]:
            raise ValueError()
        for package in output["packages"]:
            if not re.fullmatch(r"[0-9A-F]{16}", package["titleId"]) or not re.fullmatch(r"[0-9A-F]{16}", package["baseId"]):
                raise ValueError()
            if package["type"] not in ("base", "update", "dlc") or not isinstance(package["version"], int):
                raise ValueError()
        return output["packages"]
    except (KeyError, ValueError, StopIteration, TypeError):
        raise Failure("内容读取器返回的元数据不完整。", code="content_invalid") from None


def selected_packages(game):
    bases = [p for p in game["packages"] if p["type"] == "base"]
    updates = [p for p in game["packages"] if p["type"] == "update"]
    result = ([max(bases, key=lambda p: p["version"])] if bases else [])
    if updates:
        result.append(max(updates, key=lambda p: p["version"]))
    dlc = {}
    for p in game["packages"]:
        if p["type"] == "dlc" and (p["titleId"] not in dlc or dlc[p["titleId"]]["version"] < p["version"]):
            dlc[p["titleId"]] = p
    return result + [dlc[k] for k in sorted(dlc)]


def input_identity(game):
    return hashlib.sha256(json.dumps([(p["titleId"], p["version"], p["sha256"])
                                     for p in selected_packages(game)], separators=(",", ":")).encode()).hexdigest()


def _volume(path):
    # 给提示用的磁盘名：/Volumes/<名> 下就是那块外接盘，否则是系统盘
    parts = Path(path).resolve().parts
    return parts[2] if len(parts) > 2 and parts[1] == "Volumes" else "系统盘"


# 预检里没算进门槛、也量不准的开销（只列出来给玩家看，不编常数）
SPACE_UNKNOWN = [
    dict(id="shader_cache", role="library", label="着色器缓存", note="着色器缓存另占若干 GB，随游戏与设备而变，未量化，不计入门槛。"),
]


# 双轨包多占的空间（10-24 动森实测，RomFS 10.7 GB）：直连库中间缓存（S2A_DIRECT_CACHE，跨次复用）+ 包里的直连库，按游戏体积换算
# 出包要多少盘（11-08 维护者）：不看这款做没做过，第一次出包也算得准，只有一个数。
#   解包 = 原包 × 1.1（解包目录已完整在盘上时算 0）；Mac 的资源包是 APFS 克隆不另算，Windows（NTFS 无克隆）再加一份原包 × 1.1；
#   直连（翻译缓存 + 着色器库 + 收尾临时，取上限）：原包 < 3 GB 记 5 GB，其余 15 GB（最坏样本巫师3 缓存 7.5 + 库 1.5 × 2 份 + 收尾临时约 4）
PACK_SMALL_BYTES, DIRECT_SMALL_BYTES, DIRECT_LARGE_BYTES = int(3e9), int(5e9), int(15e9)


def _pack_input_bytes(packages):
    # NSZ/XCZ按解包内容与原文件取大值；同一sha的原件只计一次。
    return sum(max(p.get("size", 0), p.get("contentBytes", 0))
               for p in {p["sha256"]: p for p in packages}.values())


def _pack_direct_bytes(size):
    return DIRECT_SMALL_BYTES if size < PACK_SMALL_BYTES else DIRECT_LARGE_BYTES


def pack_need_bytes(packages, unpacked=False, windows=None):
    """这次出包要多少盘；packages 已按 selected_packages 选定。unpacked：解包目录已完整在盘上。windows 不给就按当前系统。"""
    size = _pack_input_bytes(packages)
    copy = size * 11 // 10
    windows = os.name == "nt" if windows is None else windows
    return (0 if unpacked else copy) + (copy if windows else 0) + _pack_direct_bytes(size)


# 出包实测账（11-07 联合开发者旷野之息：外接盘 NX 剩 45 GB，经验门槛要 4x GB 拦下）：每次 Mac 打包 / 打 iPhone 包成功后，
#   量这一款在游戏库所在卷上的整套占用，追加一行到 游戏库/space-usage.tsv（取最后一行，同 tools/disk_admit.py 的 est.tsv）；
#   预检先用同款实测、再用同大小档实测比例 × 本款大小，都没有才退回经验规则。余量 × 1.1 同 维护方工具 need_bytes
SPACE_LEDGER = "space-usage.tsv"
CLONED_DIRS = ("romfs", ".romfs_store")
SPACE_LEDGER_FIELDS = ("time", "gameId", "target", "direct", "inputBytes", "footprintBytes", "name", "format")
SPACE_LEDGER_FORMAT = "2"   # 2 起 RomFS 只算一份、直连缓存是压缩后的（DSZ1）；旧行偏大约 5 倍（火焰纹章记 152 GB，实占约 32 GB），不用


def _space_target(c):
    """实测账按目标分：打 iPhone 包 ipa，Mac 打包（build）mac——两边落盘的东西不同（Mac 运行版本整份落 RomFS，iPhone 出 IPA + 资源包）"""
    return "ipa" if getattr(c, "operation", "") == "ipa" else "mac"


def disk_bytes(roots, device, skip_names=(), skip_under=()):
    """这些目录在 device 卷上实际分配的字节：按 (卷, inode) 去重（硬链接只算一次；多个根互相包含也只算一次），
    不跟软链（原件软链、@现行 指向）、不跨到别的卷（私有目录在内置盘时不计入外接盘游戏库的账）。
    APFS 克隆（clonefile / cp -c）两份各有 inode、stat 看不出共享块，这里照两份算——量出来偏大、预算偏保守"""
    seen, total, stack = set(), 0, [os.fspath(r) for r in roots]
    while stack:
        path = stack.pop()
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if st.st_dev != device or stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) in seen:
            continue
        if os.path.basename(path) in skip_names and any(path.startswith(u + os.sep) for u in skip_under):
            continue
        seen.add((st.st_dev, st.st_ino))
        blocks = getattr(st, "st_blocks", None)
        total += blocks * 512 if blocks is not None else st.st_size
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(path) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                pass
    return total


def pack_footprint(c, gid):
    """这一款出包在游戏库所在卷上的整套占用：iPhone 输出（产物根 / IPA / 资源包）、共用解包与 Mac 构建缓存（cache/<程序号>）、
    Mac 运行版本（games/<程序号>），再加私有目录里本款的出包缓存（直连 / 翻译…，game_caches）——后者只在和游戏库同一卷时才算进来。
    不含各款共用的编库缓存（air / pso / gal_dl_cache）、存档、运行时着色器缓存（runtime-cache，玩的时候才长）"""
    shared = {c.work_root / "cache" / "air", c.work_root / "cache" / "pso", c.work_root / "run" / "gal_dl_cache"}
    roots = [c.library / "iPhone" / gid, c.library / "cache" / gid, c.library / "games" / gid]
    roots += [p for p in game_caches(c, gid) if p not in shared]
    # APFS 上产物根、资源包、Mac 运行版本里的这两种目录都是解包结果的克隆，不占新空间，只算解包那一份（stat 看不出共享块）；Windows（NTFS 无克隆）照实算
    clones = [] if os.name == "nt" else [str(c.library / "iPhone" / gid), str(c.library / "games" / gid)]
    return disk_bytes(roots, c.library.stat().st_dev, skip_names=CLONED_DIRS, skip_under=clones)


def record_pack_usage(c, game):
    """出包成功后（清中间产物之前）记一行实测：程序号、目标、直连与否、输入大小、整套占用。量不出来不影响这次出包，只少一行账"""
    try:
        size = _pack_input_bytes(selected_packages(game))
        if size <= 0:
            return None
        footprint = pack_footprint(c, game["id"])
        path = c.library / SPACE_LEDGER
        name = re.sub(r"[\t\r\n]+", " ", game.get("name") or "")
        row = [time.strftime("%Y-%m-%dT%H:%M:%S"), game["id"], _space_target(c), "1", str(size), str(footprint), name, SPACE_LEDGER_FORMAT]
        with path.open("a", encoding="utf-8") as f:
            if f.tell() == 0:
                f.write("# " + "\t".join(SPACE_LEDGER_FIELDS) + "\n")
            f.write("\t".join(row) + "\n")
        return footprint
    except (OSError, KeyError, TypeError, ValueError):   # 清单记录不全 / 盘读不了：只少这一行账
        return None


def _gb(n):
    return f"{n / 1e9:.0f} GB" if n >= 10e9 else f"{n / 1e9:.1f} GB"


def _unpacked_on_disk(c, game):
    """这款的解包目录已完整在盘上（物理事实，不是历史记录）"""
    from build import extract_valid   # build 顶层 import 本模块，这里函数内取
    return "id" in game and extract_valid(c, game)


def space_budget(c, game):
    """打包前的空间预算（纯计算，不拦）：只有一个数，出包页显示和拦截都用它；公式见 pack_need_bytes。space-usage.tsv 只记账校准常量，不进判断。只写盘名，不写绝对路径。"""
    import shutil as _sh
    packages = selected_packages(game)
    size = _pack_input_bytes(packages)
    unpacked = _unpacked_on_disk(c, game)
    need, free = pack_need_bytes(packages, unpacked=unpacked), _sh.disk_usage(c.library).free
    note = f"解包 {'已在盘上' if unpacked else _gb(size * 11 // 10)}，直连着色器上限 {_gb(_pack_direct_bytes(size))}"
    volumes = [dict(role="library", volume=_volume(c.library), needBytes=need, freeBytes=free, enough=free >= need,
                    note=note, unknown=["shader_cache"], basis="rule")]
    return dict(gameBytes=size, volumes=volumes, unknown=[dict(u) for u in SPACE_UNKNOWN])


def check_pack_space(c, game, *, compiler_policy=None):
    """打包前的磁盘预检（Mac 打包与打 iPhone 包共用）：IPA 可先按已有保留规则尝试回收；发最终 space 事件（存任务文件），再按预算拦。
    请求带 spaceOverride: true（玩家在失败页点「仍然继续」）：不拦，只记一条警告；真写满由出包中途的磁盘写满识别报错"""
    budget = space_budget(c, game)
    cache_result = None
    if compiler_policy is not None:
        from cache_retention import trim
        # 缓存只释放 private 所在卷；不同卷的 library 不借用这笔 free。
        device = c.private.stat().st_dev
        relevant = budget["volumes"] if c.library.stat().st_dev == device else []
        target = max([compiler_policy["min_free_bytes"], *(v["needBytes"] for v in relevant)])
        if any(not v["enough"] for v in budget["volumes"]):
            # 若不足的是另一卷，只统计缓存，不为它删除本卷冷条目。
            floor = target if any(not v["enough"] for v in relevant) else 0
            cache_result = trim(c.work_root, current_gid=game["id"],
                                **dict(compiler_policy, min_free_bytes=floor))
            budget = space_budget(c, game)
            budget["compilerCache"] = cache_result
        budget["compilerMinFreeBytes"] = target
    override = getattr(c, "r", {}).get("spaceOverride") is True
    if override and any(not v["enough"] for v in budget["volumes"]):
        budget["override"] = True   # 失败页据此不再给「仍然继续」（这次已经是继续过的）
    c.emit("space", "空间预检", space=budget)
    for v in budget["volumes"]:
        if v["enough"]:
            continue
        need, free = v["needBytes"], v["freeBytes"]
        head = f"磁盘空间不够：这次出包预计要 {need / 1e9:.0f} GB，现在只剩 {free / 1e9:.0f} GB。"
        if override:
            c.diagnostic(head + "已按你的选择仍然继续；中途真写满会停下报错，游戏原件和存档不受影响。", code="disk_full", severity="warning")
            continue
        raise Failure(head + "清出空间后点「重试」，或点「仍然继续」（预计偏保守时可以试；真写满会停下报错，游戏原件和存档不受影响）。", code="disk_full")

    return budget


def guest_isa(selected):
    # 真正跑的程序的指令集：最新更新的 ExeFS 是完整的，读得出就以它为准；读不出（unknown）才看本体。
    # 有的游戏本体 32 位、更新后 64 位（马车8 1.0 AArch32 → 4.0.1 AArch64，10-23 一键出包曾按本体误拒）
    upd = [p for p in selected if p["type"] == "update" and p.get("guestIsa") in ("aarch64", "aarch32")]
    if upd:
        return upd[0]["guestIsa"]
    return next((p.get("guestIsa") for p in selected if p["type"] == "base"), "unknown")


def requirements(c, game):
    return [str(error) for error in requirement_errors(c, game)]


def require_game(c, game):
    errors = requirement_errors(c, game)
    if errors:
        for error in errors:
            c.report_failure(error)
        raise errors[-1]
    wait_readable(c, [source_path(c, p) for p in selected_packages(game)])


def wait_readable(c, paths, poll=5.0):
    """开工前把每个原件读一下（S2Arm 10-31 出包管线深修 4.5：游戏库在「下载」/ 外接盘时，第一次读会弹系统的文件访问授权框，
    框被别的窗口挡住，读就一直停在系统调用里——零 CPU、零磁盘，界面停在某一步不动；10-31 一位开发者冷出勇者斗恶龙 XI S 停了 15 分钟以上）。
    每个原件在后台线程试读开头 4 KB；每 poll 秒没读到就在界面上说「在等什么、等了多久」；系统拒绝（权限错误）直接报出去哪里打开。
    解包目录（format=dump/extract/loose）：dump 读 base/exefs/main，其余读 exefs/main"""
    import threading
    for path in paths:
        box = {}
        # 判断是否解包目录：目录且有 exefs/main 或 base/exefs/main
        probe_path = path
        if path.is_dir():
            if (path / "base" / "exefs" / "main").is_file():
                probe_path = path / "base" / "exefs" / "main"
            elif (path / "exefs" / "main").is_file():
                probe_path = path / "exefs" / "main"
        def probe(path=probe_path):
            try:
                if os.path.isdir(path):
                    os.listdir(path)   # 普通文件夹（导入时玩家选的）：列一下，系统的文件访问授权框由这一步触发
                else:
                    with open(path, "rb") as f: f.read(4096)
                box["ok"] = True
            except OSError as e: box["err"] = e
        t = threading.Thread(target=probe, daemon=True); t0 = time.monotonic(); t.start()
        while not t.join(poll) and t.is_alive():
            c.check()
            c.emit(message=f"在等系统的文件访问授权：读 {path.name}（已等 {int(time.monotonic() - t0)} 秒）。"
                           "请在系统弹出的「允许访问」对话框里点「允许」；看不到对话框就把它从别的窗口后面切出来")
        if "err" in box:
            e = box["err"]
            if isinstance(e, PermissionError):
                raise Failure(f"系统不让 S2Arm 读 {path.name}（{path.parent}）：到「系统设置 › 隐私与安全性 › 文件与文件夹」"
                              "或「完全磁盘访问权限」里打开 S2Arm，再点一次", code="input_permission")
            raise Failure(f"读不了游戏文件 {path.name}：{e}", code="input_unreadable")


def requirement_errors(c, game):
    missing = []
    selected = selected_packages(game)
    if not any(p["type"] == "base" for p in selected):
        missing.append(Failure("缺游戏本体：只放了更新或 DLC，把本体的 NSP / XCI 也放进来", code="input_base_missing"))
    if guest_isa(selected) == "aarch32":
        missing.append(Failure("这款游戏是 32 位程序，S2Arm 暂时只支持 64 位游戏，打不了包", code="guest_isa_unsupported"))
    for p in selected:
        source = source_path(c, p)
        # 解包目录（format=dump/extract/loose）是目录，其余原件是文件
        is_extract = p.get("format") in ("dump", "extract", "loose")
        if not (source.is_dir() if is_extract else source.is_file()):
            missing.append(Failure(f"找不到游戏文件「{source.name}」：它所在的磁盘没接上，或文件被挪走了。接上磁盘，或重新添加这款游戏"
                                   if Path(p["path"]).is_absolute() else "这款游戏的文件找不到了，请重新添加游戏文件。", code="input_missing"))
    return missing


def import_games(c):
    c.begin("检查导入文件")
    c.require_library()
    # 外接盘 / 下载文件夹第一次读会弹系统授权框：先试读，框没点之前界面要说清在等什么，别一直停在「检查导入文件」
    wait_readable(c, [Path(raw).expanduser() for raw in c.r["paths"] if Path(raw).expanduser().exists()])
    files = []
    extract_dirs = []  # 解包目录单独列
    for raw in c.r["paths"]:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            # 软链照目标认（10-26 维护者把 Downloads 的原件挪到外置盘、原位置留软链）：指向文件的算文件、软链目录跟进去，
            #   记的是软链所在的路径（出包时照样跟到目标）；按真实路径记走过的目录，软链成环也不会转圈
            seen = set()
            for d, ds, fs in os.walk(p, followlinks=True):
                real = os.path.realpath(d)
                if real in seen:
                    ds[:] = []
                    continue
                seen.add(real)
                # 三种解包目录判据：认到就不再往里走
                is_extract_dir = is_extracted_game(Path(d))
                if is_extract_dir:
                    extract_dirs.append(Path(d))
                    ds[:] = []
                if not is_extract_dir:
                    files.extend(Path(d) / f for f in fs if f.lower().endswith(SOURCE_SUFFIXES) and os.path.isfile(os.path.join(d, f))
                                 and (not f.lower().endswith(NUP_SUFFIX) or is_nup(c, Path(d) / f)))   # 文件夹里扩展名是 .nup 却不是 NUP 的照旧不认
        elif p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES and (p.suffix.lower() != NUP_SUFFIX or is_nup(c, p)):
            files.append(p)
        elif not p.exists():
            raise Failure("所选游戏文件或文件夹不存在。", code="input_missing")
        else:
            raise Failure("请选择 NSP、XCI 文件、解包好的游戏文件夹或包含它们的文件夹。", code="input_type_unsupported")
    files = list(dict.fromkeys(sorted(files)))
    extract_dirs = list(dict.fromkeys(sorted(extract_dirs)))
    if not files and not extract_dirs:
        raise Failure("所选位置没有 NSP、XCI 文件或解包好的游戏文件夹。", code="input_empty")
    # prod.keys 只有 Switch 1 原件（NSP / XCI / NSZ / XCZ，要读取器解密）才要；只放 Switch 2 NUP 容器或解包目录不要密钥
    switch1 = [f for f in files if f.suffix.lower() != NUP_SUFFIX]
    if switch1 and not c.keys.is_file():
        raise Failure("请先导入 prod.keys。", code="keys_missing")
    results = []
    with c.lock():
        cat = c.catalog()
        with tempfile.TemporaryDirectory(prefix="icons-", dir=c.private) as icons:
            total = len(files) + len(extract_dirs)
            idx = 0
            for i, source in enumerate(files):
                c.begin("读取游戏信息", idx * 2 + 1, total * 2)
                packages = nup_inspect(c, source, icons) if source.suffix.lower() == NUP_SUFFIX else inspect(c, source, icons)
                c.begin("校验游戏文件", idx * 2 + 2, total * 2)
                sha = digest(source, c.progress)
                for metadata in packages:
                    tid = metadata["baseId"]
                    game = next((g for g in cat["games"] if g["id"] == tid), None)
                    if not game:
                        game = dict(id=tid, name=metadata["name"] or tid, display="", packages=[], hidden=False)
                        cat["games"].append(game)
                    game["hidden"] = False
                    same = [p for p in game["packages"] if p["sha256"] == sha and p["titleId"] == metadata["titleId"]]
                    duplicate = bool(same)
                    for p in same:
                        # 同一份内容换了位置再放一次（挪了盘 / 改了文件夹）：改记新位置；复制进库的旧托管副本还在就照旧用（不改记成引用），
                        #   已被「删除托管 NSP 副本」删掉的改记成这次放进来的原件（beta.3 升级上来想省空间的玩家：删副本 → 重放原件）
                        if Path(p["path"]).is_absolute() or not source_path(c, p).is_file():
                            p["path"] = str(source)
                    if not duplicate:
                        icon = Path(icons) / (metadata["titleId"] + ".jpg")
                        if icon.is_file():
                            target = contained(c.library, c.library / "games" / tid / "cover.jpg")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(icon, target)
                        metadata.pop("icon", None)
                        game["packages"].append(metadata | dict(path=str(source), sha256=sha, size=source.stat().st_size))
                        if metadata["name"]:
                            game["name"] = metadata["name"]
                    results.append(dict(id=tid, name=source.name, duplicate=duplicate))
                idx += 1
                c.check()
                atomic(c.library / "catalog.json", cat)
            # 解包目录
            for i, source in enumerate(extract_dirs):
                c.begin("读取游戏信息", idx * 2 + 1, total * 2)
                packages = extract_inspect(c, source, icons)
                c.begin("校验游戏文件", idx * 2 + 2, total * 2)
                # extract_inspect 已算哈希
                for metadata in packages:
                    sha = metadata.get("sha256", "")
                    tid = metadata["baseId"]
                    game = next((g for g in cat["games"] if g["id"] == tid), None)
                    if not game:
                        game = dict(id=tid, name=metadata["name"] or tid, display="", packages=[], hidden=False)
                        cat["games"].append(game)
                    game["hidden"] = False
                    same = [p for p in game["packages"] if p["sha256"] == sha and p["titleId"] == metadata["titleId"]]
                    duplicate = bool(same)
                    for p in same:
                        if Path(p["path"]).is_absolute() or not source_path(c, p).is_dir():
                            p["path"] = str(source)
                    if not duplicate:
                        icon = Path(icons) / (metadata["titleId"] + ".jpg")
                        if icon.is_file():
                            target = contained(c.library, c.library / "games" / tid / "cover.jpg")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(icon, target)
                        metadata.pop("icon", None)
                        # 目录：size = 内文件总字节（extract_inspect 已算在 contentBytes）
                        game["packages"].append(metadata | dict(path=str(source), sha256=sha, size=metadata.get("contentBytes", 0)))
                        if metadata["name"]:
                            game["name"] = metadata["name"]
                    results.append(dict(id=tid, name=source.name, duplicate=duplicate))
                idx += 1
                c.check()
                atomic(c.library / "catalog.json", cat)
    if switch1:   # 读取器用这份密钥读通了 Switch 1 原件才算验证过；只导 NUP 或解包目录没用到密钥，不改密钥状态
        atomic(c.private / "keys/status.json", dict(ready=True, verified=True))
    return {"imports": results}


JOB_LIST_LIMIT = 200


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


CUSTOM_NAME_LIMIT = 64
CUSTOM_COVER_LIMIT = 16 << 20
_CUSTOM_COVER = re.compile(r"cover\.custom\.[0-9a-f]{8}\.jpg")


def game_custom(c, gid):
    """玩家改过的显示名和封面（games/<程序号>/custom.json）。只影响显示，不改 catalog.json，不让已出的包失效；重新导入也不会覆盖。"""
    folder = contained(c.library, c.library / "games" / gid)
    try:
        data = read(folder / "custom.json", {})
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    cover = data.get("cover") if isinstance(data.get("cover"), str) and _CUSTOM_COVER.fullmatch(data["cover"]) else ""
    return dict(name=name, cover=folder / cover if cover and (folder / cover).is_file() else None)


def _custom_cover_bytes(path):
    try:
        with open(path, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Failure("封面要选一张图片文件。")
            if info.st_size > CUSTOM_COVER_LIMIT:
                raise Failure("封面图片太大了：请选 16 MB 以内的图片。")
            data = source.read(CUSTOM_COVER_LIMIT + 1)
    except OSError as error:
        raise Failure("读不了这张图片，请换一张试试。") from error
    if not data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        raise Failure("封面只支持 JPG 或 PNG 图片。")
    return data


def customize(c):
    """改游戏显示名 / 封面：name、coverPath 没给就不动，给空字符串恢复默认。"""
    with c.lock():
        gid = c.game()["id"]
        c.begin("保存游戏名字和封面")
        folder = contained(c.library, c.library / "games" / gid)
        current = game_custom(c, gid)
        name, cover = current["name"], current["cover"].name if current["cover"] else ""
        data = None
        if "name" in c.r:
            name = c.r["name"].strip() if isinstance(c.r["name"], str) else None
            if name is None or len(name) > CUSTOM_NAME_LIMIT or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
                raise Failure(f"游戏名字要在 {CUSTOM_NAME_LIMIT} 个字以内，不能有换行。")
        if "coverPath" in c.r:
            if not isinstance(c.r["coverPath"], str):
                raise Failure("封面路径不对。")
            cover = ""
            if c.r["coverPath"]:
                data = _custom_cover_bytes(c.r["coverPath"])
                cover = f"cover.custom.{hashlib.sha256(data).hexdigest()[:8]}.jpg"   # 换文件名：两端按路径缓存封面，同名会显示旧图
        folder.mkdir(parents=True, exist_ok=True)
        if data is not None:
            temporary = folder / (cover + ".partial")
            temporary.write_bytes(data)
            os.replace(temporary, folder / cover)
        if name or cover:
            atomic(folder / "custom.json", dict(schema=1, name=name, cover=cover))
        else:
            (folder / "custom.json").unlink(missing_ok=True)
        for old in folder.glob("cover.custom.*.jpg"):
            if old.name != cover:
                old.unlink(missing_ok=True)
    return dict(gameId=gid, name=name, cover=str(folder / cover) if cover else "")


def catalog(c):
    from build import validate_runtime
    import toolchain
    c.begin("读取游戏库")
    cat = c.catalog()
    input_supported, input_reason = toolchain.input_interface(c)
    for game in cat["games"]:
        game["missing"] = requirements(c, game)
        game["requirementDiagnostics"] = [c.describe_failure(error) for error in requirement_errors(c, game)]
        game["compatibility"] = "unsupported" if guest_isa(selected_packages(game)) == "aarch32" else "experimental"
        current = c.library / "games" / game["id"] / "current"
        try:
            manifest = validate_runtime(c, current, full=False)
            game["ready"] = manifest.get("memoryRequirementsVersion") == 2
        except (Failure, OSError, ValueError):
            manifest = {}
            game["ready"] = False
        # 升级启动器不算要重新准备（11-08 维护者：覆盖升级状态要保留）：运行版本自带宿主，旧版照常能玩；输入变了才要重做
        game["needsPreparation"] = not game["ready"] or manifest.get("inputIdentity") != input_identity(game)
        iphone = c.library / "iPhone" / game["id"]
        game["iphoneReady"] = iphone.is_dir() and any(iphone.glob("*.ipa")) and any(p.is_dir() and p.name.endswith("资源包") for p in iphone.iterdir())
        game["iphoneInstall"] = read(c.library / "games" / game["id"] / "ios-install.json", None)
        # F06：按宿主二进制真实能力报，不能一律 True（否则 UI 的「旧引擎缺接口」提示永远不出现）
        game["inputConfig"] = toolchain.input_interface(c, current / "S2ArmGame.app/Contents/MacOS/s2arm-host")[0] if game["ready"] else input_supported
        cover = c.library / "games" / game["id"] / "cover.jpg"
        custom = game_custom(c, game["id"])
        game["customName"], game["customCover"] = bool(custom["name"]), custom["cover"] is not None
        game["name"] = custom["name"] or game["name"]
        game["cover"] = str(custom["cover"] or cover) if custom["cover"] or cover.is_file() else ""
        choices = [p for p in selected_packages(game) if p["type"] in ("base", "update")]
        if choices:
            game["display"] = max(choices, key=lambda p: p["version"])["display"]
    try:
        validate_keys(c.keys)
        status = read(c.private / "keys/status.json", {}) | dict(ready=True)
    except (Failure, OSError, UnicodeError):
        status = dict(ready=False, verified=False)
    status["message"] = "已验证解密能力" if status.get("verified") else "格式通过，首次读取游戏时验证解密能力" if status["ready"] else "尚未导入 prod.keys"
    # 任务目录里还有各任务留存的请求（<任务>.request.json，J10 resume 用）：不是任务记录，没有 id / state；混进来启动器整个任务列表解码失败、一条都读不回
    # 任务目录有上限（J10 保留规则：结束超过 14 天的清掉、最新 50 条总留着）：catalog 每次启动都跑，在这里顺手清，一天最多一次
    #   （清理要把每条记录读一遍判结束没有），先清再读。只回最新 JOB_LIST_LIMIT 条：旧启动器的任务列表只看近期；
    #   11-07 实测任务目录 1.5 万条记录时整份读 + 回传 7 MB 占 catalog 数秒。
    marker = c.private / "jobs-pruned"
    if time.time() - _mtime(marker) > 86400:
        prune_jobs(c.private)
        marker.touch()
    records = sorted((p for p in (c.private / "jobs").glob("*.json") if p != c.jobpath and not p.name.endswith(".request.json")),
                     key=_mtime, reverse=True)[:JOB_LIST_LIMIT]
    jobs = [r for r in (read(p) for p in records) if isinstance(r, dict)]
    return dict(games=cat["games"], profiles=[], keys=status, jobs=jobs,
                engineInfo={"inputConfig": input_supported, "inputConfigReason": input_reason})


# 这一款在私有目录里按程序号存的出包缓存（直连 / 翻译 / 追加内容 / 原件核对 / 宽屏；都在 S2A_RUN_ROOT = <私有>/run 下，pack/jobs/core.py Context.env）
# 显式清理/删除含共享编库缓存；Q15 成功后的自动清理仅清本款中间产物，保留可复用编库条目。
def game_caches(c, gid, *, include_compiler=True):
    run = c.work_root / "run" / "s2arm"
    low = gid.lower()
    direct = run / "cache" / "direct" / gid.upper()
    out = [direct] if include_compiler else direct_transients(c, direct)
    for d, pat in ((run / "cache" / "bake", f"{low}-*"), (run / "cache" / "aoc", f"t{low}-*"), (run / "cache" / "srcver", f"t{low}-*"),
                   (run / "widen", f"t{low}_*"), (run / "widen" / "auto", f"t{low}_*")):
        out += sorted(d.glob(pat)) if d.is_dir() else []
    if include_compiler:
        out += [c.work_root / "cache" / "air", c.work_root / "cache" / "pso", c.work_root / "run" / "gal_dl_cache"]
    return [p for p in out if p.exists() or p.is_symlink()]


def direct_transients(c, direct):
    """Only disposable direct-build work; never remove the store (store/, logs/) or unknown layouts.
    S2Arm 11-01: the direct cache is one content-addressed store, one file per entry (C# dsh_batch build, store/dshb/); the former per-key trees
    ios/ and mac/ (b-<chain>/, cas/…) and work/ are the old layout, which direct_lib.sh deletes on its next build anyway"""
    from cache_retention import _safe_root
    if _safe_root(c.work_root, direct) != 'safe':
        return []
    out = [direct / name for name in ('work', 'merged', 'mac', 'ios')]
    for path in sorted(direct.glob('*.stage.*')):
        name, _, pid = path.name.partition('.stage.')
        if name not in ('work', 'mac', 'ios') or not pid.isdecimal() or int(pid) <= 0:
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            out.append(path)
        except (PermissionError, OverflowError):
            pass  # unknown/live process: retain its in-progress stage
    # Retain links: only real, producer-owned transient directories are eligible.
    safe = []
    for path in out:
        try:
            if not stat.S_ISLNK(path.lstat().st_mode):
                safe.append(path)
        except FileNotFoundError:
            pass
    return safe


def _rm_direct_transient(c, direct, path):
    """Remove only this lexical child, without resolving its final component."""
    from cache_retention import _safe_root
    if path.parent != direct or _safe_root(c.work_root, direct) != 'safe':
        return False
    # Pin the checked parent; rmtree's dir_fd implementation refuses a directory
    # replaced by a symlink instead of traversing it. Global _rm stays unchanged.
    if os.name == "nt":
        with winproc.open_dir_nofollow(direct) as parent:
            try:
                winproc.remove_at(parent, path.name)
            except FileNotFoundError:
                return False
            except OSError as error:
                if error.errno == errno.ELOOP:
                    return False
                raise
        return True
    fd = os.open(direct, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            mode = os.stat(path.name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode):
            return False
        if stat.S_ISDIR(mode):
            shutil.rmtree(path.name, dir_fd=fd)
        else:
            os.unlink(path.name, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def _rmtree(p):
    """删整棵目录；遇到只读目录（产物根的 RomFS 共享存储 roots/.romfs_store/ 整棵只读，pack/roots.sh）先给它加回写权限再删。
    Windows 上封存不是只读位，是 ACL 里给当前用户的拒写项（pack/orch/roots.py seal → _win_acl），chmod 解不开：
    按 roots 同一个函数把出错的项和它的父目录解封再删（11-07 Windows 出包机：启动器出包成功后清中间产物卡在 .romfs_store，WinError 5）"""
    def unlock(func, path, _):
        parent = os.path.dirname(path)
        if os.name == "nt":
            engine = str(Path(__file__).resolve().parents[2])
            if engine not in sys.path:
                sys.path.insert(0, engine)
            from pack.orch import roots
            for q in (parent, path):
                if os.path.lexists(q) and not os.path.islink(q):
                    roots._win_acl(q, False)
        else:
            os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        func(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=unlock)
    else:
        shutil.rmtree(p, onerror=unlock)


def _rm(c, root, p):
    # Check the entry before resolving it: a link into saves/ is not disposable.
    root, p = Path(os.path.abspath(root)), Path(os.path.abspath(p))
    try:
        relative = p.relative_to(root)
    except ValueError:
        raise Failure("操作路径不属于托管目录。") from None
    if not relative.parts:
        raise Failure("操作路径不属于托管目录。")
    parent = root
    for part in relative.parts[:-1]:
        parent = parent / part
        if _is_link(parent):
            raise Failure("清理路径包含目录链接，已停止操作。")
    if _is_link(p):
        # A Windows junction is removed with rmdir, a symlink with unlink.
        if p.is_symlink():
            p.unlink()
        else:
            p.rmdir()
    elif p.is_dir():
        _rmtree(p)
    else:
        p.unlink(missing_ok=True)


def _is_link(p):
    """软链或 Windows junction（目录软链建不了时 pack/orch/roots.py directory_link 退到 junction；Path.is_symlink 认不出 junction）"""
    return p.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(p)


def _rm_entry(c, p):
    """链接只删链接本身、不进指向；其余同 _rm"""
    _rm(c, c.library, p)


def _prune_roots(c, d):
    """产物根只留现行（*@现行 及其指向）与 RomFS 共享存储，清旧版本 → 清了几项。
    RomFS 共享存储：现行指向其中的身份，删了现行就断；没人指向的旧身份由下次提交清（pack/roots.sh root_romfs_sweep）"""
    links = list(d.glob("*@现行"))
    current = {q.name for q in links} | {q.resolve().name for q in links if _is_link(q)}
    n = 0
    for q in d.iterdir():
        if q.name not in current and q.name != ".romfs_store":
            _rm_entry(c, q); n += 1
    return n


def _windows_layout():   # 单测可打桩：Windows 的产物根在 work/ 下（ipa.current_root）
    return os.name == "nt"


def prune_build(c, gid, *, retain_compiler=True):
    """成功后清本款中间产物，保留共享编库缓存；显式清缓存用 retain_compiler=False。失败不调用。
    自动清理保留产物根里的现行版本（roots/*@现行 及其指向，11-02 多平台共用阶段：接着导 Mac 时克隆它起步、平台无关阶段按键跳过；
    再导 iPhone 也从它增量刷新），只清旧版本——与 Mac 构建缓存「保留现行」一致；显式清缓存照旧整个删。
    成功后的自动清理（retain_compiler=True，只在打 iPhone 包成功后调）先记一行出包实测（record_pack_usage）：要量清之前的整套占用"""
    if retain_compiler:
        game = next((g for g in read(c.library / "catalog.json", {}).get("games", []) if g.get("id") == gid), None)
        if game:
            record_pack_usage(c, game)
    out = c.library / "iPhone" / gid
    n = 0
    if out.is_dir():
        for p in out.iterdir():
            if p.name == "logs" or p.name.endswith("资源包") or (p.is_file() and p.suffix == ".ipa"):
                continue
            if p.name == "roots" and retain_compiler and p.is_dir() and not _is_link(p):
                n += _prune_roots(c, p)
                continue
            # Windows 的产物根在 work/ 下（ipa.current_root：work/pkg/product = 现行各项联接 + shaders/direct → work/direct），装进容器要用：
            #   原先整个 work/ 被删，打完 iPhone 包再装必报 container_stale（#26 同一位报告者 #32）。保留 roots 的现行、pkg/product
            #   （去掉已拷到游戏目录的交付件 product/pkg）与 direct*，其余照清
            if _windows_layout() and p.name == "work" and retain_compiler and p.is_dir() and not _is_link(p):
                for q in p.iterdir():
                    if q.name == "roots" and q.is_dir() and not _is_link(q):
                        n += _prune_roots(c, q)
                    elif q.name == "pkg" and q.is_dir() and not _is_link(q):
                        for r in q.iterdir():
                            if r.name != "product":
                                _rm_entry(c, r); n += 1
                        if os.path.lexists(q / "product" / "pkg"):
                            _rm_entry(c, q / "product" / "pkg"); n += 1
                    elif not re.fullmatch(r"direct(_p\d+)?", q.name):
                        _rm_entry(c, q); n += 1
                continue
            _rm(c, c.library, p); n += 1
    direct = c.work_root / 'run/s2arm/cache/direct' / gid.upper()
    for p in game_caches(c, gid, include_compiler=not retain_compiler):
        if retain_compiler and p.parent == direct:
            n += _rm_direct_transient(c, direct, p)
        else:
            _rm(c, c.work_root, p); n += 1
    return n


def delete(c):
    """删除这款游戏（连出包产物与缓存）：iPhone 输出整个目录（IPA / 资源包 / 中间产物）、Mac 构建缓存、game_caches，并从游戏库清单里删掉；
    保留存档（library/saves/<程序号>）与游戏原件（原地导入的不碰；旧版复制进 library/sources 的托管副本另有「删除托管 NSP 副本」）。
    执行锁可能还被这一款刚被取消的任务占着（启动器发删除前先结束它在跑的任务，进程退出要几秒）：拿不到就每 2 秒再试，最多 120 秒"""
    t0 = time.time()
    while True:
        try:
            with c.lock("execution"), c.lock():
                gid = c.game()["id"]
                c.begin("删除游戏与出包产物")
                for p in (c.library / "iPhone" / gid, c.library / "cache" / gid):
                    if p.exists():
                        _rm(c, c.library, p)
                for p in game_caches(c, gid):
                    _rm(c, c.work_root, p)
                cat = c.catalog()
                cat["games"] = [g for g in cat["games"] if g["id"] != gid]
                atomic(c.library / "catalog.json", cat)
            return {}
        except Failure as e:
            if "另一个任务正在使用游戏库" not in str(e) or time.time() - t0 > 120:
                raise
            c.begin("等这款游戏在跑的任务结束"); time.sleep(2)


def remove(c):
    with c.lock():
        cat = c.catalog()
        for game in cat["games"]:
            if game["id"] == c.r["gameId"]:
                game["hidden"] = True
        atomic(c.library / "catalog.json", cat)
    return {}


def clean(c):
    with c.lock("execution"), c.lock():
        game = c.game()
        kind = c.r.get("kind")
        if kind == "cache":
            c.begin("清理构建缓存")
            target = contained(c.library / "cache", c.library / "cache" / game["id"])
            if target.is_dir():
                _rmtree(target)
            prune_build(c, game["id"], retain_compiler=False)   # 用户显式清缓存保留原有全清语义，不套用 Q15 自动保留策略
        elif kind == "sources":
            # 只删 0.1.0-beta.3 及以前复制进游戏库的托管副本；原地导入的原件是玩家的文件，只读、不删
            other = {p["path"] for g in c.catalog()["games"] if g["id"] != game["id"] for p in g["packages"]}
            for p in game["packages"]:
                if not Path(p["path"]).is_absolute() and p["path"] not in other:
                    contained(c.library / "sources", c.library / p["path"]).unlink(missing_ok=True)
        else:
            raise Failure("未知清理类型。")
    return {}


# —— 释放空间（11-07 启动器，参考 NX2Apple frontend FreeSpace.swift 的部件划分）——
# 每款按部件列出占用，玩家勾选后删除。只列「下次出包会重建」的部件和已出好的包；存档、日志、配置、清单、游戏原件只报大小、不给删。
#   直接删，不放废纸篓（废纸篓仍占同一块盘）。有任务在跑（执行锁拿不到）拒绝，不等。
SPACE_PARTS = (
    ("unpack", "解包与构建缓存", "下次出包会重新解包（多花几分钟）"),
    ("intermediate", "出包中间产物", "下次出包会重新生成（多花几分钟到几十分钟）"),
    ("shaders", "着色器与出包缓存", "下次出包会重新翻译着色器（大游戏要多花较长时间）"),
    ("mac", "Mac 运行版本", "下次在 Mac 上玩之前会重新准备"),
    ("ipa", "已出好的 iPhone 包", "删了要重新出包才能再装到 iPhone；已装好的游戏不受影响"),
)
SPACE_KEPT = ("saves", "logs")


def _ipa_outputs(out):
    return [p for p in out.iterdir() if (p.is_file() and p.suffix == ".ipa") or p.name.endswith("资源包")] if out.is_dir() else []


def space_parts(c, gid):
    """{部件 id: [路径…]}，外加不给删的 saves / logs。路径都在游戏库或私有目录之内；不存在的不列"""
    out = c.library / "iPhone" / gid
    shared = {c.work_root / "cache" / "air", c.work_root / "cache" / "pso", c.work_root / "run" / "gal_dl_cache"}   # 各款共用的编库缓存：不归任何一款
    ipa = _ipa_outputs(out)
    inter = [p for p in out.iterdir() if p.name != "logs" and p not in ipa] if out.is_dir() else []
    parts = {
        "unpack": [c.library / "cache" / gid],
        "intermediate": inter,
        "shaders": [p for p in game_caches(c, gid) if p not in shared],
        "mac": [c.library / "games" / gid],
        "ipa": ipa,
        "saves": [c.library / "saves" / gid],
        "logs": [out / "logs"],
    }
    return {k: [p for p in v if p.exists() or p.is_symlink()] for k, v in parts.items()}


def _part_bytes(paths):
    by_dev = {}
    for p in paths:
        try:
            by_dev.setdefault(p.lstat().st_dev, []).append(p)
        except OSError:
            pass
    return sum(disk_bytes(v, dev) for dev, v in by_dev.items())


def space_survey(c):
    """每款（gameId 给了只这一款）按部件的占用（字节，按所在卷实测）：parts 可勾选删除，kept 只报不删"""
    games = c.catalog()["games"]
    gid = c.r.get("gameId")
    if gid:
        games = [g for g in games if g["id"] == gid]
        if not games:
            raise Failure("游戏未登记在当前游戏库中。")
    c.begin("统计各游戏占用的空间")
    rows = []
    for g in games:
        c.check()
        parts = space_parts(c, g["id"])
        rows.append(dict(gameId=g["id"], name=game_custom(c, g["id"])["name"] or g.get("name") or g["id"],
                         parts=[dict(id=k, title=t, rebuild=r, bytes=_part_bytes(parts[k])) for k, t, r in SPACE_PARTS],
                         kept=[dict(id=k, bytes=_part_bytes(parts[k])) for k in SPACE_KEPT]))
    return dict(games=rows)


def space_free(c):
    """删这款勾选的部件（request.parts = 部件 id 列表，只认 SPACE_PARTS 里的）。有任务在跑拒绝；返回各部件删前字节与释放合计"""
    want = c.r.get("parts")
    ids = {k for k, _, _ in SPACE_PARTS}
    if not isinstance(want, list) or not want or any(p not in ids for p in want):
        raise Failure("要删除的部件不对：只能选" + "、".join(t for _, t, _ in SPACE_PARTS) + "。")
    with c.lock("execution"), c.lock():
        gid = c.game()["id"]
        c.begin("释放空间")
        parts = space_parts(c, gid)
        freed, done = 0, []
        for k in [k for k, _, _ in SPACE_PARTS if k in want]:
            before = _part_bytes(parts[k])
            for p in parts[k]:
                root = c.work_root if p.is_relative_to(c.work_root) else c.library
                _rm(c, root, p)
            after = _part_bytes([p for p in parts[k] if p.exists() or p.is_symlink()])
            freed += before - after
            done.append(dict(id=k, bytes=before - after))
        return dict(gameId=gid, parts=done, freedBytes=freed)


def validate_input(config):
    if not isinstance(config, dict) or config.get("schema") != 1:
        raise Failure("不支持的输入映射格式。")
    targets = {"A", "B", "X", "Y", "LS", "RS", "L", "R", "ZL", "ZR", "PLUS", "MINUS",
               "LEFT", "UP", "RIGHT", "DOWN", "LS_UP", "LS_DOWN", "LS_LEFT", "LS_RIGHT",
               "RS_UP", "RS_DOWN", "RS_LEFT", "RS_RIGHT"}
    pad = {"east", "south", "north", "west", "leftStick", "rightStick", "leftShoulder", "rightShoulder",
           "leftTrigger", "rightTrigger", "menu", "options", "dpadLeft", "dpadUp", "dpadRight", "dpadDown"}
    axes = {"leftX", "leftY", "rightX", "rightY"}
    for name in ("keyboard", "mouse", "gamepad", "axes"):
        if not isinstance(config.get(name), dict):
            raise Failure("输入映射不完整。")
        seen = set()
        for target, binding in config[name].items():
            if target not in ({"LX", "LY", "RX", "RY"} if name == "axes" else targets):
                raise Failure("无效输入目标。")
            if name == "keyboard":
                if not isinstance(binding, dict) or type(binding.get("code")) is not int or not 0 <= binding["code"] <= 127 or type(binding.get("modifiers", 0)) is not int or not 0 <= binding.get("modifiers", 0) <= 7:
                    raise Failure("无效键盘映射。")
                token = (binding["code"], binding.get("modifiers", 0))
            elif name == "mouse":
                if type(binding) is not int or not 0 <= binding <= 4:
                    raise Failure("无效鼠标映射。")
                token = binding
            elif name == "gamepad":
                if not isinstance(binding, str) or binding not in pad:
                    raise Failure("无效手柄映射。")
                token = binding
            else:
                if not isinstance(binding, dict) or binding.get("source") not in axes or type(binding.get("invert", False)) is not bool:
                    raise Failure("无效摇杆映射。")
                token = binding["source"]
            if token in seen:
                raise Failure("输入映射存在重复绑定。")
            seen.add(token)
    if not isinstance(config.get("deadzone"), (int, float)) or not 0 <= config["deadzone"] < 1:
        raise Failure("摇杆死区必须在 0 到 1 之间。")
    options = config.get("mouseOptions")
    if not isinstance(options, dict) or options.get("mode") not in ("camera", "off"):
        raise Failure("无效鼠标选项。")
    for name in ("sensitivityX", "sensitivityY"):
        if type(options.get(name)) not in (int, float) or not .1 <= options[name] <= 10:
            raise Failure("鼠标灵敏度必须在 0.1 到 10 之间。")
    for name in ("invertX", "invertY", "recenter"):
        if type(options.get(name, False)) is not bool:
            raise Failure("无效鼠标选项。")
    return config


def save_input(c):
    config = validate_input(c.r["config"])
    if c.r.get("gameId"):
        c.game()
        target = contained(c.library, c.library / "mappings" / (c.r["gameId"] + ".json"))
    else:
        target = c.private / "default-input.json"
    atomic(target, config)
    return {}


def signing_status():
    try:
        from install import signing_summary
        return signing_summary()
    except Exception as error:
        return {"error": type(error).__name__}


def diagnostics(c):
    # Keep one path/rule engine, with UI truncation disabled for exported files.
    import platform
    import plistlib
    import subprocess

    def redact(value):
        return c.redact(value, limit=None)

    task_prefix = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def redact_member_component(value):
        # This position is a generated task ID, not a device identifier.
        match = task_prefix.match(value)
        return match[0] + redact(value[match.end():]) if match else redact(value)

    def redact_fields(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {key: redact_fields(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_fields(item) for item in value]
        return value

    def probe(args):
        try:
            result = subprocess.run(args, env=c.env(), capture_output=True, text=True, timeout=10)
            if result.returncode:
                return {"error": f"命令失败（退出码 {result.returncode}）"}
            return result.stdout.strip() or {"error": "命令输出为空"}
        except subprocess.TimeoutExpired:
            return {"error": "命令超过 10 秒"}
        except OSError:
            return {"error": "命令无法执行"}

    def launcher_version():
        # Installed Jobs live below Contents/Resources; source runs have no
        # launcher identity and must say so rather than invent a version.
        contents = next((p for p in Path(__file__).resolve().parents if p.name == "Contents"), None)
        if contents is None:
            return {"error": "源码运行，没有启动器 Info.plist"}
        try:
            info = plistlib.loads((contents / "Info.plist").read_bytes())
            value = info.get("CFBundleShortVersionString")
            return value if isinstance(value, str) and value.strip() else {"error": "启动器版本字段缺失"}
        except (OSError, ValueError, plistlib.InvalidFileException):
            return {"error": "启动器 Info.plist 无法读取"}

    destination = Path(c.r["path"]).expanduser().resolve()
    if c.private == destination or c.private in destination.parents or c.library == destination or c.library in destination.parents:
        raise Failure("诊断包请选择应用数据和游戏库以外的位置。")
    descriptors = []
    temporary = None
    try:
        flags = 0 if os.name == "nt" else os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        private_fd = winproc.open_dir_nofollow(c.private) if os.name == "nt" else os.open(c.private, flags)
        descriptors.append(private_fd)
        roots = {}
        # begin persists jobs; reject static root links before that write occurs.
        for name in ("jobs", "logs"):
            try:
                roots[name] = winproc.open_dir_nofollow(name, private_fd) if os.name == "nt" else os.open(name, flags, dir_fd=private_fd)
                descriptors.append(roots[name])
            except FileNotFoundError:
                roots[name] = None
        c.begin("导出脱敏诊断")
        if roots["jobs"] is None:
            roots["jobs"] = winproc.open_dir_nofollow("jobs", private_fd) if os.name == "nt" else os.open("jobs", flags, dir_fd=private_fd)
            descriptors.append(roots["jobs"])

        fd, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".partial", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w+b") as output:
            created = os.fstat(output.fileno())
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                def write_file(parent_fd, name, member, job=False):
                    c.check()
                    info = winproc.stat_at(name, parent_fd) if os.name == "nt" else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        return
                    if os.name == "nt":
                        source_file = winproc.open_file(name, parent_fd)
                    else:
                        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                        source_file = os.fdopen(file_fd, "rb")
                    with source_file as source:
                        opened = winproc.file_info(source) if os.name == "nt" else os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            return
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise Failure("诊断文件在导出期间发生变化，请重试。")
                        if job:
                            limit = DIAGNOSTIC_JOB_LIMIT
                            if opened.st_size > limit:
                                raise Failure("诊断任务文件超过 4 MiB 上限，未生成诊断包。")
                            data = source.read(limit + 1)
                            if len(data) > limit:
                                raise Failure("诊断任务文件超过 4 MiB 上限，未生成诊断包。")
                        else:
                            data = diagnostic_log_excerpt(source, opened.st_size)
                    stem, suffix = os.path.splitext(member)
                    # Archive separators are known structure, not email local-part characters.
                    public_member = "/".join(redact_member_component(part) for part in stem.split("/")) + suffix
                    if public_member in archive.namelist() or public_member == "manifest.json":
                        raise Failure("脱敏后的诊断文件名重复，未生成诊断包。")
                    # These allowlisted job/log files are text. Invalid UTF-8 is
                    # replaced, never copied as raw bytes around redaction.
                    archive.writestr(public_member, redact(data.decode(errors="replace")))

                if roots["jobs"] is not None:
                    for name in sorted(winproc.listdir(roots["jobs"]) if os.name == "nt" else os.listdir(roots["jobs"])):
                        if name.endswith(".json"):
                            write_file(roots["jobs"], name, "jobs/" + name[:-5] + ".txt", job=True)

                def logs(directory_fd, relative=""):
                    for name in sorted(winproc.listdir(directory_fd) if os.name == "nt" else os.listdir(directory_fd)):
                        c.check()
                        info = winproc.stat_at(name, directory_fd) if os.name == "nt" else os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        member = relative + name
                        if stat.S_ISDIR(info.st_mode):
                            child = winproc.open_dir_nofollow(name, directory_fd) if os.name == "nt" else os.open(name, flags, dir_fd=directory_fd)
                            try:
                                opened = child.info if os.name == "nt" else os.fstat(child)
                                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                    raise Failure("诊断目录在导出期间发生变化，请重试。")
                                logs(child, member + "/")
                            finally:
                                winproc.close(child) if os.name == "nt" else os.close(child)
                        elif name.endswith(".log"):
                            write_file(directory_fd, name, "logs/" + member)
                if roots["logs"] is not None:
                    logs(roots["logs"])
                manifest = dict(launcher_version=launcher_version(),
                                macos_version=platform.mac_ver()[0] or {"error": "macOS 版本不可用"},
                                xcode_version=probe(["/usr/bin/xcodebuild", "-version"]),
                                mac_model=probe(["/usr/sbin/sysctl", "-n", "hw.model"]),
                                xcode_signing=signing_status())
                manifest = redact_fields(manifest)
                # These names have already been sanitized; redacting them again
                # would hide task UUIDs and break the archive index.
                manifest["files"] = sorted(archive.namelist() + ["manifest.json"])
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        c.check()
        current = temporary.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
            raise Failure("诊断临时文件在导出期间发生变化，请重试。")
        os.replace(temporary, destination)
    except OSError:
        raise Failure("无法安全读取或写入诊断文件，未更新诊断包。") from None
    finally:
        for descriptor in reversed(descriptors):
            winproc.close(descriptor) if os.name == "nt" else os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return dict(outputPath=str(destination))
