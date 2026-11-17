"""Notifications: tell a person when a requirement needs them.

Messages are English by default. Nothing is sent unless [jenny] notify_command is set.
The message is passed to the command in the environment variable LANE_MESSAGE. Use that in a
shell command (PowerShell: $env:LANE_MESSAGE). The {message} placeholder is still replaced in
the arguments, but do not put it inside a shell string: the text is not escaped.

Blocked requirements and other problems reach the person through Jenny's alerts (see jenny.tick),
which call send() as well, so on_stage_end() does not repeat them.
"""
from __future__ import annotations

import os
import subprocess

from .core import Config, Lane

GATE_TEXT = {
    "spec_gate": "spec is ready, waiting for your approval",
    "merge_gate": "summary is ready, waiting for your confirmation",
}


def send(cfg: Config, text: str) -> None:
    command = cfg.get("jenny", "notify_command", [])
    if not command:
        return
    try:
        subprocess.run([part.replace("{message}", text) for part in command], timeout=30, capture_output=True,
                       env={**os.environ, "LANE_MESSAGE": text})
    except Exception:
        pass  # a failed notification must never stop the scheduler


def on_stage_end(cfg: Config, lane: Lane, finished_stage: str) -> list[str]:
    """Called when a stage has finished. Sends and returns the messages for a person who should know."""
    d = lane.data
    tag = f'{lane.id} "{d["title"]}"'
    messages = []
    if d["status"] == "waiting" and d["stage"] in GATE_TEXT:
        if cfg.get("jenny", "notify_gates", True):
            messages.append(f"{tag}: {GATE_TEXT[d['stage']]}")
    elif d["status"] == "queued" and cfg.get("jenny", "notify_stage_done", False):
        last = (d.get("history") or [{}])[-1]
        if last.get("result") == "ok":   # not for a retry after a usage limit, a resume, etc.
            messages.append(f"{tag}: {finished_stage} finished, next stage: {d['stage']}")
    for text in messages:
        send(cfg, text)
    return messages
