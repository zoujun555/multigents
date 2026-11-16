"""提示词渲染和无头工具的调用。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .core import Config, Lane, add_usage, now


# ---- 提示词和命令 ------------------------------------------------------

HEADLESS_NOTE = (
    "以下是本项目的规则，必须遵守。规则是为有人在场的对话写的；你在无头流水线里运行，所以："
    "凡是规则要求「先给思路等确认」「停下提问」或「等待确认」的地方，一律改为把问题或方案写进本阶段的结果文件并标记 BLOCKED，然后结束；"
    "关于回复语言和排版的规则不适用于结果文件。\n\n")


def load_rules(cfg: Config, proj: dict) -> str:
    rel = proj.get("rules")
    if not rel:
        return "（本项目没有配置规则文件，按仓库现有代码的风格。）"
    path = Path(rel) if Path(rel).is_absolute() else cfg.home / rel
    if not path.exists():
        raise FileNotFoundError(f"规则文件不存在：{path}。用 lane rules <项目> 创建")
    return HEADLESS_NOTE + path.read_text(encoding="utf-8-sig").strip()


def render_verify(lane: Lane, proj: dict, full: bool) -> str:
    """把项目的 [verify] 配置渲染成给 Agent 的步骤清单。full=False 只含还原、构建、单元测试。"""
    v = proj.get("verify", {})
    port = str(v.get("port_base", 64000) + int(lane.id.split("-")[1]))

    def fill(s: str) -> str:
        return s.replace("{port}", port).replace("{worktree}", str(lane.worktree))

    steps = [("setup_cmd", "还原依赖（新工作树里没有依赖包，先做这一步）"), ("build_cmd", "构建"), ("unit_cmd", "单元测试")]
    lines = [f"- {label}：`{fill(v[key])}`" for key, label in steps if v.get(key)]
    if full and v.get("serve_cmd"):
        lines.append(f"- 启动被测应用（从本工作树启动，后台运行；端口 {port} 是分配给本需求的，不要换）：`{fill(v['serve_cmd'])}`")
        if v.get("ready_url"):
            lines.append(f"- 就绪判断：`{fill(v['ready_url'])}` 能访问之后再开始测试")
    if full:
        for key, val in v.get("env", {}).items():
            lines.append(f"- 运行自动化测试的那条命令执行前，设置环境变量 `{key}={fill(val)}`")
        if v.get("ta_cmd"):
            lines.append(f"- 自动化测试：`{fill(v['ta_cmd'])}`")
    if full and v.get("serve_cmd"):
        lines.append("- 测试结束后（无论成败）停掉你启动的应用进程，不要留下后台进程。")
    if not lines:
        return "（本项目没有配置验证命令，请从仓库的说明文件里找构建和测试方式。）"
    return "\n".join(lines)


def allow_rules(cfg: Config, proj: dict, agent: str) -> list[str]:
    """该工具在本项目里可以不经询问执行的命令（正则）。只对命令模板里带 {settings} 的工具有效。"""
    spec = cfg.agent(agent)
    if not any("{settings}" in arg for arg in spec["command"]):
        return []
    return list(spec.get("allow_commands", [])) + list(proj.get("allow_commands", []))


def command_note(rules: list[str]) -> str:
    if not rules:
        return ""
    return ("\n\n**命令权限（重要）**：你在无头模式下运行。执行任何不在允许列表里的命令，都会被拒绝，并且**整个会话会立刻结束**，"
            "你来不及写结果文件。所以只执行符合下列形式的命令，一次一条，不要用 `;` `&&` `||` `|` 串联，也不要换一种写法去绕。"
            "确实需要其他命令时不要尝试：把需要的命令写进结果文件并标记 BLOCKED。读文件、搜索、编辑文件请用你自带的工具，不要用命令。\n\n"
            "允许的命令（正则，从命令开头匹配）：\n" + "\n".join(f"- `{r}`" for r in rules))


def write_settings(lane: Lane, stage: str, rules: list[str]) -> str:
    """把允许规则写成本次会话用的 settings 文件（不保存到工具的全局配置）。没有规则时返回空串。"""
    if not rules:
        return ""
    path = lane.worktree / ".lane" / f"settings-{stage}.json"
    path.write_text(json.dumps({"permissions": {"allow": [f"command(regex:{r})" for r in rules]}}, ensure_ascii=False),
                    encoding="utf-8")
    return str(path)


def render_prompt(cfg: Config, lane: Lane, stage: str) -> str:
    proj = cfg.project(lane.data["project"])
    stage_cfg = cfg.data["stages"].get(stage)
    note = command_note(allow_rules(cfg, proj, stage_cfg["agent"])) if stage_cfg else ""
    text = (cfg.home / "prompts" / f"{stage}.md").read_text(encoding="utf-8")
    values = {
        "id": lane.id,
        "title": lane.data["title"],
        "project": lane.data["project"],
        "req_dir": lane.req_rel,
        "base": proj.get("base", "main"),
        "round": str(lane.data.get("round", 0)),
        "max_rounds": str(cfg.get("review", "max_rounds", 2)),
        "verify_build": render_verify(lane, proj, full=False) + note,
        "verify_full": render_verify(lane, proj, full=True) + note,
        "test_paths": "、".join(proj.get("test_paths", [])) or "（未配置）",
        "rules": load_rules(cfg, proj),
        "coveragehub_project": proj.get("coveragehub_project", ""),
    }
    if stage == "itest":  # 配了 CoverageHub 的项目，在排队的集成运行里对接它
        values["coveragehub_body"] = ((cfg.home / "prompts" / "itest-coveragehub.md").read_text(encoding="utf-8")
                                      if proj.get("coveragehub_project") else "")
    for _ in range(2):  # 第二遍替换 coveragehub_body 里的占位符
        for key, val in values.items():
            text = text.replace("{{" + key + "}}", val)
    return text


def build_command(cfg: Config, stage_cfg: dict, prompt_arg: str, worktree: Path, model: str | None = None,
                  settings: str = "") -> list[str]:
    values = {
        "settings": settings,
        "prompt": prompt_arg,
        "model": stage_cfg.get("model", "") if model is None else model,
        "mode": stage_cfg.get("mode", ""),
        "effort": cfg.agent(stage_cfg["agent"]).get("effort", ""),
        "timeout": str(int(stage_cfg.get("timeout_min", 30) * 60)),
        "worktree": str(worktree),
    }
    cmd: list[str] = []
    for arg in cfg.agent(stage_cfg["agent"])["command"]:
        keys = re.findall(r"\{(\w+)\}", arg)
        if any(k in values and values[k] == "" for k in keys):
            if cmd and cmd[-1].startswith("-"):
                cmd.pop()  # 值为空：连同前面的 --flag 一起省略
            continue
        for k in keys:
            if k in values:
                arg = arg.replace("{" + k + "}", values[k])
        cmd.append(arg)
    exe = shutil.which(cmd[0])
    if not exe:
        raise FileNotFoundError(f"找不到命令 '{cmd[0]}'，请安装或在 config/lane.toml 的 [agents] 里改路径")
    cmd[0] = exe
    return cmd


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def run_agent(cfg: Config, lane: Lane, stage: str, stage_cfg: dict, prompt_text: str,
              model: str | None = None) -> tuple[str, Path]:
    """返回 (ok | timeout | error, 日志路径)。运行期间每几秒写一次心跳。"""
    lane_dir = lane.worktree / ".lane"
    lane_dir.mkdir(exist_ok=True)
    (lane_dir / f"prompt-{stage}.md").write_text(prompt_text, encoding="utf-8")
    prompt_arg = f"Read the file .lane/prompt-{stage}.md in the current directory and follow its instructions exactly."

    log_dir = cfg.logs_dir / lane.id
    log_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(log_dir.glob("*.log"))) + 1
    log_path = log_dir / f"{seq:02d}-{stage}.log"
    rules = allow_rules(cfg, cfg.project(lane.data["project"]), stage_cfg["agent"])
    cmd = build_command(cfg, stage_cfg, prompt_arg, lane.worktree, model, write_settings(lane, stage, rules))
    timeout = stage_cfg.get("timeout_min", 30) * 60 + 60  # 比工具自己的时限多留一分钟
    add_usage(cfg, stage_cfg["agent"])

    with open(log_path, "wb") as log:
        log.write(f"# {now()} {lane.id} {stage}\n# {' '.join(cmd)}\n\n".encode("utf-8"))
        log.flush()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, cwd=lane.worktree, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags,
                                start_new_session=(os.name != "nt"))
        lane.data["pid"] = proc.pid
        start = time.time()
        while proc.poll() is None:
            lane.data["heartbeat"] = now()
            lane.save()
            if time.time() - start > timeout:
                kill_tree(proc.pid)
                proc.wait()
                return "timeout", log_path
            time.sleep(3)
    lane.data["pid"] = None
    return ("ok" if proc.returncode == 0 else "error"), log_path
