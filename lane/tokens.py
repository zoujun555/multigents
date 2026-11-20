"""解析并记录各阶段日志里的 Token 与费用（支持 Claude JSON 与 agy JSON）。"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

from .core import Config, now

_tokens_lock = threading.Lock()


def _extract_last_json(text: str) -> dict | None:
    """从日志文本末尾提取最后一个带用量信息的 JSON 对象（忽略日志头与普通文本）。"""
    dec = json.JSONDecoder()
    tail = text[-131072:]
    starts = [i for i, ch in enumerate(tail) if ch == "{" and (i == 0 or tail[i - 1] in "\r\n")]
    for pos in reversed(starts):
        try:
            obj, _ = dec.raw_decode(tail, pos)
            if isinstance(obj, dict) and ("usage" in obj or "total_cost_usd" in obj or "modelUsage" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_log_usage(log_path: Path) -> dict | None:
    """从单次阶段日志里提取标准化 Token 统计；非 JSON 或无 usage 时返回 None。"""
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    obj = _extract_last_json(text)
    if not obj:
        return None
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    if not usage and isinstance(obj.get("modelUsage"), dict):
        # Claude Code 部分版本汇总在 modelUsage 下
        merged = defaultdict(int)
        for m in obj["modelUsage"].values():
            if isinstance(m, dict):
                for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
                    merged[k] += int(m.get(k) or 0)
        usage = {
            "input_tokens": merged["inputTokens"],
            "output_tokens": merged["outputTokens"],
            "cache_read_input_tokens": merged["cacheReadInputTokens"],
            "cache_creation_input_tokens": merged["cacheCreationInputTokens"],
        }
    if not usage and "total_cost_usd" not in obj:
        return None
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "thinking_tokens": int(usage.get("thinking_tokens") or 0),
        "cost_usd": round(float(obj.get("total_cost_usd") or obj.get("cost_usd") or 0.0), 6),
        "num_turns": int(obj.get("num_turns") or 0),
    }


def _save_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def record_from_log(cfg: Config, lane_id: str, project: str, stage: str, agent: str, model: str,
                    log_path: Path) -> dict | None:
    """解析日志里的 Token 用量并追加写入 state/tokens.json。"""
    parsed = parse_log_usage(log_path)
    if not parsed:
        return None
    entry = {
        "at": now(),
        "date": date.today().isoformat(),
        "lane": lane_id,
        "project": project,
        "stage": stage,
        "agent": agent,
        "model": model,
        **parsed,
    }
    path = cfg.state_dir / "tokens.json"
    with _tokens_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
        except (json.JSONDecodeError, OSError):
            data = {"entries": []}
        data.setdefault("entries", []).append(entry)
        _save_atomic(path, data)
    return entry


def summarize(cfg: Config, project: str = "", date_from: str = "", date_to: str = "") -> list[dict]:
    """按 agent 汇总 Token 和费用，供统计页展示。"""
    path = cfg.state_dir / "tokens.json"
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    except (json.JSONDecodeError, OSError):
        return []
    grouped: dict[str, dict] = {}
    for e in entries:
        if project and e.get("project") != project:
            continue
        day = e.get("date") or (e.get("at") or "")[:10]
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        agent = e.get("agent", "unknown")
        row = grouped.setdefault(agent, {
            "agent": agent, "runs": 0, "input_tokens": 0, "cache_creation_tokens": 0,
            "cache_read_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.0,
        })
        row["runs"] += 1
        for k in ("input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "thinking_tokens"):
            row[k] += int(e.get(k) or 0)
        row["cost_usd"] = round(row["cost_usd"] + float(e.get("cost_usd") or 0.0), 6)
    return [grouped[k] for k in sorted(grouped)]
