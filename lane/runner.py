"""阶段结果判定和并行调度。"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import jenny, notify
from .agents import kill_tree, render_prompt, run_agent
from .core import (AUTO_STAGES, Config, Lane, agent_paused, all_lanes, changed_files, commit_all, load_lane, now,
                   pause_agent, revert_file, usage_today)

MARK = re.compile(r"^\s*(?:\*\*)?(VERDICT|RESULT|STATUS)(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*([A-Za-z_]+)", re.M)


# ---- 阶段结果判定 ------------------------------------------------------

def read_mark(path: Path) -> str | None:
    if not path.exists():
        return None
    m = MARK.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(2).upper() if m else None


def enforce_scope(cfg: Config, lane: Lane, stage: str) -> tuple[list[str], str]:
    """检查阶段是否改了不该改的文件。返回 (越界文件, 处理方式)。"""
    stage_cfg = cfg.stage(stage)
    allowed: list[str] = []
    for w in stage_cfg.get("writes", ["*"]):
        if w == "{req_dir}":
            allowed.append(lane.req_rel)
        elif w == "{test_paths}":
            allowed += cfg.project(lane.data["project"]).get("test_paths", [])
        else:
            allowed.append(w)
    if "*" in allowed:
        return [], ""
    allowed = [a.replace("\\", "/") for a in allowed]
    bad = [(c, p) for c, p in changed_files(lane.worktree) if not any(p.startswith(a) for a in allowed)]
    policy = stage_cfg.get("on_violation", "revert")
    if bad and policy == "revert":
        for code, path in bad:
            revert_file(lane.worktree, code, path)
    return [p for _, p in bad], policy


def permission_denied(log_path: Path) -> bool:
    """无头模式下命令权限被拒时，agy 会在输出里留下 denied_actions 或 auto-denied 字样。"""
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    return bool(re.search(r'"denied_actions"\s*:\s*\[\s*\{', tail)) or "auto-denied" in tail


LIMIT = re.compile(r"hit your .{0,40}limit|usage limit|rate.?limit|quota.{0,20}(exceeded|exhausted)|RESOURCE_EXHAUSTED", re.I)


def usage_limited(log_path: Path) -> bool:
    """工具因为用量额度用完而没干活时，会在输出末尾留下这类字样。只在阶段没有产出结果时才查。"""
    return bool(LIMIT.search(log_path.read_text(encoding="utf-8", errors="replace")[-1500:]))


def decide(cfg: Config, lane: Lane, stage: str) -> tuple[str, str, str]:
    """根据阶段产出的文件决定下一步：(阶段, 状态, 说明)。"""
    req = lane.req_dir
    max_rounds = cfg.get("review", "max_rounds", 2)
    if stage == "plan":
        spec = req / "spec.md"
        if spec.exists() and spec.stat().st_size > 50:
            if read_mark(spec) == "BLOCKED":  # 仍然停在闸门等人，但要让人一眼看到
                return "spec_gate", "waiting", "规划阶段提出了需要你决定的阻塞问题，见 spec.md 开头和「风险和待确认问题」一节"
            return "spec_gate", "waiting", "规格已起草，请阅读 spec.md 后批准或退回"
        return "plan", "blocked", "规划阶段没有产出 spec.md"
    if stage in ("code", "fix"):
        mark = read_mark(req / "selftest.md")
        if mark == "PASS":
            return "review", "queued", ""
        if mark is None:
            return stage, "blocked", "没有产出带 RESULT 的 selftest.md"
        if mark == "FAIL":  # 构建或单元测试没过：算一轮修复，不浪费评审
            lane.data["round"] = lane.data.get("round", 0) + 1
            if lane.data["round"] > max_rounds:
                return stage, "blocked", f"自测连续失败，已用完 {max_rounds} 轮修复，见 selftest.md"
            return "fix", "queued", "自测失败，退回修复"
        return stage, "blocked", f"自测结果 {mark}，见 selftest.md"
    if stage == "review":
        mark = read_mark(req / "review.md")
        if mark == "PASS":
            return "test", "queued", ""
        if mark == "FAIL":
            lane.data["round"] = lane.data.get("round", 0) + 1
            if lane.data["round"] > max_rounds:
                return "review", "blocked", f"评审连续 {max_rounds} 轮修复后仍不通过，需要你决定"
            return "fix", "queued", ""
        return "review", "blocked", "没有产出带 VERDICT 的 review.md"
    if stage == "test":  # 测试设计：只写测试，不占共享资源
        mark = read_mark(req / "test-design.md")
        if mark == "PASS":
            return "itest", "queued", ""
        if mark is None:
            return "test", "blocked", "没有产出带 RESULT 的 test-design.md"
        return "test", "blocked", f"测试设计结果 {mark}，见 test-design.md"
    if stage == "itest":  # 集成运行：排队执行
        mark = read_mark(req / "test-report.md")
        if mark == "PASS":
            return "report", "queued", ""
        if mark in ("FAIL_PRODUCT", "FAIL_TEST"):
            back = "fix" if mark == "FAIL_PRODUCT" else "test"
            lane.data["round"] = lane.data.get("round", 0) + 1
            if lane.data["round"] > max_rounds:
                return "report", "queued", "集成测试仍有失败，修复轮次已用完，汇总里会列出"
            return back, "queued", f"集成测试失败（{mark}），退回{'修复' if back == 'fix' else '测试设计'}"
        if mark is None:
            return "itest", "blocked", "没有产出带 RESULT 的 test-report.md"
        return "itest", "blocked", f"集成测试结果 {mark}，见 test-report.md"
    if stage == "report":
        if (req / "summary.md").exists():
            return "merge_gate", "waiting", "汇总已生成，请阅读 summary.md 后确认"
        return "report", "blocked", "没有产出 summary.md"
    raise ValueError(stage)


WAKE = threading.Event()   # 阶段结束时置位，让调度器马上开始下一个阶段，不用等满一轮轮询间隔


def run_and_wake(cfg: Config, lane: Lane) -> None:
    try:
        run_stage(cfg, lane)
    finally:
        WAKE.set()


def mark_running(lane: Lane) -> None:
    """派出阶段时立刻写下新的心跳。否则 Jenny 紧接着检查，会拿上一次运行（可能是几小时前）留下的旧心跳，
    误报"没有心跳"。"""
    lane.data["heartbeat"] = now()
    lane.set(status="running")


def run_stage(cfg: Config, lane: Lane) -> None:
    stage = lane.data["stage"]
    stage_cfg = cfg.stage(stage)
    lane.data["heartbeat"] = now()   # 否则 Jenny 会拿上一次运行留下的旧心跳误报"没有心跳"
    lane.set(status="running", note="")
    started = now()
    # 清掉本阶段上一次的结果文件，避免把旧结论当成新的
    stale = {"code": "selftest.md", "fix": "selftest.md", "review": "review.md",
             "test": "test-design.md", "itest": "test-report.md", "report": "summary.md"}.get(stage)
    if stale and (lane.req_dir / stale).exists():
        prev = lane.req_dir / "history"
        prev.mkdir(exist_ok=True)
        n = len(list(prev.glob(f"*-{stale}"))) + 1
        (lane.req_dir / stale).replace(prev / f"{n:02d}-{stale}")
    try:
        outcome, log_path = run_agent(cfg, lane, stage, stage_cfg, render_prompt(cfg, lane, stage))
    except Exception as exc:  # 找不到命令、提示词缺失等
        lane.event(stage=stage, started=started, result="error", note=str(exc))
        lane.set(status="blocked", note=str(exc))
        return

    violations, policy = enforce_scope(cfg, lane, stage)
    denied = permission_denied(log_path)
    if outcome == "timeout":
        nxt, status, note = stage, "blocked", f"超时（{stage_cfg.get('timeout_min')} 分钟），已终止"
    elif violations and policy == "block":
        nxt, status, note = stage, "blocked", "改动了允许范围之外的文件：" + "、".join(violations[:5])
    else:
        nxt, status, note = decide(cfg, lane, stage)
        if status == "blocked" and usage_limited(log_path):
            # 工具的用量额度用完：不算卡住。暂停这个工具一段时间，需求留在原阶段排队，到时自动重试
            minutes = cfg.get("workbench", "limit_pause_minutes", 30)
            pause_agent(cfg, stage_cfg["agent"], minutes)
            nxt, status = stage, "queued"
            note = f"{stage_cfg['agent']} 的用量额度用完了，{minutes} 分钟后自动重试（调度器需要保持运行）"
            outcome = "limit"
        elif denied and status == "blocked":
            note = ("工具执行了不在允许列表里的命令，被拒绝后会话直接结束。"
                    "在 config/local.toml 里该项目的 allow_commands（或 lane.toml 里 [agents.agy] 的 allow_commands）补上需要的命令后 resume；原说明：" + note)
        if outcome == "error" and status != "blocked":
            note = (note + "；" if note else "") + "工具退出码非 0，但产出文件齐全"
    try:
        commit_all(lane.worktree, f"{lane.id}: {stage}" + (f" (round {lane.data.get('round', 0)})" if stage in ("fix", "review") else ""))
    except RuntimeError as exc:
        status, note = "blocked", f"提交失败：{exc}"
        nxt = stage
    lane.event(stage=stage, started=started, result=outcome, next=nxt, status=status, note=note,
               log=str(log_path), reverted=violations if policy == "revert" else [])
    lane.set(stage=nxt, status=status, note=note)

    if status == "blocked" and cfg.get("jenny", "diagnose", False):
        diagnose(cfg, lane, stage, log_path)


def diagnose(cfg: Config, lane: Lane, stage: str, log_path: Path) -> None:
    """卡住后让 Claude 读日志尾部，写一份给人看的诊断。失败不影响状态。"""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
        (lane.worktree / ".lane" / "last-log.txt").write_text(tail, encoding="utf-8")
        prompt = render_prompt(cfg, lane, "diagnose").replace("{{stage}}", stage).replace("{{note}}", lane.data.get("note", ""))
        stage_cfg = {"agent": "claude", "mode": "acceptEdits", "timeout_min": 5}
        run_agent(cfg, lane, "diagnose", stage_cfg, prompt, model=cfg.get("jenny", "diagnose_model", "haiku"))
        enforce_scope_for_diagnose(lane)
        lane.save()
    except Exception as exc:
        lane.event(stage="diagnose", result="error", note=str(exc))
        lane.save()


def enforce_scope_for_diagnose(lane: Lane) -> None:
    for code, path in changed_files(lane.worktree):
        if path != lane.req_rel + "diagnosis.md":
            revert_file(lane.worktree, code, path)


# ---- 调度器 ------------------------------------------------------------

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def recover(cfg: Config) -> None:
    """调度器启动时，把上次中断在"运行中"的需求重新排队。"""
    for lane in all_lanes(cfg):
        if lane.data["status"] == "running":
            if pid_alive(lane.data.get("pid")):
                kill_tree(lane.data["pid"])
            lane.event(stage=lane.data["stage"], result="interrupted", note="调度器重启，阶段重新排队")
            lane.data["pid"] = None
            lane.set(status="queued", note="上次运行被中断，重新排队")


def acquire_lock(cfg: Config) -> Path:
    lock = cfg.state_dir / "scheduler.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text())
        except ValueError:
            pid = 0
        if pid_alive(pid) and pid != os.getpid():
            raise SystemExit(f"已有一个调度器在运行（PID {pid}）。同一时间只能有一个 lane run。")
    lock.write_text(str(os.getpid()))
    return lock


def scheduler(cfg_factory, once: bool = False) -> None:
    cfg: Config = cfg_factory()
    lock = acquire_lock(cfg)
    recover(cfg)
    threads: dict[str, tuple[threading.Thread, str, str]] = {}  # 需求编号 -> (线程, 工具名, 阶段)
    print(f"调度器已启动，工作台：{cfg.home}。Ctrl+C 停止（正在运行的阶段会被终止并在下次启动时重排）。")
    try:
        while True:
            try:
                cfg = cfg_factory()  # 配置改了立即生效
            except Exception as exc:  # 配置正在被保存，或被改坏了：沿用上一份，不让调度器退出
                print(f"[{now()[11:]}] 配置读取失败，沿用上一份：{exc}")
            for lane_id in [k for k, v in threads.items() if not v[0].is_alive()]:
                finished_stage = threads.pop(lane_id)[2]
                try:
                    lane = load_lane(cfg, lane_id)
                except SystemExit:   # 阶段刚结束就被人批准并清理了：只是少打一行，不能让调度器退出
                    continue
                print(f"[{now()[11:]}] {lane_id} → {lane.data['stage']} / {lane.data['status']}  {lane.data.get('note', '')}")
                notify.on_stage_end(cfg, lane, finished_stage)

            used = usage_today(cfg)
            waiting_budget = []
            for lane in sorted(all_lanes(cfg), key=lambda l: l.data["created"]):
                if lane.id in threads or lane.data["status"] != "queued" or lane.data["stage"] not in AUTO_STAGES:
                    continue
                stage = lane.data["stage"]
                agent = cfg.stage(stage)["agent"]
                if len(threads) >= cfg.get("workbench", "max_parallel", 2):
                    break
                if sum(1 for v in threads.values() if v[1] == agent) >= cfg.agent(agent).get("max_parallel", 99):
                    continue
                # 阶段自己的并发上限：测试阶段设为 1，集成测试（共享数据库、CoverageHub）就会排队
                if sum(1 for v in threads.values() if v[2] == stage) >= cfg.stage(stage).get("max_parallel", 99):
                    continue
                if agent_paused(cfg, agent):
                    continue   # 暂停是暂时的：需求留在队列里，不算"额度用完"，也不能让 --once 提前退出
                if used.get(agent, 0) >= cfg.data.get("budget", {}).get(agent, 10 ** 9):
                    waiting_budget.append(lane.id)
                    continue
                print(f"[{now()[11:]}] {lane.id} 开始 {lane.data['stage']}（{agent}）")
                t = threading.Thread(target=run_and_wake, args=(cfg, lane), daemon=True)
                threads[lane.id] = (t, agent, stage)
                used[agent] = used.get(agent, 0) + 1
                mark_running(lane)
                t.start()

            for alert in jenny.tick(cfg, waiting_budget):
                print(f"[Jenny] {alert}")

            if once and not threads:
                if not any(l.data["status"] == "queued" and l.data["stage"] in AUTO_STAGES and l.id not in waiting_budget
                           for l in all_lanes(cfg)):
                    break
            if WAKE.wait(0.2 if once else cfg.get("workbench", "poll_seconds", 15)):
                WAKE.clear()
                time.sleep(0.05)   # 让刚结束的阶段线程真正退出，下一轮才认得出它已结束
    except KeyboardInterrupt:
        print("\n正在停止……")
        for lane_id in threads:
            pid = load_lane(cfg, lane_id).data.get("pid")
            if pid:
                kill_tree(pid)
    finally:
        lock.unlink(missing_ok=True)
