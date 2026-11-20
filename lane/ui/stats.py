"""统计：从每条需求记录的历史和每日用量里算出来，不另外存数据。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

from .. import tokens
from ..core import AUTO_STAGES, Config


def _minutes(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


def _load_lanes(cfg: Config) -> list[dict]:
    lanes = []
    for folder in (cfg.lanes_dir, cfg.state_dir / "archive"):
        for path in sorted(folder.glob("REQ-*.json")):
            try:
                lanes.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return lanes


def compute(cfg: Config, project: str = "", date_from: str = "", date_to: str = "") -> dict:
    lanes = [d for d in _load_lanes(cfg)
             if (not project or d["project"] == project)
             and (not date_from or d["created"][:10] >= date_from)
             and (not date_to or d["created"][:10] <= date_to)]

    status = Counter(d["status"] for d in lanes)
    stage_runs: dict[str, list[float]] = defaultdict(list)
    stage_bad: Counter = Counter()
    blocked_by_stage: Counter = Counter()
    blocked_notes: Counter = Counter()
    reviewed = first_pass = 0
    cycle: list[float] = []

    for d in lanes:
        runs = [h for h in d.get("history", []) if "started" in h and h.get("stage") in AUTO_STAGES]
        for h in runs:
            stage_runs[h["stage"]].append(_minutes(h["started"], h["at"]))
            if h.get("result") in ("timeout", "error"):
                stage_bad[h["stage"]] += 1
            if h.get("status") == "blocked":
                blocked_by_stage[h["stage"]] += 1
                blocked_notes[(h.get("note") or "")[:80]] += 1
        reviews = [h for h in runs if h["stage"] == "review"]
        if reviews:
            reviewed += 1
            first_pass += reviews[0].get("next") == "test"
        if d["status"] == "done" and d.get("history"):
            cycle.append(_minutes(d["created"], d["history"][-1]["at"]))

    stages = [{"stage": s, "runs": len(stage_runs[s]),
               "avg_min": round(sum(stage_runs[s]) / len(stage_runs[s]), 1) if stage_runs[s] else 0,
               "total_min": round(sum(stage_runs[s]), 1),
               "failed": stage_bad[s], "blocked": blocked_by_stage[s]} for s in AUTO_STAGES]

    usage_path = cfg.state_dir / "usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else {}
    days = [{"date": day, **counts} for day, counts in sorted(usage.items())
            if (not date_from or day >= date_from) and (not date_to or day <= date_to)][-30:]

    return {
        "total": len(lanes),
        "by_status": dict(status),
        "avg_rounds": round(sum(d.get("round", 0) for d in lanes) / len(lanes), 2) if lanes else 0,
        "reviewed": reviewed,
        "review_first_pass": first_pass,
        "avg_cycle_min": round(sum(cycle) / len(cycle), 1) if cycle else 0,
        "done": len(cycle),
        "stages": stages,
        "blocked_notes": [{"note": n, "count": c} for n, c in blocked_notes.most_common(8)],
        "usage_days": days,
        "tokens": tokens.summarize(cfg, project, date_from, date_to),
        "agents": sorted(cfg.data.get("agents", {})),
        "budget": cfg.data.get("budget", {}),
        "projects": sorted(cfg.projects),
    }
