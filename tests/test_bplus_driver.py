"""B+ 核心闭环测试：tokens 解析、0-Token preflight + lint_cmds 插槽、driver 原子命令、Web 只读监控。

运行：python tests/test_bplus_driver.py
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
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("LANE_CONFIG", None)

from lane import driver, preflight, tokens  # noqa: E402
from lane.core import Config, load_lane  # noqa: E402
from lane.ui import stats  # noqa: E402


FAKE_JSON_AGENT = '''import json, re, sys
from pathlib import Path
prompt_arg = sys.argv[1]
stage = re.search(r"prompt-([a-z_]+)\\.md", prompt_arg).group(1)
prompt = Path(f".lane/prompt-{stage}.md").read_text(encoding="utf-8")
req_id = re.search(r"REQ-\\d+", prompt).group(0)
req = Path("docs/reqs") / req_id
if stage == "plan_review":
    (req / "spec-review.md").write_text("VERDICT: REVISE\\n\\n## 必须修订\\n1. 补充边界说明\\n", encoding="utf-8")
    out = {"type": "result", "usage": {"input_tokens": 1200, "cache_creation_input_tokens": 400,
           "cache_read_input_tokens": 3000, "output_tokens": 250}, "total_cost_usd": 0.0125, "num_turns": 3}
elif stage == "review":
    pack = Path(".lane/review-pack.md").read_text(encoding="utf-8")
    if "（INCREMENTAL）" in pack:
        (req / "review.md").write_text("VERDICT: PASS\\n\\n## 必须修复\\n无\\n", encoding="utf-8")
    else:
        Path("stray_edit.txt").write_text("should be reverted", encoding="utf-8")
        (req / "review.md").write_text("VERDICT: FAIL\\n\\n## 必须修复\\n1. app.py:1 修复示例\\n", encoding="utf-8")
    out = {"type": "result", "usage": {"input_tokens": 800, "cache_creation_input_tokens": 200,
           "cache_read_input_tokens": 1500, "output_tokens": 180}, "total_cost_usd": 0.0065, "num_turns": 4}
print(json.dumps(out))
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class BPlusCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lane-bplus-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        shutil.copytree(ROOT / "prompts", self.home / "prompts")
        shutil.copytree(ROOT / "templates", self.home / "templates")
        (self.home / "config" / "rules").mkdir(parents=True)
        self.fake_script = self.tmp / "fake_json_agent.py"
        self.fake_script.write_text(FAKE_JSON_AGENT, encoding="utf-8")

        lane_text = (ROOT / "config" / "lane.toml").read_text(encoding="utf-8")
        fake_cmd = json.dumps([sys.executable, str(self.fake_script), "{prompt}"])
        lines = []
        for line in lane_text.splitlines():
            if line.startswith('command = ["claude"'):
                line = f"command = {fake_cmd}"
            elif line.startswith('command = ["agy"'):
                line = f"command = {fake_cmd}"
            lines.append(line)
        (self.home / "config" / "lane.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "tester")
        _git(self.repo, "config", "user.email", "tester@example.invalid")
        (self.repo / "app.py").write_text("# header comment\n# line 2\nprint('ok')\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        self._write_local()

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _py(self) -> str:
        return f'& "{sys.executable}"' if os.name == "nt" else f'"{sys.executable}"'

    def _write_local(self, extra_verify: str = ""):
        py = self._py()
        local = (
            f'[workbench]\nworktree_root = {json.dumps(str(self.tmp / "ws"))}\n'
            f'[projects.demo]\nrepo = {json.dumps(str(self.repo))}\nbase = "main"\ntest_paths = ["tests/"]\n'
            f'[projects.demo.verify]\n'
            f'build_cmd = {json.dumps(py + " -c \"print(\'build ok\')\"")}\n'
            f'unit_cmd = {json.dumps(py + " -c \"print(\'unit ok\')\"")}\n'
            f'{extra_verify}\n'
        )
        (self.home / "config" / "local.toml").write_text(local, encoding="utf-8")

    def cfg(self) -> Config:
        return Config(self.home)

    def test_tokens_parse_and_summarize(self):
        c = self.cfg()
        log_claude = self.tmp / "claude.log"
        log_claude.write_text('# header\n{"usage":{"input_tokens":100,"cache_creation_input_tokens":50,"cache_read_input_tokens":500,"output_tokens":40},"total_cost_usd":0.02,"num_turns":2}\n', encoding="utf-8")
        log_agy = self.tmp / "agy.log"
        log_agy.write_text('# header\n{"usage":{"input_tokens":200,"output_tokens":80,"thinking_tokens":30,"cache_read_tokens":900},"num_turns":1}\n', encoding="utf-8")
        e1 = tokens.record_from_log(c, "REQ-001", "demo", "review", "claude", "sonnet", log_claude)
        e2 = tokens.record_from_log(c, "REQ-001", "demo", "code", "agy", "gemini", log_agy)
        self.assertEqual(e1["input_tokens"], 100)
        self.assertEqual(e1["cache_creation_tokens"], 50)
        self.assertEqual(e1["cost_usd"], 0.02)
        self.assertEqual(e2["thinking_tokens"], 30)
        summary = tokens.summarize(c, project="demo")
        self.assertEqual([r["agent"] for r in summary], ["agy", "claude"])
        self.assertEqual(summary[1]["output_tokens"], 40)
        self.assertEqual(summary[0]["cache_read_tokens"], 900)

    def test_preflight_checks_lint_bom_lines_and_protected(self):
        lint_probe = self.tmp / "lint_ok.py"
        lint_probe.write_text("import sys; assert len(sys.argv) >= 2\n", encoding="utf-8")
        lint_fail = self.tmp / "lint_fail.py"
        lint_fail.write_text("import sys; print('LINT_BAD_STYLE'); sys.exit(1)\n", encoding="utf-8")
        py = self._py()
        self._write_local(
            f'lint_cmds = [{json.dumps(py + f" \"{lint_probe}\" {{changed_files}}")}, '
            f'{json.dumps(py + f" \"{lint_fail}\" {{changed_files}}")}]\n'
            'require_utf8_bom = true\nmax_new_file_lines = 5\n'
        )
        c = self.cfg()
        info = driver.init_lane(c, "demo", "check preflight", "desc", tier="standard")
        lane = load_lane(c, info["id"])
        wt = lane.worktree
        # 触发全部机械错误：超行非测试新文件、受保护 AGENTS.md、缺 BOM、双 BOM、Lint 失败
        (wt / "big.py").write_text("\n".join(f"x = {i}" for i in range(8)) + "\n", encoding="utf-8")
        (wt / "tests").mkdir()
        (wt / "tests" / "big_test.py").write_text("\n".join(f"t = {i}" for i in range(10)) + "\n", encoding="utf-8")
        (wt / "AGENTS.md").write_text("# modified\n", encoding="utf-8")
        (wt / "no_bom.js").write_bytes(b"console.log(1);\n")
        (wt / "dbl_bom.js").write_bytes(b"\xef\xbb\xbf\xef\xbb\xbfconsole.log(2);\n")
        (wt / "ok_bom.js").write_bytes(b"\xef\xbb\xbfconsole.log(3);\n")

        rc, res = driver.run_preflight_cmd(c, lane.id)
        self.assertEqual(rc, 1)
        self.assertEqual(res["status"], "FAIL")
        lint_errs = [e for e in res["errors"] if e.startswith("Lint 失败")]
        other_errs = [e for e in res["errors"] if not e.startswith("Lint 失败")]
        self.assertEqual(len(lint_errs), 1)
        self.assertIn("LINT_BAD_STYLE", lint_errs[0])
        self.assertEqual(set(other_errs), {
            "新增文件超出行数上限（8 > 5）：big.py",
            "修改了未在 spec 中提及的受保护文件：AGENTS.md",
            "缺少 UTF-8 BOM：no_bom.js",
            "UTF-8 双 BOM：dbl_bom.js",
        })
        self.assertTrue((wt / ".lane" / "verify-result.txt").exists())

    def test_driver_bplus_flow_and_incremental_review_and_locks(self):
        c = self.cfg()
        # 1. light 档跳过规划评审
        light_info = driver.init_lane(c, "demo", "small fix", "tiny", tier="light")
        rc, res = driver.run_review(c, light_info["id"], kind="plan")
        self.assertEqual(rc, 0)
        self.assertEqual(res["verdict"], "SKIP")

        # 2. standard 档走规划轻审 + preflight + 第 1 轮全量 & 第 2 轮增量评审（支持单次覆写模型/推理深度）
        info = driver.init_lane(c, "demo", "feature x", "full flow", tier="standard")
        lid = info["id"]
        lane = load_lane(c, lid)
        (lane.req_dir / "spec.md").write_text("STATUS: READY\n\n# 规格\n\nAC1: ok\n", encoding="utf-8")
        driver.set_stage(c, lid, "plan", "running", note="drafted", commit=True)

        rc_p, res_p = driver.run_review(c, lid, kind="plan", agent="agy", model="gemini-4-argon-high", effort="")
        self.assertEqual(rc_p, 0)
        self.assertEqual(res_p["verdict"], "REVISE")
        self.assertEqual(res_p["agent"], "agy")
        self.assertEqual(res_p["model"], "gemini-4-argon-high")
        self.assertEqual(res_p["usage"]["output_tokens"], 250)
        self.assertTrue((lane.req_dir / "spec-review.md").exists())

        # 编码 -> preflight PASS -> 第 1 轮 review FAIL（并撤销越界 stray_edit.txt）
        (lane.worktree / "app.py").write_text("# header comment\nprint('v1')\n", encoding="utf-8")
        rc_pf, pf1 = driver.run_preflight_cmd(c, lid)
        self.assertEqual(rc_pf, 0)
        self.assertIn("（FULL）", Path(pf1["pack_path"]).read_text(encoding="utf-8"))

        rc_r1, r1 = driver.run_review(c, lid, kind="code")
        self.assertEqual(rc_r1, 0)
        self.assertEqual(r1["verdict"], "FAIL")
        self.assertEqual(r1["agent"], "claude")
        self.assertEqual(r1["model"], "sonnet")
        self.assertEqual(r1["effort"], "high")
        self.assertFalse((lane.worktree / "stray_edit.txt").exists())
        self.assertTrue(load_lane(c, lid).data.get("last_review_sha"))
        self.assertEqual(load_lane(c, lid).data["round"], 1)

        # 修复 -> preflight 生成增量包 -> 第 2 轮 review PASS
        (lane.worktree / "app.py").write_text("# header comment\nprint('v2')\n", encoding="utf-8")
        rc_pf2, pf2 = driver.run_preflight_cmd(c, lid)
        self.assertEqual(rc_pf2, 0)
        pack2 = Path(pf2["pack_path"]).read_text(encoding="utf-8")
        self.assertIn("（INCREMENTAL）", pack2)
        self.assertIn("app.py:1 修复示例", pack2)
        self.assertIn("print('v2')", pack2)

        rc_r2, r2 = driver.run_review(c, lid, kind="code")
        self.assertEqual(rc_r2, 0)
        self.assertEqual(r2["verdict"], "PASS")

        # 统计页已聚合精确 Token（1 次 agy 规划评审 + 2 次 claude 代码评审）
        st = stats.compute(c, project="demo")
        by_agent = {r["agent"]: r for r in st["tokens"]}
        self.assertEqual(set(by_agent), {"agy", "claude"})
        self.assertEqual(by_agent["agy"]["runs"], 1)
        self.assertEqual(by_agent["agy"]["input_tokens"], 1200)
        self.assertEqual(by_agent["claude"]["runs"], 2)
        self.assertEqual(by_agent["claude"]["input_tokens"], 1600)

        # 共享资源锁
        driver.acquire_lock(c, "itest", lid)
        driver.acquire_lock(c, "itest", lid)  # 同一需求幂等
        with self.assertRaises(SystemExit) as ctx:
            driver.acquire_lock(c, "itest", light_info["id"])
        self.assertIn(lid, str(ctx.exception))
        driver.release_lock(c, "itest", lid)
        driver.acquire_lock(c, "itest", light_info["id"])
        driver.release_lock(c, "itest", light_info["id"])

        # finish + clean
        driver.finish_lane(c, lid, clean=True)
        self.assertFalse(lane.worktree.exists())
        self.assertTrue((c.state_dir / "archive" / f"{lid}.json").exists())

    def test_profile_switching_and_stage_override_priority(self):
        c = self.cfg()
        self.assertEqual(c.get("workbench", "profile"), "claudeplus")
        self.assertEqual((c.stage("plan_review")["agent"], c.stage("plan_review")["model"], c.stage("plan_review")["effort"]),
                         ("claude", "opus", "high"))
        self.assertEqual((c.stage("review")["agent"], c.stage("review")["model"], c.stage("review")["effort"]),
                         ("claude", "sonnet", "high"))

        # 手动切换到 agy-only
        out = driver.switch_profile(c, "agy-only")
        self.assertIn("agy-only", out)
        self.assertIn("gemini-4-argon-high", out)
        self.assertIn("gemini-4-neon-high", out)
        c2 = self.cfg()
        self.assertEqual(c2.get("workbench", "profile"), "agy-only")
        self.assertEqual((c2.stage("plan_review")["agent"], c2.stage("plan_review")["model"]), ("agy", "gemini-4-argon-high"))
        self.assertEqual((c2.stage("review")["agent"], c2.stage("review")["model"]), ("agy", "gemini-4-neon-high"))

        # local.toml 显式阶段覆盖优先级高于 profile
        local = self.home / "config" / "local.toml"
        local.write_text(local.read_text(encoding="utf-8") + '\n[stages.review]\nmodel = "gemini-4-argon-high"\n', encoding="utf-8")
        c3 = self.cfg()
        self.assertEqual((c3.stage("review")["agent"], c3.stage("review")["model"]), ("agy", "gemini-4-argon-high"))
        self.assertEqual(c3.stage("plan_review")["model"], "gemini-4-argon-high")

        # 未知方案直接报错
        with self.assertRaises(SystemExit) as ctx:
            driver.switch_profile(c3, "unknown-profile")
        self.assertIn("没有方案 'unknown-profile'", str(ctx.exception))

        # claude-review 子命令已移除，调用必须报错
        r = subprocess.run([sys.executable, "-m", "lane", "driver", "claude-review", "REQ-001"],
                           env={**os.environ, "LANE_HOME": str(self.home), "PYTHONPATH": str(ROOT)},
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid choice: 'claude-review'", r.stderr)

    def test_worktree_zero_path_bootstrap(self):
        c = self.cfg()
        info = driver.init_lane(c, "demo", "bootstrap check", "desc", tier="light")
        lid = info["id"]
        wt = Path(info["worktree"])
        ctx_path = wt / ".lane" / "context.json"
        cmd_path = wt / ".lane" / "lane.cmd"
        sh_path = wt / ".lane" / "lane.sh"
        self.assertTrue(ctx_path.exists())
        self.assertTrue(cmd_path.exists())
        self.assertTrue(sh_path.exists())
        self.assertEqual(json.loads(ctx_path.read_text(encoding="utf-8")), {
            "id": lid, "project": "demo", "tier": "light", "branch": f"lane/{lid}",
        })
        self.assertIn(b"\r\n", cmd_path.read_bytes())
        self.assertNotIn(b"\r\n", sh_path.read_bytes())
        # .lane/ 已在 .git/info/exclude 中，工作树 git 状态必须干净（且不会产生 .agents/skills）
        status = subprocess.run(["git", "status", "--porcelain"], cwd=wt, check=True,
                                capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(status, "")
        self.assertFalse((wt / ".agents" / "skills" / "multiagents-lane").exists())

        # 在完全清空 LANE_HOME / PYTHONPATH 的环境下，直接从 worktree 调用本地引导器
        clean_env = {k: v for k, v in os.environ.items() if k not in ("LANE_HOME", "PYTHONPATH", "LANE_CONFIG")}
        clean_env["PYTHONIOENCODING"] = "utf-8"
        launcher = str(cmd_path if os.name == "nt" else sh_path)
        run_cmd = ["cmd", "/c", launcher, "driver", "stage", lid, "code", "running", "--note", "from-bootstrap"] if os.name == "nt" else [launcher, "driver", "stage", lid, "code", "running", "--note", "from-bootstrap"]
        r = subprocess.run(run_cmd, cwd=wt, env=clean_env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, msg=r.stderr or r.stdout)
        out = json.loads(r.stdout)
        self.assertEqual(out, {"id": lid, "stage": "code", "status": "running", "note": "from-bootstrap"})
        updated = load_lane(c, lid)
        self.assertEqual((updated.data["stage"], updated.data["status"], updated.data["note"]),
                         ("code", "running", "from-bootstrap"))

    def test_web_readonly_monitor_mode(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        pw = secrets.token_urlsafe(8)
        (self.home / "config" / "server.toml").write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\nreadonly = true\n[login]\nusername = "admin"\npassword = "{pw}"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "LANE_HOME": str(self.home), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, "-m", "lane", "ui", "--no-browser"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(path, body=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            hdrs = {"X-Requested-With": "lane", "Content-Type": "application/json"} if body is not None else {}
            req = urllib.request.Request(base + path, data=data, headers=hdrs)
            try:
                with opener.open(req, timeout=10) as res:
                    return res.status, json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read().decode("utf-8"))

        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    if call("/api/session")[0] == 200:
                        break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.2)
            self.assertEqual(call("/api/login", {"username": "admin", "password": pw})[0], 200)
            code, board = call("/api/board")
            self.assertEqual(code, 200)
            self.assertIs(board["readonly"], True)
            code, cfg_res = call("/api/config")
            self.assertEqual(code, 200)
            self.assertIs(cfg_res["readonly"], True)
            for path, payload in (
                ("/api/lane/new", {"project": "demo", "title": "x"}),
                ("/api/scheduler/start", {}),
                ("/api/config/file", {"key": "lane.toml", "text": "x"}),
            ):
                st_code, body = call(path, payload)
                self.assertEqual(st_code, 403)
                self.assertEqual(body["error"], "当前为只读监控模式，禁止修改操作")
            self.assertEqual(call("/api/logout", {})[0], 200)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()

    def test_web_create_agy_lane_and_legacy_modes(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        pw = secrets.token_urlsafe(8)
        (self.home / "config" / "server.toml").write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\nreadonly = false\n[login]\nusername = "admin"\npassword = "{pw}"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "LANE_HOME": str(self.home), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, "-m", "lane", "ui", "--no-browser"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(path, body=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            hdrs = {"X-Requested-With": "lane", "Content-Type": "application/json"} if body is not None else {}
            req = urllib.request.Request(base + path, data=data, headers=hdrs)
            try:
                with opener.open(req, timeout=10) as res:
                    return res.status, json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read().decode("utf-8"))

        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    if call("/api/session")[0] == 200:
                        break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.2)
            self.assertEqual(call("/api/login", {"username": "admin", "password": pw})[0], 200)

            # 1. AGY standard
            code, res1 = call("/api/lane/new", {"project": "demo", "title": "agy std", "text": "body1", "mode": "agy-standard"})
            self.assertEqual(code, 200)
            self.assertEqual((res1["id"], res1["tier"], res1["branch"]), ("REQ-001", "standard", "lane/REQ-001"))
            wt1 = Path(res1["worktree"])
            self.assertTrue((wt1 / ".lane" / "context.json").exists())
            self.assertTrue((wt1 / ".lane" / "lane.cmd").exists())
            self.assertTrue((wt1 / ".lane" / "lane.sh").exists())
            lane1 = load_lane(self.cfg(), "REQ-001")
            self.assertEqual((lane1.data["driver"], lane1.data["tier"], lane1.data["status"]), ("agy", "standard", "running"))

            # 2. AGY light
            code, res2 = call("/api/lane/new", {"project": "demo", "title": "agy light", "text": "body2", "mode": "agy-light"})
            self.assertEqual(code, 200)
            self.assertEqual((res2["id"], res2["tier"]), ("REQ-002", "light"))
            self.assertEqual(json.loads((Path(res2["worktree"]) / ".lane" / "context.json").read_text(encoding="utf-8"))["tier"], "light")

            # 3. 缺省 mode 保持旧调度器队列兼容
            code, res3 = call("/api/lane/new", {"project": "demo", "title": "legacy item", "text": "body3"})
            self.assertEqual(code, 200)
            self.assertNotIn("worktree", res3)
            self.assertEqual(load_lane(self.cfg(), "REQ-003").data["status"], "queued")

            # 4. 非法 mode 严格报 HTTP 400
            code, bad = call("/api/lane/new", {"project": "demo", "title": "bad", "mode": "unknown"})
            self.assertEqual(code, 400)
            self.assertEqual(bad["error"], "未知创建模式 'unknown'，可选：scheduler / agy-standard / agy-light")

            self.assertEqual(call("/api/logout", {})[0], 200)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
