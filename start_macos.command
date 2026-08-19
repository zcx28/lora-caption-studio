#!/bin/sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python)
else
  printf '%s\n' "找不到 Python 3.10+。请从 https://www.python.org/downloads/macos/ 安装后重试。"
  printf '%s' "按回车键关闭…"
  read -r _answer
  exit 1
fi

"$PYTHON_BIN" "$SCRIPT_DIR/bootstrap.py" "$@"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  printf '%s' "按回车键关闭…"
  read -r _answer
fi
exit "$STATUS"
