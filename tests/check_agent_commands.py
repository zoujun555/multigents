"""检查 config/lane.toml 生成的 Claude / agy 命令行（不启动任何 Agent，不耗额度）。

用法：python tests/check_agent_commands.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LANE_CONFIG"] = str(ROOT / "config" / "lane.toml")

from lane.agents import build_command  # noqa: E402
from lane.core import Config  # noqa: E402

home = Path(tempfile.mkdtemp(prefix="lane-cmd-"))  # 状态目录放临时位置，不碰真实的 state/
cfg = Config(home)
wt = Path("worktree")
claude = shutil.which("claude")
agy = shutil.which("agy")
assert claude and agy, "本机需要能找到 claude 和 agy 命令"


def claude_cmd(stage_model: str, mode: str, effort: str, max_turns: int | None = None, model: str | None = None) -> list[str]:
    tail = ["--effort", effort] if effort else []
    if max_turns:
        tail += ["--max-turns", str(max_turns)]
    return [claude, "-p", "P", "--model", model or stage_model, "--permission-mode", mode, *tail, "--output-format", "json"]


# 默认方案 claudeplus：plan/plan_review=opus+high，review=sonnet+high，mode 取自 [agents.claude].default_mode
assert cfg.get("workbench", "profile") == "claudeplus"
assert cfg.agent("claude")["effort"] == "high"
assert build_command(cfg, cfg.stage("plan"), "P", wt) == claude_cmd("opus", "acceptEdits", "high", 25)
assert build_command(cfg, cfg.stage("plan_review"), "P", wt) == claude_cmd("opus", "acceptEdits", "high", 12)
assert build_command(cfg, cfg.stage("review"), "P", wt) == claude_cmd("sonnet", "acceptEdits", "high", 15)

# 阶段覆盖 effort 为空时，--effort 连同它的值一起省略；未设 max_turns 时也省略 --max-turns
assert build_command(cfg, {**cfg.stage("plan"), "effort": "", "max_turns": ""}, "P", wt) == claude_cmd("opus", "acceptEdits", "")

# 阶段未配置 effort 时回退到 [agents.claude] 的默认 effort
plan_no_eff = {k: v for k, v in cfg.stage("plan").items() if k != "effort"}
cfg.agent("claude")["effort"] = "low"
assert build_command(cfg, plan_no_eff, "P", wt) == claude_cmd("opus", "acceptEdits", "low", 25)

# agy 阶段（含 report）：模型 ID 自带级别，mode 取自 [agents.agy].default_mode，命令里不出现 --effort
for name in ("code", "fix", "test", "itest", "report"):
    stage = cfg.stage(name)
    assert stage["model"] == "gemini-3.8-flash-high", (name, stage["model"])
    expected = [agy, "-p", "P", "--model", "gemini-3.8-flash-high", "--mode", "accept-edits",
                "--print-timeout", f"{int(stage['timeout_min'] * 60)}s", "--output-format", "json", "--settings", "S.json"]
    assert build_command(cfg, stage, "P", wt, settings="S.json") == expected, name

# 切到 agy-only 方案：plan/plan_review 用 gemini-4-argon-high，review 用 gemini-4-neon-high，自动用 accept-edits
(home / "config").mkdir(parents=True, exist_ok=True)
(home / "config" / "local.toml").write_text('[workbench]\nprofile = "agy-only"\n', encoding="utf-8")
cfg_agy = Config(home)
assert build_command(cfg_agy, cfg_agy.stage("plan_review"), "P", wt, settings="S.json") == [
    agy, "-p", "P", "--model", "gemini-4-argon-high", "--mode", "accept-edits",
    "--print-timeout", "900s", "--output-format", "json", "--settings", "S.json"
]
assert build_command(cfg_agy, cfg_agy.stage("review"), "P", wt, settings="S.json") == [
    agy, "-p", "P", "--model", "gemini-4-neon-high", "--mode", "accept-edits",
    "--print-timeout", "1200s", "--output-format", "json", "--settings", "S.json"
]

shutil.rmtree(home)
print("全部通过")
