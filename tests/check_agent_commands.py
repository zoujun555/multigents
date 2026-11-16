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


def claude_cmd(stage_model: str, mode: str, effort: str, model: str | None = None) -> list[str]:
    tail = ["--effort", effort] if effort else []
    return [claude, "-p", "P", "--model", model or stage_model, "--permission-mode", mode, *tail, "--output-format", "text"]


# 规划、评审、汇总三个 Claude 阶段：模型来自阶段配置，effort 统一来自 [agents.claude]
assert cfg.agent("claude")["effort"] == "high"
assert build_command(cfg, cfg.stage("plan"), "P", wt) == claude_cmd("opus", "acceptEdits", "high")
assert build_command(cfg, cfg.stage("review"), "P", wt) == claude_cmd("sonnet", "acceptEdits", "high")
assert build_command(cfg, cfg.stage("report"), "P", wt) == claude_cmd("sonnet", "acceptEdits", "high")

# 诊断步骤：用报告阶段的配置，模型被覆盖成 haiku，同样带 --effort high（不豁免）
assert build_command(cfg, cfg.stage("report"), "P", wt, model="haiku") == claude_cmd("sonnet", "acceptEdits", "high", model="haiku")

# effort 改成别的值，命令里同步变化
cfg.agent("claude")["effort"] = "medium"
assert build_command(cfg, cfg.stage("plan"), "P", wt) == claude_cmd("opus", "acceptEdits", "medium")

# effort 留空：--effort 连同它的值一起省略
cfg.agent("claude")["effort"] = ""
assert build_command(cfg, cfg.stage("plan"), "P", wt) == claude_cmd("opus", "acceptEdits", "")

# agy 阶段：模型 ID 自带级别，命令里不出现 --effort
for name in ("code", "fix", "test", "itest"):
    stage = cfg.stage(name)
    assert stage["model"] == "gemini-3.8-flash-high", (name, stage["model"])
    expected = [agy, "-p", "P", "--model", "gemini-3.8-flash-high", "--mode", "accept-edits",
                "--print-timeout", f"{int(stage['timeout_min'] * 60)}s", "--output-format", "json", "--settings", "S.json"]
    assert build_command(cfg, stage, "P", wt, settings="S.json") == expected, name

shutil.rmtree(home)
print("全部通过")
