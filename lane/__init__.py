"""多 Agent 工作台。只用 Python 标准库。"""
import sys

MIN_PYTHON = (3, 11)   # 读配置用的 tomllib 从 3.11 起才在标准库里


def check_python(version_info=None) -> None:
    """版本太旧时给一句清楚的话，而不是一个看不懂的导入错误（Ubuntu 22.04 自带的是 3.10）。"""
    info = tuple(version_info or sys.version_info)[:3]
    if info < MIN_PYTHON:
        raise SystemExit(f"lane 需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新的版本，当前是 "
                         f"{'.'.join(map(str, info))}。请安装新版本，或用 LANE_PYTHON 指定一个新版本的 python。")


check_python()
