"""配置、需求状态、Git 操作。只用标准库。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

HOME = Path(os.environ.get("LANE_HOME") or Path(__file__).resolve().parent.parent)

# 阶段顺序。spec_gate / merge_gate 是人工闸门，其余由工具执行。
AUTO_STAGES = ("plan", "code", "review", "fix", "test", "itest", "report")
GATES = ("spec_gate", "merge_gate")
STAGE_LABEL = {
    "plan": "规划", "spec_gate": "等待批准规格", "code": "编码", "review": "评审",
    "fix": "修复", "test": "测试设计", "itest": "集成测试", "report": "汇总", "merge_gate": "等待确认合并", "done": "完成",
}
STATUS_LABEL = {"queued": "排队", "running": "运行中", "waiting": "等你", "blocked": "卡住", "done": "完成"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def age_minutes(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 60


# ---- 配置 --------------------------------------------------------------

class Config:
    def __init__(self, home: Path = HOME):
        self.home = home
        path = Path(os.environ.get("LANE_CONFIG") or home / "config" / "lane.toml")
        self.data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.state_dir = home / "state"
        self.lanes_dir = self.state_dir / "lanes"
        self.logs_dir = self.state_dir / "logs"
        for d in (self.lanes_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)

    @property
    def projects(self) -> dict:
        return self.data.get("projects", {})

    def project(self, name: str) -> dict:
        if name not in self.projects:
            raise SystemExit(f"配置里没有项目 '{name}'。已登记：{', '.join(self.projects) or '（无）'}")
        return self.projects[name]

    def stage(self, name: str) -> dict:
        return self.data["stages"][name]

    def agent(self, name: str) -> dict:
        return self.data["agents"][name]

    def worktree_root(self) -> Path:
        root = Path(self.get("workbench", "worktree_root", "worktrees"))
        return root if root.is_absolute() else self.home / root


# ---- 需求（lane）状态 --------------------------------------------------

@dataclass
class Lane:
    cfg: Config
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def path(self) -> Path:
        return self.cfg.lanes_dir / f"{self.id}.json"

    @property
    def worktree(self) -> Path:
        return Path(self.data["worktree"])

    @property
    def req_rel(self) -> str:
        return f"docs/reqs/{self.id}/"

    @property
    def req_dir(self) -> Path:
        return self.worktree / "docs" / "reqs" / self.id

    def save(self) -> None:
        self.data["updated"] = now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        for _ in range(5):  # Windows 上偶尔被杀毒或另一个读者占用
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.2)
        os.replace(tmp, self.path)

    def set(self, stage: str | None = None, status: str | None = None, note: str | None = None) -> None:
        if stage:
            self.data["stage"] = stage
        if status:
            self.data["status"] = status
            self.data["since"] = now()
        if note is not None:
            self.data["note"] = note
        self.save()

    def event(self, **fields) -> None:
        self.data.setdefault("history", []).append({"at": now(), **fields})


def load_lane(cfg: Config, lane_id: str) -> Lane:
    path = cfg.lanes_dir / f"{lane_id}.json"
    if not path.exists():
        raise SystemExit(f"没有需求 {lane_id}。用 lane status 查看现有需求。")
    return Lane(cfg, json.loads(path.read_text(encoding="utf-8")))


def all_lanes(cfg: Config) -> list[Lane]:
    lanes = []
    for p in sorted(cfg.lanes_dir.glob("*.json")):
        try:
            lanes.append(Lane(cfg, json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue  # 正在被另一个进程写
    return lanes


def next_id(cfg: Config) -> str:
    files = list(cfg.lanes_dir.glob("REQ-*.json")) + list((cfg.state_dir / "archive").glob("REQ-*.json"))
    nums = [int(p.stem.split("-")[1]) for p in files]  # 归档的编号也不重用
    return f"REQ-{max(nums, default=0) + 1:03d}"


# ---- 每日用量 ----------------------------------------------------------

_usage_lock = threading.Lock()  # 多个阶段线程会同时读写 usage.json


def _read_usage(cfg: Config) -> dict:
    path = cfg.state_dir / "usage.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def usage_today(cfg: Config) -> dict:
    with _usage_lock:
        return _read_usage(cfg).get(date.today().isoformat(), {})


def add_usage(cfg: Config, agent: str) -> None:
    with _usage_lock:
        data = _read_usage(cfg)
        today = data.setdefault(date.today().isoformat(), {})
        today[agent] = today.get(agent, 0) + 1
        (cfg.state_dir / "usage.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def pause_agent(cfg: Config, agent: str, minutes: float) -> None:
    """工具额度用完时暂停它一段时间。调度器在这期间不给它派活。"""
    with _usage_lock:
        path = cfg.state_dir / "pause.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[agent] = datetime.fromtimestamp(time.time() + minutes * 60).isoformat(timespec="seconds")
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def agent_paused(cfg: Config, agent: str) -> bool:
    with _usage_lock:
        path = cfg.state_dir / "pause.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return bool(data.get(agent)) and datetime.now() < datetime.fromisoformat(data[agent])


# ---- Git ---------------------------------------------------------------

def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def create_worktree(repo: Path, worktree: Path, branch: str, base: str) -> None:
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    # .lane/ 放每个阶段的提示词，不进版本库
    common = Path(git(worktree, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = worktree / common
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".lane/" not in text:
        exclude.write_text(text.rstrip("\n") + "\n.lane/\n", encoding="utf-8")


def copy_local_files(repo: Path, worktree: Path, items: list[str]) -> list[str]:
    """把不在版本库里的本地文件（如 .agents/、.env）复制进工作树。只复制被 git 忽略的路径，所以不会被提交。"""
    copied = []
    for item in items:
        src, dst = repo / item, worktree / item
        if not src.exists():
            continue
        if subprocess.run(["git", "check-ignore", "-q", item.rstrip("/")], cwd=repo).returncode != 0:
            raise RuntimeError(f"copy_files 里的 '{item}' 没有被 git 忽略，复制进工作树会被提交。请从配置里去掉它")
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied.append(item)
    return copied


def changed_files(worktree: Path) -> list[tuple[str, str]]:
    """(状态码, 路径) 列表，相对工作树，含未跟踪文件。"""
    out = git(worktree, "status", "--porcelain", "--untracked-files=all")
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            files.append((line[:2], path))
    return files


def revert_file(worktree: Path, code: str, path: str) -> None:
    if code.strip() == "??":
        try:
            (worktree / path).unlink()
        except OSError:
            pass
    else:
        git(worktree, "checkout", "HEAD", "--", path, check=False)


def commit_all(worktree: Path, message: str) -> bool:
    git(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain").strip():
        return False
    git(worktree, "commit", "-m", message)
    return True
