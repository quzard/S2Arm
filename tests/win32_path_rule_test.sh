#!/bin/bash
# Win32 路径入口规则检查：bash tests/win32_path_rule_test.sh
cd "$(dirname "$0")/.." || exit 1
exec "${S2A_PY:-python3}" -B tests/test_win32_path_rule.py
