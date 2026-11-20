"""AGY 主控入口（multiagents-lane）的原子命令：状态同步、0-Token 预检、受控 Claude 轻评审、共享资源锁。"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import ops, preflight, tokens
from .agents import render_prompt, run_agent
from .core import STAGE_LABEL, Config, Lane, commit_all, git, load_lane, now
from .runner import enforce_scope, read_mark, usage_limited

VALID_STATUSES = ("queued", "running", "waiting", "blocked", "done")
VALID_TIERS = ("standard", "light")
RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _write_worktree_bootstrap(cfg: Config, lane: Lane) -> str:
    """在 worktree 的 .lane/（已加入 .git/info/exclude）生成需求上下文与零路径调用引导器。"""
    lane_dir = lane.worktree / ".lane"
    lane_dir.mkdir(exist_ok=True)
    ctx = {
        "id": lane.id, "project": lane.data["project"],
        "tier": lane.data.get("tier", "standard"), "branch": lane.data["branch"],
    }
    (lane_dir / "context.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    home = str(cfg.home.resolve())
    pkg_root = str(Path(__file__).resolve().parent.parent)
    py = sys.executable

    cmd_lines = [
        "@echo off",
        "setlocal",
        f'set "LANE_HOME={home.replace("%", "%%")}"',
        f'set "PYTHONPATH={pkg_root.replace("%", "%%")};%PYTHONPATH%"',
        f'"{py.replace("%", "%%")}" -m lane %*',
    ]
    (lane_dir / "lane.cmd").write_bytes(("\r\n".join(cmd_lines) + "\r\n").encode("utf-8"))

    sh_lines = [
        "#!/usr/bin/env sh",
        f"export LANE_HOME={shlex.quote(home)}",
        f'export PYTHONPATH={shlex.quote(pkg_root)}"${{PYTHONPATH:+:$PYTHONPATH}}"',
        f"py=${{LANE_PYTHON:-{shlex.quote(py)}}}",
        'exec "$py" -m lane "$@"',
    ]
    sh_path = lane_dir / "lane.sh"
    sh_path.write_bytes(("\n".join(sh_lines) + "\n").encode("utf-8"))
    if os.name != "nt":
        sh_path.chmod(0o755)
    return ".lane/lane.cmd" if os.name == "nt" else ".lane/lane.sh"


def init_lane(cfg: Config, project: str, title: str, text: str = "", tier: str = "standard") -> dict:
    """新建需求（独立 worktree），标记为 AGY 驱动并返回工作树与编号信息。"""
    if tier not in VALID_TIERS:
        raise SystemExit(f"tier 只能是 {' / '.join(VALID_TIERS)}")
    msg = ops.new(cfg, project, title, text)
    m = re.search(r"REQ-\d+", msg)
    if not m:
        raise SystemExit(f"无法解析新建的需求编号：{msg}")
    lane = load_lane(cfg, m.group(0))
    lane.data["tier"] = tier
    lane.data["driver"] = "agy"
    lane.data["heartbeat"] = now()
    lane.set(stage="plan", status="running", note=f"AGY 驱动（{tier}）")
    launcher = _write_worktree_bootstrap(cfg, lane)
    return {
        "id": lane.id, "project": project, "tier": tier, "branch": lane.data["branch"],
        "worktree": str(lane.worktree), "req_dir": str(lane.req_dir), "launcher": launcher,
    }


def set_stage(cfg: Config, lane_id: str, stage: str, status: str, note: str = "",
              commit: bool = False, result: str = "ok") -> dict:
    """更新需求阶段、状态与心跳，使 Web 监控页实时同步。"""
    if stage not in STAGE_LABEL:
        raise SystemExit(f"未知阶段 '{stage}'，可选：{', '.join(STAGE_LABEL)}")
    if status not in VALID_STATUSES:
        raise SystemExit(f"未知状态 '{status}'，可选：{', '.join(VALID_STATUSES)}")
    lane = load_lane(cfg, lane_id)
    if commit:
        commit_all(lane.worktree, f"{lane.id}: {stage}")
    lane.data["heartbeat"] = now()
    if status != "running" or commit:
        lane.event(stage=stage, result=result, status=status, note=note)
    lane.set(stage=stage, status=status, note=note)
    return {"id": lane.id, "stage": stage, "status": status, "note": note}


def run_preflight_cmd(cfg: Config, lane_id: str, skip_verify: bool = False) -> tuple[int, dict]:
    """执行 0-Token 预检门禁并生成评审上下文包。返回 (退出码, 结果字典)。"""
    lane = load_lane(cfg, lane_id)
    lane.data["heartbeat"] = now()
    res = preflight.run_preflight(cfg, lane, skip_verify=skip_verify)
    ok = res["status"] == "PASS"
    note = "预检通过" if ok else f"预检未通过（{len(res['errors'])} 项）"
    lane.event(stage="preflight", result="ok" if ok else "error", status=lane.data["status"], note=note)
    lane.save()
    return (0 if ok else 1), res


def _archive_stale(lane: Lane, filename: str) -> None:
    target = lane.req_dir / filename
    if target.exists():
        prev = lane.req_dir / "history"
        prev.mkdir(exist_ok=True)
        n = len(list(prev.glob(f"*-{filename}"))) + 1
        target.replace(prev / f"{n:02d}-{filename}")


def run_review(cfg: Config, lane_id: str, kind: str = "code", agent: str | None = None,
               model: str | None = None, effort: str | None = None) -> tuple[int, dict]:
    """受控调用评审 Agent 执行规划轻审或代码评审（支持按 profile/local.toml 或单次参数覆写工具、模型与推理深度）。"""
    if kind not in ("plan", "code"):
        raise SystemExit("kind 只能是 plan 或 code")
    lane = load_lane(cfg, lane_id)
    if kind == "plan" and lane.data.get("tier") == "light":
        lane.event(stage="plan_review", result="ok", note="light 档跳过规划评审")
        lane.save()
        return 0, {"id": lane.id, "kind": "plan", "verdict": "SKIP", "reason": "light tier"}

    stage = "plan_review" if kind == "plan" else "review"
    out_file = "spec-review.md" if kind == "plan" else "review.md"
    valid_marks = ("PASS", "REVISE") if kind == "plan" else ("PASS", "FAIL")
    stage_cfg = dict(cfg.stage(stage))
    if agent:
        if agent not in cfg.data.get("agents", {}):
            raise SystemExit(f"配置里没有工具 '{agent}'。可选：{', '.join(sorted(cfg.data.get('agents', {})))}")
        if agent != stage_cfg.get("agent"):
            stage_cfg.pop("mode", None)  # 跨工具覆写时改用新工具的 default_mode
        stage_cfg["agent"] = agent
    if model is not None:
        stage_cfg["model"] = model
    if effort is not None:
        stage_cfg["effort"] = effort

    if kind == "code" and not (lane.worktree / ".lane" / "review-pack.md").exists():
        preflight.build_review_pack(cfg, lane)
    _archive_stale(lane, out_file)

    started = now()
    lane.data["heartbeat"] = started
    lane.set(stage=stage, status="running", note="")
    outcome, log_path = run_agent(cfg, lane, stage, stage_cfg, render_prompt(cfg, lane, stage))
    violations, policy = enforce_scope(cfg, lane, stage)
    verdict = read_mark(lane.req_dir / out_file)

    if verdict not in valid_marks:
        note = f"{stage_cfg['agent']} 额度受限" if usage_limited(log_path) else f"未产出带有效标记的 {out_file}"
        status, nxt = "blocked", stage
        rc = 1
    else:
        rc = 0
        if kind == "plan":
            nxt, status, note = "plan_review", "running", f"规划评审结论：{verdict}"
        elif verdict == "PASS":
            nxt, status, note = "test", "running", "代码评审通过"
        else:
            lane.data["round"] = lane.data.get("round", 0) + 1
            nxt, status, note = "fix", "running", f"代码评审未通过（第 {lane.data['round']} 轮）"

    commit_all(lane.worktree, f"{lane.id}: {stage}")
    if kind == "code" and verdict in valid_marks:
        lane.data["last_review_sha"] = git(lane.worktree, "rev-parse", "HEAD").strip()

    usage = tokens.parse_log_usage(log_path) or {}
    lane.event(stage=stage, started=started, result=outcome, next=nxt, status=status, note=note,
               log=str(log_path), reverted=violations if policy == "revert" else [])
    lane.set(stage=nxt, status=status, note=note)
    return rc, {
        "id": lane.id, "kind": kind, "agent": stage_cfg["agent"], "model": stage_cfg.get("model", ""),
        "effort": stage_cfg.get("effort", ""), "verdict": verdict or "NONE",
        "file": str(lane.req_dir / out_file), "log": str(log_path), "usage": usage, "note": note,
    }


def finish_lane(cfg: Config, lane_id: str, clean: bool = False) -> str:
    """确认合并闸门完成；加 clean=True 时同时移除工作树并归档。"""
    lane = load_lane(cfg, lane_id)
    msgs = []
    if lane.data["status"] != "done":
        if lane.data["stage"] == "merge_gate" and lane.data["status"] == "waiting":
            msgs.append(ops.approve(cfg, lane_id))
        else:
            lane.event(stage="merge_gate", result="approved", note="AGY 确认完成")
            lane.set(stage="done", status="done", note="")
            msgs.append(f"{lane.id} 已标记完成。")
    if clean:
        msgs.append(ops.clean(cfg, lane_id))
    return "\n".join(msgs)


def _lock_path(cfg: Config, resource: str) -> Path:
    if not RESOURCE_NAME.fullmatch(resource or ""):
        raise SystemExit("资源锁名称只能包含字母、数字、- 和 _")
    return cfg.state_dir / f"lock-{resource}.json"


def acquire_lock(cfg: Config, resource: str, lane_id: str) -> dict:
    """获取跨会话共享资源锁（例如 itest 共享测试库）。已被他人占用时抛 SystemExit。"""
    path = _lock_path(cfg, resource)
    payload = {"resource": resource, "lane": lane_id, "at": now()}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            holder = {}
        if holder.get("lane") == lane_id:
            return holder
        raise SystemExit(f"资源 '{resource}' 正被 {holder.get('lane', '未知需求')} 占用（自 {holder.get('at', '?')}）")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def release_lock(cfg: Config, resource: str, lane_id: str, force: bool = False) -> str:
    """释放共享资源锁；默认只允许持有者释放。"""
    path = _lock_path(cfg, resource)
    if not path.exists():
        return f"资源 '{resource}' 当前未加锁"
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        holder = {}
    if not force and holder.get("lane") not in (None, lane_id):
        raise SystemExit(f"资源 '{resource}' 由 {holder.get('lane')} 持有，不能由 {lane_id} 释放（强制释放加 --force）")
    path.unlink(missing_ok=True)
    return f"已释放资源 '{resource}' 的锁"


def format_profile_table(cfg: Config) -> str:
    """渲染当前生效的方案预设和各阶段实际使用的工具、模型、推理深度。"""
    active = str(cfg.get("workbench", "profile", "") or "（默认）")
    names = sorted(cfg.data.get("profiles", {}))
    lines = [f"当前方案：{active}", f"可选方案：{', '.join(names) or '（无）'}", "",
             f"{'阶段':12} {'工具':8} {'模型':24} {'推理深度':10} {'轮次上限'}"]
    for name, sc in cfg.data.get("stages", {}).items():
        agent_cfg = cfg.agent(sc["agent"])
        cmd_str = " ".join(agent_cfg.get("command", []))
        effort = (sc["effort"] if "effort" in sc else agent_cfg.get("effort", "")) if "{effort}" in cmd_str else "-"
        turns = str(sc.get("max_turns", "")) if "{max_turns}" in cmd_str and sc.get("max_turns") else "-"
        lines.append(f"{name:12} {sc['agent']:8} {sc.get('model', '') or '-':24} {effort or '-':10} {turns}")
    return "\n".join(lines)


def switch_profile(cfg: Config, name: str) -> str:
    """把 config/local.toml 中 [workbench] 的 profile 切换到指定方案（保留其余内容与注释）。"""
    profiles = cfg.data.get("profiles", {})
    if name not in profiles:
        raise SystemExit(f"没有方案 '{name}'。可选：{', '.join(sorted(profiles)) or '（无）'}")
    local = cfg.home / "config" / "local.toml"
    local.parent.mkdir(parents=True, exist_ok=True)
    text = local.read_text(encoding="utf-8") if local.exists() else ""
    lines = text.splitlines()
    out, in_wb, replaced, wb_idx = [], False, False, -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_wb = (stripped == "[workbench]")
            if in_wb:
                wb_idx = len(out)
        elif in_wb and re.match(r"^profile\s*=", stripped):
            out.append(f'profile = "{name}"')
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if wb_idx != -1:
            out.insert(wb_idx + 1, f'profile = "{name}"')
        else:
            out = ["[workbench]", f'profile = "{name}"', "", *out]
    tmp = local.with_suffix(".tmp")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, local)
    return f"已切换方案为 '{name}'（写入 {local}）\n\n" + format_profile_table(Config(cfg.home))
