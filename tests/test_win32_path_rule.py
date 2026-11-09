#!/usr/bin/env python3
"""规则检查：直接调 Win32 的路径入口必须过 long_path / win_path（pack/orch/winlong.py）。

v0.1.6 回归：winlong 只包了 os / builtins，roots.py 直接 ctypes 调 SetFileSecurityW / CopyFile2 / CreateFileW / GetVolumePathNameW，
注册表 LongPathsEnabled 关着（多数玩家机）时 >260 字符的裸路径得 WinError 3 / 123（播种报 roots.py _win_acl 123）。
这条只防「新增裸调用」：扫 pack/ 下非测试 .py 里这些 API 名，调用点往后 14 行内要有 long_path( / win_path( / _long_path()(，
或在同一行 / 上一行写 `lp-exempt: 原因`（例如只传卷根 C:\\）。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH_APIS = ("CreateFileW", "SetFileSecurityW", "CopyFile2", "GetVolumePathNameW", "CreateDirectoryW", "DeleteFileW",
             "RemoveDirectoryW", "MoveFileExW", "GetFileAttributesW", "SetFileAttributesW", "CreateSymbolicLinkW",
             "CreateHardLinkW", "FindFirstFileW", "GetDiskFreeSpaceExW", "GetNamedSecurityInfoW", "SetNamedSecurityInfoW",
             "GetFullPathNameW", "GetLongPathNameW", "GetShortPathNameW", "CreateProcessW")
OK = re.compile(r"long_path\(|win_path\(|_long_path\(\)\(|lp-exempt")
WINDOW = 14


def violations(text, name="<text>"):
    lines = text.splitlines()
    found = []
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0]
        for api in PATH_APIS:
            if re.search(r"['\"]" + api + r"['\"]|\." + api + r"\b", code):
                around = "\n".join(lines[max(0, i - 1): i + WINDOW])
                if not OK.search(around):
                    found.append(f"{name}:{i + 1}: {api} 没过 long_path / win_path（要么加，要么写 `lp-exempt: 原因`）")
    return found


class Win32PathRuleTest(unittest.TestCase):
    def test_rule_catches_a_bare_call(self):   # 负对照：规则本身有效
        bare = "f = _api('kernel32', 'SetFileSecurityW', W.BOOL, W.LPCWSTR, W.DWORD, C.c_void_p)\nif not f(str(path), 1, sd): pass\n"
        self.assertTrue(violations(bare))
        ok = "f = _api('kernel32', 'SetFileSecurityW', W.BOOL, W.LPCWSTR, W.DWORD, C.c_void_p)\nif not f(long_path(str(path)), 1, sd): pass\n"
        self.assertEqual(violations(ok), [])
        exempt = "h = _api('kernel32', 'CreateFileW', W.HANDLE)(absolute.anchor, 1)   # lp-exempt: 只传卷根\n"
        self.assertEqual(violations(exempt), [])

    def test_repo_has_no_bare_win32_path_call(self):
        found = []
        for path in sorted((ROOT / "pack").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or rel.startswith("pack/orch/winlong.py"):
                continue
            found += violations(path.read_text(encoding="utf-8"), rel)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
