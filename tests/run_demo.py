"""端到端自检：在临时目录里用假 Agent 跑完整条流水线。不消耗任何额度。

运行：python tests/run_demo.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="lane-demo-"))
home, repo = tmp / "home", tmp / "repo"
shutil.copytree(ROOT / "prompts", home / "prompts")
(home / "config").mkdir()
repo.mkdir()

def sh(*args, cwd=repo):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)

sh("git", "init", "-b", "main")
sh("git", "config", "user.name", "demo")
sh("git", "config", "user.email", "demo@example.invalid")
(repo / "app.txt").write_text("hello\n", encoding="utf-8")
(repo / ".gitignore").write_text("local/\n", encoding="utf-8")
sh("git", "add", "-A")
sh("git", "commit", "-m", "init")
(repo / "local").mkdir()
(repo / "local" / ".env").write_text("X=1\n", encoding="utf-8")  # 被忽略的本地文件，应复制进工作树

fake = [sys.executable, str(ROOT / "tests" / "fake_agent.py")]
config = (ROOT / "config" / "lane.toml").read_text(encoding="utf-8")
config = config.replace('command = ["claude", "-p", "{prompt}", "--model", "{model}", "--permission-mode", "{mode}", "--effort", "{effort}", "--output-format", "text"]',
                        f"command = {json.dumps(fake + ['claude', '{prompt}'])}")
config = config.replace('command = ["agy", "-p", "{prompt}", "--model", "{model}", "--mode", "{mode}", "--print-timeout", "{timeout}s", "--output-format", "json", "--settings", "{settings}"]',
                        f"command = {json.dumps(fake + ['agy', '{prompt}', '{settings}'])}")
assert "fake_agent" in config
config = config.replace("limit_pause_minutes = 30", "limit_pause_minutes = 0.05")  # 额度暂停 3 秒
assert "0.05" in config
config = config.replace("timeout_min = 90", "timeout_min = -0.9")  # 编码阶段约 6 秒后超时
(home / "config" / "rules").mkdir()
(home / "config" / "rules" / "demo.md").write_text("- 演示规则：断言必须严格匹配\n", encoding="utf-8")
workspace = tmp / "ws"
local = (f'[workbench]\nworktree_root = {json.dumps(str(workspace))}\n[projects.demo]\nrepo ={json.dumps(str(repo))}\nbase = "main"\ntest_paths = ["tests/"]\n'
           'rules = "config/rules/demo.md"\ncopy_files = ["local/", "missing.txt"]\nallow_commands = ["make .*"]\n'
           '[projects.demo.verify]\nbuild_cmd = "make build"\nserve_cmd = "serve --dir {worktree} --port {port}"\n'
           'ta_cmd = "make e2e"\nport_base = 64000\n[projects.demo.verify.env]\nBASE_URL = "http://localhost:{port}"\n')
assert "[projects." not in config, "项目登记应在 local.toml 里，不在 lane.toml 里"
(home / "config" / "lane.toml").write_text(config, encoding="utf-8")
(home / "config" / "local.toml").write_text(local, encoding="utf-8")

env = {**os.environ, "LANE_HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}

def lane(*args, ok=True):
    r = subprocess.run([sys.executable, "-m", "lane", *args], env=env, capture_output=True, text=True, encoding="utf-8")
    print(f"$ lane {' '.join(args)}\n{r.stdout}{r.stderr}")
    assert (r.returncode == 0) == ok, f"lane {args} 退出码 {r.returncode}"
    return r.stdout

def state(i):
    return json.loads((home / "state" / "lanes" / f"REQ-00{i}.json").read_text(encoding="utf-8"))

lane("doctor")
lane("new", "demo", "问候语", "-m", "首页显示问候语")
lane("new", "demo", "第二个需求")
lane("new", "demo", "会超时的需求")
lane("new", "demo", "自测失败的需求")
lane("new", "demo", "集成失败的需求")
lane("new", "demo", "权限被拒的需求")
lane("new", "demo", "额度用完的需求")
lane("run", "--once")
assert all(state(i)["stage"] == "spec_gate" for i in (1, 2, 3, 4, 5, 6, 7)), "七个需求都应停在规格闸门"

assert "阻塞问题" in state(2)["note"] and "阻塞问题" not in state(1)["note"], "规划标了 BLOCKED 时，闸门说明要提示"
lane("reject", "REQ-002", "-m", "范围太大")
lane("approve", "REQ-001")
lane("approve", "REQ-003")
lane("approve", "REQ-004")
lane("approve", "REQ-005")
lane("approve", "REQ-006")
lane("approve", "REQ-007")
lane("approve", "REQ-002", ok=False)  # 已退回，不在等待
out = lane("run", "--once")

s1, s2, s3 = state(1), state(2), state(3)
wt1 = Path(s1["worktree"])
assert workspace in wt1.parents and (workspace / "demo" / "REQ-001").exists(), f"工作树应在配置的工作空间里：{wt1}"
assert not (home / "worktrees").exists(), "默认位置不应再被使用"
assert s1["stage"] == "merge_gate" and s1["status"] == "waiting", s1
assert s1["round"] == 1, "应经过一轮修复"
assert (wt1 / "local" / ".env").read_text(encoding="utf-8") == "X=1\n", "copy_files 应把被忽略的本地文件复制进工作树"
assert "演示规则" in (wt1 / ".lane" / "prompt-code.md").read_text(encoding="utf-8"), "编码提示词应包含项目规则"
assert "演示规则" in (wt1 / ".lane" / "prompt-review.md").read_text(encoding="utf-8"), "评审提示词应包含项目规则"
assert "演示规则" not in (wt1 / ".lane" / "prompt-plan.md").read_text(encoding="utf-8")
code_prompt = (wt1 / ".lane" / "prompt-code.md").read_text(encoding="utf-8")
design_prompt = (wt1 / ".lane" / "prompt-test.md").read_text(encoding="utf-8")
test_prompt = (wt1 / ".lane" / "prompt-itest.md").read_text(encoding="utf-8")
assert "make build" in design_prompt and "make e2e" not in design_prompt and "serve --dir" not in design_prompt,     "测试设计提示词只含构建，不含启动应用和集成测试"
assert "make build" in code_prompt and "make e2e" not in code_prompt, "编码提示词只含构建和单元测试"
assert f"serve --dir {wt1} --port 64001" in test_prompt, "集成测试提示词应含本需求的端口和工作树"
assert "BASE_URL=http://localhost:64001" in test_prompt and "make e2e" in test_prompt
s4 = state(4)
assert s4["stage"] == "merge_gate" and s4["round"] == 2, f"自测失败算一轮，评审不通过再算一轮：{s4['stage']} {s4['round']}"
s5 = state(5)
kinds = [(h["stage"], h.get("next")) for h in s5["history"] if h.get("stage") in ("itest", "fix")]
assert ("itest", "fix") in kinds and s5["stage"] == "merge_gate" and s5["round"] == 2,     f"集成测试报产品缺陷应退回修复，修好后再次排队通过：{s5['stage']} {s5['round']} {kinds}"
# 让两条需求同时排到集成测试：必须一个跑完另一个才开始
lane("resume", "REQ-001", "--stage", "itest")
lane("resume", "REQ-004", "--stage", "itest")
lane("run", "--once")
spans = [[h for h in state(i)["history"] if h.get("stage") == "itest" and "started" in h][-1] for i in (1, 4)]
spans = sorted((h["started"], h["at"]) for h in spans)
assert spans[0][1] <= spans[1][0], f"集成测试应排队执行，不能重叠：{spans}"
s1 = state(1)
assert s1["stage"] == "merge_gate", s1
assert state(7)["stage"] == "merge_gate", "额度恢复后应自动重试并走完"
assert (wt1 / "app.txt").read_text(encoding="utf-8") == "hello from fix\n", "评审者的越界改动应被撤销"
assert not (wt1 / "stray.txt").exists(), "评审者新建的越界文件应被删除"
assert (wt1 / "docs/reqs/REQ-001/history/01-review.md").exists(), "第一轮评审应归档"
assert any(h.get("reverted") for h in s1["history"]), "历史里应记录被撤销的文件"
assert s2["stage"] == "spec_gate", "被退回的需求应重新规划后再次等待批准"
assert s3["status"] == "blocked" and "超时" in s3["note"], s3
s7 = state(7)
limits = [h for h in s7["history"] if h.get("result") == "limit"]
assert len(limits) == 1 and limits[0]["status"] == "queued" and "额度" in limits[0]["note"], "额度用完应排队重试，不算卡住"
assert s7["status"] != "blocked", s7
s6 = state(6)
assert s6["status"] == "blocked" and "allow_commands" in s6["note"], f"命令被拒应明确提示去补允许规则：{s6['note']}"
settings = json.loads((wt1 / ".lane" / "settings-code.json").read_text(encoding="utf-8"))
assert settings == {"permissions": {"allow": ["command(regex:make .*)"]}}, settings
assert "`make .*`" in code_prompt and "整个会话会立刻结束" in code_prompt, "agy 阶段的提示词应列出允许的命令"
assert "整个会话会立刻结束" not in (wt1 / ".lane" / "prompt-review.md").read_text(encoding="utf-8"), "Claude 阶段不带这段"
assert not (wt1 / ".lane" / "settings-review.json").exists()
assert "[Jenny]" in out and "REQ-003" in out, "Jenny 应上报卡住的需求"
assert not (home / "STATUS.md").exists() and not (home / "ALERTS.md").exists(), "不再生成这两个文件"
status = lane("status")
assert "需要你处理" in status and "REQ-003" in status, "lane status 仍应在终端打印状态"
history = [json.loads(l) for l in (home / "state" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()]
mine = [e for e in history if e["lane"] == "REQ-003"]
assert mine and mine[0]["project"] == "demo" and "卡住" in mine[0]["text"] and mine[0]["at"], history
log = subprocess.run(["git", "log", "--format=%s", "lane/REQ-001"], cwd=repo, capture_output=True, text=True).stdout
assert "REQ-001: test" in log and "REQ-001: itest" in log and "REQ-001: fix (round 1)" in log, log
assert subprocess.run(["git", "status", "--porcelain"], cwd=wt1, capture_output=True, text=True).stdout == "", "工作树应干净"

lane("approve", "REQ-001")
lane("clean", "REQ-001")
lane("status")
assert state(2)["stage"] == "spec_gate"
assert not wt1.exists()
shutil.rmtree(tmp, ignore_errors=True)
print("全部通过")
