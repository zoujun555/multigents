"""网页界面读数据和控制调度器的函数。对需求的操作在 lane/ops.py。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from .. import jenny
from ..agents import kill_tree
from ..core import AUTO_STAGES, GATES, Config, Lane, all_lanes, usage_today
from ..runner import pid_alive

ARTIFACTS = ("request.md", "spec.md", "selftest.md", "review.md", "test-design.md", "test-report.md",
             "summary.md", "feedback.md", "diagnosis.md")
STAGE_ORDER = ("plan", "spec_gate", "code", "review", "fix", "test", "itest", "report", "merge_gate", "done")
HISTORY_FILE = re.compile(r"^history/\d\d-[a-z-]+\.md$")
CONFIG_KEY = re.compile(r"^(lane\.toml|local\.toml|templates/AGENTS\.md|rules/[A-Za-z0-9_-]+\.md)$")


# ---- 调度器 ------------------------------------------------------------

def scheduler_state(cfg: Config) -> dict:
    lock = cfg.state_dir / "scheduler.lock"
    try:
        pid = int(lock.read_text())
    except (OSError, ValueError):
        return {"running": False, "pid": None}
    return {"running": pid_alive(pid), "pid": pid}


_start_lock = threading.Lock()   # 两个人同时点"启动"时，只让一个真的去启动


def start_scheduler(cfg: Config) -> str:
    with _start_lock:
        if scheduler_state(cfg)["running"]:
            raise SystemExit("调度器已经在运行")
        env = {**os.environ, "LANE_HOME": str(cfg.home), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        log_path = cfg.state_dir / "scheduler.log"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with open(log_path, "ab") as log:
            proc = subprocess.Popen([sys.executable, "-u", "-m", "lane", "run"], cwd=cfg.home, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
                                    start_new_session=(os.name != "nt"))
        for _ in range(50):   # 等它写下锁文件再返回，最多 10 秒
            if scheduler_state(cfg)["running"]:
                return "调度器已启动"
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
                raise SystemExit(f"调度器启动后立刻退出了：{tail}")
            time.sleep(0.2)
        raise SystemExit("调度器启动超时，请查看 state/scheduler.log")


def stop_scheduler(cfg: Config) -> str:
    state = scheduler_state(cfg)
    if not state["running"]:
        raise SystemExit("调度器没有在运行")
    kill_tree(state["pid"])   # 连同它启动的 Agent 进程一起结束；运行中的阶段下次启动时重新排队
    (cfg.state_dir / "scheduler.lock").unlink(missing_ok=True)
    return "调度器已停止。正在运行的阶段已终止，下次启动时会重新排队。"


# ---- 看板和需求 --------------------------------------------------------

def lane_summary(lane: Lane) -> dict:
    d = lane.data
    return {k: d.get(k) for k in ("id", "project", "title", "stage", "status", "round", "note", "since", "created", "branch")}


def board(cfg: Config) -> dict:
    lanes = all_lanes(cfg)
    return {
        "lanes": [lane_summary(l) for l in lanes],
        "alerts": list(jenny.find_alerts(cfg, lanes, []).values()),   # 当前仍存在的；历史见 alerts_history
        "usage": usage_today(cfg),
        "budget": cfg.data.get("budget", {}),
        "max_parallel": cfg.get("workbench", "max_parallel", 2),
        "scheduler": scheduler_state(cfg),
        "projects": sorted(cfg.projects),
        "stages": STAGE_ORDER,
        "auto_stages": AUTO_STAGES,
        "gates": GATES,
    }


def find_lane(cfg: Config, lane_id: str) -> tuple[Lane, bool]:
    """在进行中和已归档的需求里找。返回 (需求, 是否已归档)。"""
    if not re.fullmatch(r"REQ-\d+", lane_id or ""):
        raise SystemExit("需求编号格式不对")
    for folder, archived in ((cfg.lanes_dir, False), (cfg.state_dir / "archive", True)):
        path = folder / f"{lane_id}.json"
        if path.exists():
            return Lane(cfg, json.loads(path.read_text(encoding="utf-8"))), archived
    raise SystemExit(f"没有需求 {lane_id}")


def lane_detail(cfg: Config, lane_id: str) -> dict:
    lane, archived = find_lane(cfg, lane_id)
    files = [n for n in ARTIFACTS if (lane.req_dir / n).exists()]
    history_dir = lane.req_dir / "history"
    if history_dir.exists():
        files += sorted(f"history/{p.name}" for p in history_dir.glob("*.md"))
    logs = sorted((cfg.logs_dir / lane.id).glob("*.log"))
    tail = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-200:] if logs else []
    return {**lane.data, "archived": archived, "files": files, "auto_stages": AUTO_STAGES,
            "log_name": logs[-1].name if logs else "", "log_tail": "\n".join(tail)}


def lane_file(cfg: Config, lane_id: str, name: str) -> str:
    lane, _ = find_lane(cfg, lane_id)
    if name not in ARTIFACTS and not HISTORY_FILE.fullmatch(name or ""):
        raise SystemExit("不能读取这个文件")
    path = lane.req_dir / name
    if not path.exists():
        raise SystemExit(f"{name} 不存在（工作树可能已被清理）")
    return path.read_text(encoding="utf-8", errors="replace")


# ---- 配置文件 ----------------------------------------------------------

def config_path(home: Path, key: str) -> Path:
    if not CONFIG_KEY.fullmatch(key or ""):
        raise SystemExit("不能编辑这个文件")
    return home / key if key.startswith("templates/") else home / "config" / key


def config_files(home: Path) -> list[str]:
    rules = sorted(f"rules/{p.name}" for p in (home / "config" / "rules").glob("*.md"))
    return ["lane.toml", "local.toml", *rules, "templates/AGENTS.md"]


def config_read(home: Path, key: str) -> str:
    path = config_path(home, key)
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


def check_lane_toml(text: str) -> None:
    """保存前校验：格式正确，并且调度器需要的内容都在。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"TOML 格式错误：{exc}")
    agents, stages = data.get("agents", {}), data.get("stages", {})
    for stage in AUTO_STAGES:
        if stage not in stages:
            raise SystemExit(f"缺少 [stages.{stage}]")
        agent = stages[stage].get("agent")
        if agent not in agents:
            raise SystemExit(f"[stages.{stage}] 的 agent = \"{agent}\" 没有对应的 [agents.{agent}]")
    for name, agent in agents.items():
        if not isinstance(agent.get("command"), list) or not agent["command"]:
            raise SystemExit(f"[agents.{name}] 缺少 command")


def check_local_toml(text: str) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"TOML 格式错误：{exc}")
    for name, proj in data.get("projects", {}).items():
        if not proj.get("repo"):
            raise SystemExit(f"[projects.{name}] 缺少 repo")


def config_save(home: Path, key: str, text: str) -> str:
    path = config_path(home, key)
    if key == "lane.toml":
        check_lane_toml(text)
    elif key == "local.toml":
        check_local_toml(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())   # 保留上一版
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    os.replace(tmp, path)   # 一步替换：调度器不会读到写了一半的文件
    return f"已保存 {key}" + ("，上一版在同目录的 .bak 文件里" if path.with_suffix(path.suffix + ".bak").exists() else "")
