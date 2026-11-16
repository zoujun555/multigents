"""网页界面的自检：在临时目录里启动 lane ui，用假 Agent 走一遍登录、接口和操作。不消耗任何额度。

运行：python tests/run_ui_check.py
"""
import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="lane-ui-"))
home, repo = tmp / "home", tmp / "repo"
shutil.copytree(ROOT / "prompts", home / "prompts")
shutil.copytree(ROOT / "templates", home / "templates")
(home / "config" / "rules").mkdir(parents=True)
repo.mkdir()


def sh(*args):
    subprocess.run(args, cwd=repo, check=True, capture_output=True)


sh("git", "init", "-b", "main")
sh("git", "config", "user.name", "demo")
sh("git", "config", "user.email", "demo@example.invalid")
(repo / "app.txt").write_text("hello\n", encoding="utf-8")
sh("git", "add", "-A")
sh("git", "commit", "-m", "init")

# 配置：把两个工具都换成假 Agent，只保留一个演示项目
fake = [sys.executable, str(ROOT / "tests" / "fake_agent.py")]
lines, skip = [], False
for line in (ROOT / "config" / "lane.toml").read_text(encoding="utf-8").splitlines():
    if line.startswith("[projects."):
        skip = True
    if skip:
        continue
    if line.startswith("command = [\"claude\""):
        line = f"command = {json.dumps(fake + ['claude', '{prompt}'])}"
    elif line.startswith("command = [\"agy\""):
        line = f"command = {json.dumps(fake + ['agy', '{prompt}', '{settings}'])}"
    elif line.startswith("poll_seconds"):
        line = "poll_seconds = 1"
    lines.append(line)
lane_toml = "\n".join(lines) + f'\n[projects.demo]\nrepo = {json.dumps(str(repo))}\nbase = "main"\ntest_paths = ["tests/"]\n'
assert lane_toml.count("fake_agent") == 2
(home / "config" / "lane.toml").write_text(lane_toml, encoding="utf-8")

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
password = secrets.token_urlsafe(9)   # 只用于这次自检的临时目录
(home / "config" / "server.toml").write_text(
    f'[server]\nhost = "127.0.0.1"\nport = {port}\n[login]\nusername = "chris"\npassword = "{password}"\n', encoding="utf-8")

env = {**os.environ, "LANE_HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
server = subprocess.Popen([sys.executable, "-m", "lane", "ui", "--no-browser"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{port}"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(path, body=None, headers=None, raw=False):
    """返回 (状态码, 数据)。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    head = {"X-Requested-With": "lane", "Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=headers if headers is not None else head)
    try:
        with opener.open(req, timeout=20) as res:
            text = res.read().decode("utf-8")
            return res.status, (text if raw else json.loads(text))
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8")
        return err.code, (text if raw else (json.loads(text) if text.startswith("{") else {"error": text}))


def wait_for(check, what, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return
        time.sleep(0.5)
    raise AssertionError(f"等待超时：{what}")


try:
    wait_for(lambda: call("/api/session")[0] == 200, "服务启动", 20)

    # 静态文件和安全
    code, page = call("/", raw=True)
    assert code == 200 and "main.js" in page
    assert call("/main.js", raw=True)[0] == 200 and call("/i18n/en.json")[0] == 200
    assert call("/../config/server.toml", raw=True)[0] == 404, "不能读到静态目录以外的文件"
    assert call("/%2e%2e/%2e%2e/config/server.toml", raw=True)[0] == 404
    zh, en = call("/i18n/zh.json")[1], call("/i18n/en.json")[1]
    assert set(zh) == set(en) and len(zh) > 80, "中英文资源的键必须完全一致"

    # 未登录
    assert call("/api/session")[1] == {"logged_in": False}
    assert call("/api/board")[0] == 401
    assert call("/api/alerts")[0] == 401
    assert call("/api/lane/new", {"project": "demo", "title": "x"})[0] == 401
    assert call("/api/login", {"username": "chris", "password": "wrong"})[0] == 401
    assert call("/api/login", {"username": "chris", "password": password}, headers={"Content-Type": "application/json"})[0] == 403, \
        "不带 X-Requested-With 的 POST 应被拒绝"

    # 登录
    assert call("/api/login", {"username": "chris", "password": password})[0] == 200
    assert call("/api/session")[1] == {"logged_in": True}
    board = call("/api/board")[1]
    assert board["lanes"] == [] and board["projects"] == ["demo"] and board["scheduler"]["running"] is False

    # 新建需求 → 启动调度器 → 等到规格闸门
    assert call("/api/lane/new", {"project": "demo", "title": ""})[0] == 400, "标题为空应报错"
    assert call("/api/lane/new", {"project": "nope", "title": "x"})[0] == 400
    code, res = call("/api/lane/new", {"project": "demo", "title": "问候语", "text": "首页显示问候语"})
    assert code == 200 and "REQ-001" in res["message"], res
    assert call("/api/scheduler/start", {})[0] == 200
    assert call("/api/scheduler/start", {})[0] == 400, "重复启动应报错"
    wait_for(lambda: call("/api/lane?id=REQ-001")[1].get("stage") == "spec_gate", "REQ-001 到达规格闸门")
    assert call("/api/board")[1]["scheduler"]["running"] is True

    # 需求页、文件、越界读取
    detail = call("/api/lane?id=REQ-001")[1]
    assert detail["status"] == "waiting" and "spec.md" in detail["files"] and detail["log_tail"]
    assert "STATUS: READY" in call("/api/file?id=REQ-001&name=spec.md")[1]["text"]
    for bad in ("../../../../config/server.toml", "..%2Fspec.md", "history/../spec.md", "C:/x.md"):
        assert call(f"/api/file?id=REQ-001&name={bad}")[0] == 400, bad
    assert call("/api/lane?id=../x")[0] == 400

    # 退回必须写意见；批准后一路跑到合并闸门
    assert call("/api/lane/reject", {"id": "REQ-001", "message": "  "})[0] == 400
    assert call("/api/lane/approve", {"id": "REQ-001"})[0] == 200
    assert call("/api/lane/approve", {"id": "REQ-001"})[0] == 400, "已不在闸门，再次批准应报错"
    wait_for(lambda: call("/api/lane?id=REQ-001")[1].get("stage") == "merge_gate", "REQ-001 到达合并闸门", 120)
    assert call("/api/lane/clean", {"id": "REQ-001"})[0] == 400, "未完成的需求不加 force 不能清理"
    assert call("/api/lane/approve", {"id": "REQ-001"})[0] == 200

    # 告警历史：没有告警时为空，项目筛选不报错
    assert call("/api/alerts")[1] == {"alerts": []} and call("/api/alerts?project=demo")[1] == {"alerts": []}
    rows = [json.dumps({"at": "2026-01-01T10:00:00", "project": "demo", "lane": "REQ-009", "text": "甲"}, ensure_ascii=False),
            json.dumps({"at": "2026-01-01T11:00:00", "project": "", "lane": "", "text": "乙"}, ensure_ascii=False),
            "{写到一半"]
    (home / "state" / "alerts.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert [a["text"] for a in call("/api/alerts")[1]["alerts"]] == ["乙", "甲"], "最新的在前，坏行跳过"
    assert [a["text"] for a in call("/api/alerts?project=demo")[1]["alerts"]] == ["甲"]
    assert call("/api/alerts?project=other")[1]["alerts"] == []

    # 统计
    st = call("/api/stats")[1]
    assert st["total"] == 1 and st["by_status"] == {"done": 1} and st["reviewed"] == 1 and st["review_first_pass"] == 0
    assert {s["stage"]: s["runs"] for s in st["stages"]} == \
        {"plan": 1, "code": 1, "review": 2, "fix": 1, "test": 1, "itest": 1, "report": 1}
    assert st["usage_days"] and st["usage_days"][-1]["claude"] == 4 and st["usage_days"][-1]["agy"] == 4
    assert call("/api/stats?project=other")[1]["total"] == 0

    # 清理后仍能在需求页看到（已归档）
    assert call("/api/lane/clean", {"id": "REQ-001"})[0] == 200
    assert call("/api/lane?id=REQ-001")[1]["archived"] is True and call("/api/board")[1]["lanes"] == []

    # 配置：读取、校验、保存、不能碰 server.toml
    files = call("/api/config")[1]["files"]
    assert files == ["lane.toml", "templates/AGENTS.md"], files
    text = call("/api/config/file?key=lane.toml")[1]["text"]
    assert call("/api/config/file", {"key": "lane.toml", "text": "[workbench\nbroken"})[0] == 400
    assert call("/api/config/file", {"key": "lane.toml", "text": text.replace("[stages.itest]", "[stages.gone]")})[0] == 400
    assert call("/api/config/file", {"key": "lane.toml", "text": text.replace("max_parallel = 2", "max_parallel = 3", 1)})[0] == 200
    assert call("/api/board")[1]["max_parallel"] == 3 and (home / "config" / "lane.toml.bak").exists()
    assert call("/api/config/file", {"key": "rules/demo.md", "text": "- 规则\n"})[0] == 200
    assert (home / "config" / "rules" / "demo.md").read_text(encoding="utf-8") == "- 规则\n"
    for bad in ("server.toml", "../lane/core.py", "rules/../server.toml", "rules/a.txt"):
        assert call(f"/api/config/file?key={bad}")[0] == 400, bad
        assert call("/api/config/file", {"key": bad, "text": "x"})[0] == 400, bad
    assert password not in json.dumps(call("/api/config")[1]) + text

    # 停止调度器、退出登录
    assert call("/api/scheduler/stop", {})[0] == 200
    wait_for(lambda: call("/api/board")[1]["scheduler"]["running"] is False, "调度器停止", 15)
    assert call("/api/logout", {})[0] == 200 and call("/api/board")[0] == 401

    # 连续输错密码后锁定
    for _ in range(5):
        assert call("/api/login", {"username": "chris", "password": "x"})[0] == 401
    assert call("/api/login", {"username": "chris", "password": password})[0] == 429, "5 次错误后应暂时锁定"
    print("全部通过")
finally:
    server.kill()
    lock = home / "state" / "scheduler.lock"
    if lock.exists():
        subprocess.run(["taskkill", "/F", "/T", "/PID", lock.read_text()], capture_output=True)
    time.sleep(0.5)
    shutil.rmtree(tmp, ignore_errors=True)
