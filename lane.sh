#!/usr/bin/env sh
# Linux / macOS 启动脚本：./lane.sh status、./lane.sh run、./lane.sh ui ……（Windows 用 lane.cmd）
# 用别的 python：LANE_PYTHON=python3.12 ./lane.sh status
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "${LANE_PYTHON:-python3}" -m lane "$@"
