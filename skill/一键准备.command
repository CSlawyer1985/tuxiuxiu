#!/bin/sh
SKILL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=""
for candidate in python3.14 python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys;sys.exit(not ((3,12)<=sys.version_info[:2]<(3,15)))' 2>/dev/null; then
    PYTHON_BIN=$(command -v "$candidate")
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo '需要先安装 Python 3.12—3.14。本包不会改动系统 Python。'
else
  "$PYTHON_BIN" "$SKILL_DIR/scripts/setup_runtime.py" setup --human
fi
printf '\n按回车关闭窗口。'
read -r answer
