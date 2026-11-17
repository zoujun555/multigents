"""Jenny：持续跟踪进展，发现问题就上报。纯脚本，不调用模型，随调度器每轮运行一次。

产出：
  state/alerts.jsonl  告警历史，所有项目合在一份里，每条带 project 字段（只追加新的）
当前状态和当前告警由看板（lane ui）和 lane status 实时算出，不再写成文件。
"""
from __future__ import annotations

import json

from . import notify
from .core import STAGE_LABEL, STATUS_LABEL, Config, Lane, age_minutes, all_lanes, now, usage_today


def fmt_age(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f} 小时"
    return f"{minutes / 1440:.1f} 天"


def next_step(lane: Lane) -> str:
    d = lane.data
    if d["status"] == "waiting" and d["stage"] == "spec_gate":
        return f"读 spec.md → `lane approve {lane.id}` 或 `lane reject {lane.id} -m 意见`"
    if d["status"] == "waiting" and d["stage"] == "merge_gate":
        return f"读 summary.md → `lane approve {lane.id}` 或 `lane reject {lane.id} -m 意见`"
    if d["status"] == "blocked":
        return f"处理后 `lane resume {lane.id}`（可加 --stage 指定从哪个阶段继续）"
    return ""


def find_alerts(cfg: Config, lanes: list[Lane], waiting_budget: list[str]) -> dict[str, dict]:
    """返回 {去重键: {"project", "lane", "text"}}。同一个键只上报一次，直到情况消失后再次出现。
    与具体需求无关的告警（额度）project 和 lane 为空。"""
    alerts: dict[str, dict] = {}
    stale = cfg.get("jenny", "stale_minutes", 10)
    remind = cfg.get("jenny", "gate_reminder_hours", 4) * 60

    def add(key: str, lane: Lane | None, text: str) -> None:
        alerts[key] = {"project": lane.data["project"] if lane else "", "lane": lane.id if lane else "", "text": text}

    for lane in lanes:
        d = lane.data
        tag = f"{lane.id}「{d['title']}」"
        if d["status"] == "blocked":
            add(f"{lane.id}:blocked:{d.get('since')}", lane, f"{tag} 在{STAGE_LABEL[d['stage']]}阶段卡住：{d.get('note', '')}")
        elif d["status"] == "running" and age_minutes(d.get("heartbeat")) > stale:
            add(f"{lane.id}:stale:{d.get('since')}", lane,
                f"{tag} 的{STAGE_LABEL[d['stage']]}阶段已 {fmt_age(age_minutes(d.get('heartbeat')))}没有心跳，"
                "调度器可能已停止（重新运行 lane run 会自动重排）")
        elif d["status"] == "waiting":
            waited = age_minutes(d.get("since"))
            if waited > remind:
                bucket = int(waited // remind)  # 每过一个提醒周期再报一次
                add(f"{lane.id}:gate:{d.get('since')}:{bucket}", lane, f"{tag} {STAGE_LABEL[d['stage']]}已 {fmt_age(waited)}")
        if d["status"] != "blocked" and d.get("round", 0) >= cfg.get("review", "max_rounds", 2) and d["stage"] in ("fix", "review"):
            add(f"{lane.id}:lastround", lane, f"{tag} 已进入最后一轮修复，再不通过会停下等你")

    used, ratio = usage_today(cfg), cfg.get("jenny", "budget_warn_ratio", 0.8)
    for agent, limit in cfg.data.get("budget", {}).items():
        n = used.get(agent, 0)
        if n >= limit:
            add(f"budget:{agent}:full:{now()[:10]}", None, f"{agent} 今天已用完 {limit} 次额度，相关需求排队到明天或等你调高 [budget]")
        elif n >= limit * ratio:
            add(f"budget:{agent}:warn:{now()[:10]}", None, f"{agent} 今天已用 {n}/{limit} 次")
    if waiting_budget:
        add(f"budget:waiting:{now()[:10]}:{','.join(waiting_budget)}", None, "因额度用完而排队的需求：" + "、".join(waiting_budget))
    return alerts


def render_status(cfg: Config, lanes: list[Lane], alerts: dict[str, dict]) -> str:
    """给 lane status 打印的文字。不写文件。"""
    used = usage_today(cfg)
    budget = cfg.data.get("budget", {})
    running = [l for l in lanes if l.data["status"] == "running"]
    lines = [
        "# 工作台状态", "",
        f"更新时间：{now().replace('T', ' ')}　|　运行中 {len(running)}/{cfg.get('workbench', 'max_parallel', 2)}　|　今日用量："
        + "，".join(f"{a} {used.get(a, 0)}/{budget[a]}" for a in budget), "",
    ]
    need_you = [l for l in lanes if l.data["status"] in ("waiting", "blocked")]
    if need_you:
        lines += ["## 需要你处理", ""]
        for l in need_you:
            lines.append(f"- **{l.id}**「{l.data['title']}」{STAGE_LABEL[l.data['stage']]}"
                         + (f"：{l.data['note']}" if l.data.get("note") else "") + f"  \n  {next_step(l)}")
        lines.append("")
    lines += ["## 全部需求", "", "| 编号 | 项目 | 标题 | 阶段 | 状态 | 修复轮次 | 已持续 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for l in lanes:
        d = l.data
        lines.append(f"| {l.id} | {d['project']} | {d['title']} | {STAGE_LABEL[d['stage']]} | {STATUS_LABEL[d['status']]} "
                     f"| {d.get('round', 0)} | {fmt_age(age_minutes(d.get('since')))} | {d.get('note', '')} |")
    if not lanes:
        lines.append("| （还没有需求，用 `lane new` 创建） | | | | | | | |")
    if alerts:
        lines += ["", "## 当前告警", ""] + [f"- {a['text']}" for a in alerts.values()]
    return "\n".join(lines) + "\n"


def alert_history(cfg: Config, project: str = "", limit: int = 200) -> list[dict]:
    """告警历史，最新的在前。project 非空时只要该项目的（与具体项目无关的额度告警不显示）。"""
    path = cfg.state_dir / "alerts.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue   # 写到一半的行
    if project:
        entries = [e for e in entries if e.get("project") == project]
    return entries[::-1][:limit]


def tick(cfg: Config, waiting_budget: list[str] | None = None) -> list[str]:
    """跑一轮检查，把本轮新出现的告警追加到历史并通知，返回它们的文字。"""
    alerts = find_alerts(cfg, all_lanes(cfg), waiting_budget or [])

    seen_path = cfg.state_dir / "jenny.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    fresh = [a for key, a in alerts.items() if key not in seen]
    seen_path.write_text(json.dumps(sorted(alerts)), encoding="utf-8")  # 只记当前仍存在的
    if fresh:
        with open(cfg.state_dir / "alerts.jsonl", "a", encoding="utf-8") as f:
            for a in fresh:
                f.write(json.dumps({"at": now(), **a}, ensure_ascii=False) + "\n")
        for a in fresh:
            notify.send(cfg, a["text"])
    return [a["text"] for a in fresh]
