"""Fast unit tests: call the functions directly in one process. No scheduler, no fake agents, no waiting.

Run: python tests/unit_tests.py        (takes a few seconds)

They check decisions and rules (what happens after a stage, which alerts fire, who may log in, what an
operation allows) and avoid asserting on message wording, so translating the messages does not break them.
The slower end-to-end run is tests/run_demo.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("LANE_CONFIG", None)

from lane import jenny, notify, ops  # noqa: E402
from lane.agents import build_command, render_verify  # noqa: E402
from lane.core import Config, Lane, add_usage, merge  # noqa: E402
from lane.runner import decide, mark_running, permission_denied, read_mark, usage_limited  # noqa: E402
from lane.ui import api  # noqa: E402
from lane.ui.auth import Sessions, ensure_server_config, read_server_config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="lane-unit-"))
HOME, REPO = TMP / "home", TMP / "repo"


def setUpModule():
    shutil.copytree(ROOT / "prompts", HOME / "prompts")
    shutil.copytree(ROOT / "templates", HOME / "templates")
    (HOME / "config" / "rules").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "lane.toml", HOME / "config" / "lane.toml")
    REPO.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@example.invalid"]):
        subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True)
    (REPO / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=REPO, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=REPO, check=True, capture_output=True)
    local = (f'[workbench]\nworktree_root = {json.dumps(str(TMP / "ws"))}\n'
             f'[projects.demo]\nrepo = {json.dumps(str(REPO))}\nbase = "main"\ntest_paths = ["tests/"]\n'
             '[projects.demo.verify]\nsetup_cmd = "make setup"\nbuild_cmd = "make build"\nunit_cmd = "make unit"\n'
             'serve_cmd = "serve {worktree} {port}"\nready_url = "http://localhost:{port}/"\nta_cmd = "make e2e"\n'
             'port_base = 64000\n[projects.demo.verify.env]\nBASE_URL = "http://localhost:{port}"\n')
    (HOME / "config" / "local.toml").write_text(local, encoding="utf-8")


def tearDownModule():
    subprocess.run(["git", "worktree", "prune"], cwd=REPO, capture_output=True)
    shutil.rmtree(TMP, ignore_errors=True)


def cfg() -> Config:
    return Config(HOME)


def fake_lane(c: Config, stage="code", status="queued", number=1, **extra) -> Lane:
    """A lane in memory with a worktree folder, no git involved."""
    lane_id = f"REQ-{number:03d}"
    wt = TMP / "fake" / lane_id
    data = {"id": lane_id, "project": "demo", "title": "t", "branch": "x", "worktree": str(wt), "stage": stage,
            "status": status, "round": 0, "note": "", "created": datetime.now().isoformat(timespec="seconds"),
            "since": datetime.now().isoformat(timespec="seconds"), "history": []}
    data.update(extra)
    lane = Lane(c, data)
    lane.req_dir.mkdir(parents=True, exist_ok=True)
    return lane


def write(lane: Lane, name: str, text: str) -> None:
    (lane.req_dir / name).write_text(text, encoding="utf-8")


class Marks(unittest.TestCase):
    def test_forms(self):
        d = TMP / "marks"
        d.mkdir(exist_ok=True)
        for text, want in [("RESULT: PASS\nbody", "PASS"), ("**VERDICT**: FAIL", "FAIL"), ("RESULT\uff1aPASS", "PASS"),
                           ("STATUS: BLOCKED\n\n# x", "BLOCKED"), ("RESULT: FAIL_PRODUCT", "FAIL_PRODUCT"),
                           ("no mark here", None), ("", None)]:
            (d / "m.md").write_text(text, encoding="utf-8")
            self.assertEqual(read_mark(d / "m.md"), want, text)
        self.assertIsNone(read_mark(d / "missing.md"))

    def test_log_signals(self):
        d = TMP / "logs"
        d.mkdir(exist_ok=True)
        (d / "a.log").write_text("x\nYou've hit your 5-hour limit \u00b7 resets 6pm", encoding="utf-8")
        (d / "b.log").write_text('{"response":"","denied_actions":[{"action":"command"}]}', encoding="utf-8")
        (d / "c.log").write_text("all fine", encoding="utf-8")
        self.assertTrue(usage_limited(d / "a.log"))
        self.assertFalse(usage_limited(d / "c.log"))
        self.assertTrue(permission_denied(d / "b.log"))
        self.assertFalse(permission_denied(d / "c.log"))


class Decisions(unittest.TestCase):
    """What happens after each stage, from the files it wrote."""

    def go(self, stage, files, number=1, **lane_kw):
        c = cfg()
        lane = fake_lane(c, stage=stage, number=number, **lane_kw)
        for name in list(lane.req_dir.iterdir()):
            name.unlink()
        for name, text in files.items():
            write(lane, name, text)
        nxt, status, _ = decide(c, lane, stage)
        return nxt, status, lane.data["round"]

    def test_plan(self):
        self.assertEqual(self.go("plan", {"spec.md": "STATUS: READY\n" + "x" * 80})[:2], ("spec_gate", "waiting"))
        self.assertEqual(self.go("plan", {"spec.md": "STATUS: BLOCKED\n" + "x" * 80})[:2], ("spec_gate", "waiting"))
        self.assertEqual(self.go("plan", {})[:2], ("plan", "blocked"))

    def test_code_and_fix(self):
        for stage in ("code", "fix"):
            self.assertEqual(self.go(stage, {"selftest.md": "RESULT: PASS"})[:2], ("review", "queued"))
            self.assertEqual(self.go(stage, {"selftest.md": "RESULT: FAIL"}), ("fix", "queued", 1), "failed self-test is one fix round")
            self.assertEqual(self.go(stage, {"selftest.md": "RESULT: FAIL"}, round=2)[:2], (stage, "blocked"), "rounds used up")
            self.assertEqual(self.go(stage, {"selftest.md": "RESULT: BLOCKED"})[:2], (stage, "blocked"))
            self.assertEqual(self.go(stage, {})[:2], (stage, "blocked"))

    def test_review(self):
        self.assertEqual(self.go("review", {"review.md": "VERDICT: PASS"})[:2], ("test", "queued"))
        self.assertEqual(self.go("review", {"review.md": "VERDICT: FAIL"}), ("fix", "queued", 1))
        self.assertEqual(self.go("review", {"review.md": "VERDICT: FAIL"}, round=2)[:2], ("review", "blocked"))
        self.assertEqual(self.go("review", {})[:2], ("review", "blocked"))

    def test_test_design(self):
        self.assertEqual(self.go("test", {"test-design.md": "RESULT: PASS"})[:2], ("itest", "queued"))
        self.assertEqual(self.go("test", {"test-design.md": "RESULT: BLOCKED"})[:2], ("test", "blocked"))
        self.assertEqual(self.go("test", {})[:2], ("test", "blocked"))

    def test_integration(self):
        self.assertEqual(self.go("itest", {"test-report.md": "RESULT: PASS"})[:2], ("report", "queued"))
        self.assertEqual(self.go("itest", {"test-report.md": "RESULT: FAIL_PRODUCT"}), ("fix", "queued", 1))
        self.assertEqual(self.go("itest", {"test-report.md": "RESULT: FAIL_TEST"}), ("test", "queued", 1))
        self.assertEqual(self.go("itest", {"test-report.md": "RESULT: FAIL_PRODUCT"}, round=2)[:2], ("report", "queued"),
                         "rounds used up: go to the summary, the person decides")
        self.assertEqual(self.go("itest", {"test-report.md": "RESULT: BLOCKED"})[:2], ("itest", "blocked"))

    def test_report(self):
        self.assertEqual(self.go("report", {"summary.md": "x"})[:2], ("merge_gate", "waiting"))
        self.assertEqual(self.go("report", {})[:2], ("report", "blocked"))


class Config_(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(merge({"a": {"x": 1, "y": 2}, "l": [1, 2]}, {"a": {"y": 3}, "l": [9]}), {"a": {"x": 1, "y": 3}, "l": [9]})

    def test_local_file_is_merged(self):
        c = cfg()
        self.assertIn("demo", c.projects)
        self.assertIn("agents", c.data, "general settings still come from lane.toml")
        self.assertEqual(c.worktree_root(), TMP / "ws")

    def test_command_drops_flag_with_empty_value(self):
        c = cfg()
        stage_cfg = {**c.stage("code"), "model": ""}
        cmd = build_command(c, stage_cfg, "do it", TMP, settings="")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--settings", cmd)
        self.assertIn("do it", cmd)
        self.assertIn("--model", build_command(c, {**stage_cfg, "model": "m1"}, "p", TMP))


class Verify(unittest.TestCase):
    def test_build_only_vs_full(self):
        c = cfg()
        lane = fake_lane(c, number=7)
        proj = c.project("demo")
        short, full = render_verify(lane, proj, full=False), render_verify(lane, proj, full=True)
        for cmd in ("make setup", "make build", "make unit"):
            self.assertIn(cmd, short)
        self.assertNotIn("make e2e", short)
        self.assertNotIn("serve", short)
        self.assertIn("make e2e", full)
        self.assertIn(f"serve {lane.worktree} 64007", full, "port is port_base + lane number, {worktree} is filled in")
        self.assertIn("BASE_URL=http://localhost:64007", full)
        self.assertIn("http://localhost:64007/", full)


class Notifications(unittest.TestCase):
    def messages(self, c, **kw):
        sent = []
        notify.send = lambda _c, text: sent.append(text)
        try:
            return notify.on_stage_end(c, fake_lane(c, **kw), "plan")
        finally:
            importlib_reload()

    def test_gate_and_stage_done(self):
        c = cfg()
        gate = self.messages(c, stage="spec_gate", status="waiting")
        self.assertEqual(len(gate), 1)
        merge_gate = self.messages(c, stage="merge_gate", status="waiting")
        self.assertEqual(len(merge_gate), 1)
        self.assertNotEqual(gate, merge_gate)
        done = {"stage": "review", "status": "queued", "history": [{"at": "x", "stage": "code", "result": "ok"}]}
        self.assertEqual(self.messages(c, **done), [], "off by default")
        c.data["jenny"]["notify_stage_done"] = True
        self.assertEqual(len(self.messages(c, **done)), 1)
        retry = {**done, "history": [{"at": "x", "stage": "code", "result": "limit"}]}
        self.assertEqual(self.messages(c, **retry), [], "a retry after a usage limit is not a completion")
        self.assertEqual(self.messages(c, stage="code", status="blocked"), [], "blocked goes through alerts")
        c.data["jenny"]["notify_gates"] = False
        self.assertEqual(self.messages(c, stage="spec_gate", status="waiting"), [])

    def test_message_travels_in_environment(self):
        out = TMP / "sent.txt"
        probe = TMP / "probe.py"
        probe.write_text("import os,sys\nopen(sys.argv[1],'a',encoding='utf-8').write(os.environ['LANE_MESSAGE']+'\\n')\n", encoding="utf-8")
        c = cfg()
        c.data["jenny"]["notify_command"] = [sys.executable, str(probe), str(out)]
        nasty = "title with 'single' and \"double\" quotes; & echo injected"
        notify.send(c, nasty)
        self.assertEqual(out.read_text(encoding="utf-8").strip(), nasty)


def importlib_reload():
    import importlib
    importlib.reload(notify)


class Alerts(unittest.TestCase):
    def keys(self, c, *lanes, waiting_budget=()):
        return set(jenny.find_alerts(c, list(lanes), list(waiting_budget)))

    def test_blocked_and_stale(self):
        c = cfg()
        old = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
        blocked = fake_lane(c, status="blocked", number=2)
        stale = fake_lane(c, status="running", number=3, heartbeat=old)
        fresh = fake_lane(c, status="running", number=4, heartbeat=datetime.now().isoformat(timespec="seconds"))
        keys = self.keys(c, blocked, stale, fresh)
        self.assertTrue(any(k.startswith("REQ-002:blocked") for k in keys))
        self.assertTrue(any(k.startswith("REQ-003:stale") for k in keys))
        self.assertFalse(any(k.startswith("REQ-004") for k in keys))

    def test_fresh_dispatch_is_not_stale(self):
        c = cfg()
        old = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
        lane = fake_lane(c, number=5, heartbeat=old)
        mark_running(lane)
        self.assertEqual(lane.data["status"], "running")
        self.assertFalse(any("stale" in k for k in self.keys(c, lane)), "regression: old heartbeat from the previous run")

    def test_gate_waiting_and_last_round_and_budget(self):
        c = cfg()
        long_ago = (datetime.now() - timedelta(hours=6)).isoformat(timespec="seconds")
        waiting = fake_lane(c, stage="spec_gate", status="waiting", number=6, since=long_ago)
        soon = fake_lane(c, stage="spec_gate", status="waiting", number=7)
        last = fake_lane(c, stage="fix", number=8, round=2)
        keys = self.keys(c, waiting, soon, last)
        self.assertTrue(any(k.startswith("REQ-006:gate") for k in keys))
        self.assertFalse(any(k.startswith("REQ-007") for k in keys))
        self.assertIn("REQ-008:lastround", keys)
        c.data["budget"] = {"claude": 2}
        self.assertFalse(any(k.startswith("budget:claude") for k in self.keys(c)))
        add_usage(c, "claude")
        add_usage(c, "claude")
        self.assertTrue(any(k.startswith("budget:claude:full") for k in self.keys(c)))

    def test_alerts_carry_project(self):
        c = cfg()
        alerts = jenny.find_alerts(c, [fake_lane(c, status="blocked", number=9)], [])
        entry = next(iter(alerts.values()))
        self.assertEqual((entry["project"], entry["lane"]), ("demo", "REQ-009"))


class Operations(unittest.TestCase):
    """Real lanes on a throw-away git repository."""

    @classmethod
    def setUpClass(cls):
        cls.c = cfg()

    def new_lane(self, stage="spec_gate", status="waiting"):
        ops.new(self.c, "demo", "title", "text")
        lane_id = sorted(p.stem for p in self.c.lanes_dir.glob("REQ-*.json"))[-1]
        lane = Lane(self.c, json.loads((self.c.lanes_dir / f"{lane_id}.json").read_text(encoding="utf-8")))
        write(lane, "spec.md", "STATUS: READY\n" + "x" * 80)
        lane.set(stage=stage, status=status)
        return lane.id

    def fresh(self, lane_id):
        return json.loads((self.c.lanes_dir / f"{lane_id}.json").read_text(encoding="utf-8"))

    def test_new_lane_lives_in_the_workspace(self):
        lane_id = self.new_lane()
        d = self.fresh(lane_id)
        self.assertEqual(Path(d["worktree"]).parent.parent, TMP / "ws")
        self.assertTrue((Path(d["worktree"]) / "docs" / "reqs" / lane_id / "request.md").exists())
        self.assertEqual(ops.new.__name__, "new")

    def test_approve_with_notes_and_edit(self):
        lane_id = self.new_lane()
        d = self.fresh(lane_id)
        spec = Path(d["worktree"]) / "docs" / "reqs" / lane_id / "spec.md"
        original = spec.read_text(encoding="utf-8")
        with self.assertRaises(SystemExit):
            ops.edit_spec(self.c, lane_id, "  \n")
        ops.edit_spec(self.c, lane_id, original)   # unchanged: no history version
        self.assertFalse((spec.parent / "history").exists())
        ops.edit_spec(self.c, lane_id, "STATUS: READY\n\nnew spec\n")
        self.assertEqual((spec.parent / "history" / "01-spec.md").read_text(encoding="utf-8"), original)
        ops.approve(self.c, lane_id, "keep the login page untouched")
        feedback = (spec.parent / "feedback.md").read_text(encoding="utf-8")
        self.assertIn(ops.APPROVAL_NOTE_TITLE, feedback)
        self.assertIn("keep the login page untouched", feedback)
        self.assertEqual((self.fresh(lane_id)["stage"], self.fresh(lane_id)["status"]), ("code", "queued"))
        with self.assertRaises(SystemExit):   # locked after approval
            ops.edit_spec(self.c, lane_id, "again")

    def test_notes_only_at_the_spec_gate(self):
        lane_id = self.new_lane(stage="merge_gate")
        with self.assertRaises(SystemExit):
            ops.approve(self.c, lane_id, "notes at the merge gate")
        ops.approve(self.c, lane_id)
        self.assertEqual(self.fresh(lane_id)["status"], "done")

    def test_reject_needs_a_reason_and_goes_back(self):
        lane_id = self.new_lane()
        with self.assertRaises(SystemExit):
            ops.reject(self.c, lane_id, "   ")
        ops.reject(self.c, lane_id, "too broad")
        self.assertEqual((self.fresh(lane_id)["stage"], self.fresh(lane_id)["status"]), ("plan", "queued"))

    def test_resume_and_clean_rules(self):
        lane_id = self.new_lane(stage="code", status="blocked")
        with self.assertRaises(SystemExit):
            ops.resume(self.c, lane_id, "spec_gate")   # not an automatic stage
        ops.resume(self.c, lane_id, "review", reset_rounds=True)
        self.assertEqual(self.fresh(lane_id)["stage"], "review")
        with self.assertRaises(SystemExit):
            ops.clean(self.c, lane_id)   # not done, no force
        wt = Path(self.fresh(lane_id)["worktree"])
        ops.clean(self.c, lane_id, force=True)
        self.assertFalse(wt.exists())
        self.assertTrue((self.c.state_dir / "archive" / f"{lane_id}.json").exists())

    def test_ids_are_not_reused_after_archive(self):
        first = self.new_lane()
        ops.clean(self.c, first, force=True)
        self.assertNotEqual(self.new_lane(), first)


class Login(unittest.TestCase):
    def setUp(self):
        self.home = TMP / f"login-{self.id().split('.')[-1]}"
        (self.home / "config").mkdir(parents=True, exist_ok=True)

    def config(self, password):
        (self.home / "config" / "server.toml").write_text(
            f'[server]\nhost = "127.0.0.1"\nport = 1\n[login]\nusername = "admin"\npassword = "{password}"\n', encoding="utf-8")

    def test_login_lock_and_password_change(self):
        self.config("pw")
        s = Sessions(self.home)
        token = s.login("1.2.3.4", "admin", "pw")
        self.assertTrue(token and s.valid(token))
        self.assertIsNone(s.login("1.2.3.4", "admin", "wrong"))
        self.assertIsNone(s.login("1.2.3.4", "wrong", "pw"))
        self.config("changed")
        self.assertFalse(s.valid(token), "changing the password signs everybody out")
        for _ in range(5):
            s.login("9.9.9.9", "admin", "bad")
        self.assertGreater(s.locked("9.9.9.9"), 0)
        self.assertEqual(s.locked("1.2.3.4"), 0, "another address is not locked")

    def test_empty_password_is_refused(self):
        path, created = ensure_server_config(self.home)
        self.assertTrue(created)
        self.assertFalse(ensure_server_config(self.home)[1], "an existing file is never overwritten")
        with self.assertRaises(SystemExit):
            read_server_config(self.home)
        self.config("x")
        self.assertEqual(read_server_config(self.home)["login"]["password"], "x")


class WebConfigFiles(unittest.TestCase):
    def test_only_known_files_can_be_edited(self):
        for ok in ("lane.toml", "local.toml", "templates/AGENTS.md", "rules/demo.md"):
            api.config_path(HOME, ok)
        for bad in ("server.toml", "../lane/core.py", "rules/../server.toml", "rules/a.txt", "x"):
            with self.assertRaises(SystemExit, msg=bad):
                api.config_path(HOME, bad)

    def test_validation_before_saving(self):
        good = (ROOT / "config" / "lane.toml").read_text(encoding="utf-8")
        api.check_lane_toml(good)
        with self.assertRaises(SystemExit):
            api.check_lane_toml("[broken")
        with self.assertRaises(SystemExit):
            api.check_lane_toml(good.replace("[stages.itest]", "[stages.gone]"))
        api.check_local_toml('[projects.x]\nrepo = "p"\n')
        with self.assertRaises(SystemExit):
            api.check_local_toml('[projects.x]\nbase = "main"\n')


class PythonVersion(unittest.TestCase):
    def test_old_python_gets_a_clear_message(self):
        import lane
        lane.check_python((3, 11, 0))
        lane.check_python((3, 12, 4))
        with self.assertRaises(SystemExit) as ctx:
            lane.check_python((3, 10, 12))
        self.assertIn("3.11", str(ctx.exception))
        self.assertIn("3.10.12", str(ctx.exception))


class ProcessControl(unittest.TestCase):
    """启动并结束一棵进程树。Windows 走 taskkill，Linux/macOS 走进程组（os.killpg）：两种系统都会跑这个测试。"""

    def test_kill_tree_kills_children_too(self):
        from lane.agents import kill_tree
        from lane.runner import pid_alive
        child_pid_file = TMP / "child.pid"
        child_pid_file.unlink(missing_ok=True)
        parent_code = ("import subprocess, sys, time\n"
                       "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                       f"open({str(child_pid_file)!r}, 'w').write(str(c.pid))\n"
                       "time.sleep(60)\n")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0   # 和 run_agent 的启动方式一致
        proc = subprocess.Popen([sys.executable, "-c", parent_code], creationflags=flags, start_new_session=(os.name != "nt"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = None
        try:
            for _ in range(100):   # 最多等 10 秒，等子进程写下自己的编号
                if child_pid_file.exists() and child_pid_file.read_text().strip():
                    child = int(child_pid_file.read_text())
                    break
                import time
                time.sleep(0.1)
            self.assertIsNotNone(child, "子进程没有启动")
            self.assertTrue(pid_alive(proc.pid) and pid_alive(child))
            kill_tree(proc.pid)
            proc.wait(timeout=10)
            for _ in range(50):
                if not pid_alive(child):
                    break
                import time
                time.sleep(0.1)
            self.assertFalse(pid_alive(proc.pid), "父进程应已结束")
            self.assertFalse(pid_alive(child), "子进程也应被一起结束")
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    unittest.main(verbosity=1)
