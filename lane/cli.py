"""lane 命令行。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import driver, jenny, ops
from .core import AUTO_STAGES, STAGE_LABEL, Config, all_lanes, load_lane
from .runner import scheduler


def cmd_new(cfg: Config, args) -> None:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.message or args.title)
    print(ops.new(cfg, args.project, args.title, text))


def cmd_status(cfg: Config, args) -> None:
    lanes = all_lanes(cfg)
    alerts = jenny.find_alerts(cfg, lanes, [])
    print(jenny.render_status(cfg, lanes, alerts))


def cmd_approve(cfg: Config, args) -> None:
    print(ops.approve(cfg, args.id, args.message or ""))


def cmd_reject(cfg: Config, args) -> None:
    print(ops.reject(cfg, args.id, args.message))


def cmd_resume(cfg: Config, args) -> None:
    print(ops.resume(cfg, args.id, args.stage, args.reset_rounds))


def cmd_open(cfg: Config, args) -> None:
    lane = load_lane(cfg, args.id)
    print(f"需求目录：{lane.req_dir}")
    for name in ("request.md", "spec.md", "selftest.md", "review.md", "test-design.md", "test-report.md", "summary.md", "feedback.md", "diagnosis.md"):
        if (lane.req_dir / name).exists():
            print(f"  {name}")
    print("\n想自己和 Opus 交互讨论规格（代替自动起草），在新终端里运行：")
    print(f"  cd \"{lane.worktree}\"")
    print("  claude --model opus")
    print(f"让它把结论写进 {lane.req_rel}spec.md，然后 lane approve {lane.id}")


def cmd_log(cfg: Config, args) -> None:
    lane = load_lane(cfg, args.id)
    for h in lane.data.get("history", []):
        print(f"{h['at'][5:16]}  {STAGE_LABEL.get(h.get('stage', ''), h.get('stage', '')):8} {h.get('result', ''):11} {h.get('note', '')}")
    logs = sorted((cfg.logs_dir / lane.id).glob("*.log"))
    if logs:
        print(f"\n最近一次日志 {logs[-1]}（末尾 {args.lines} 行）：\n")
        text = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(text[-args.lines:]))


def cmd_clean(cfg: Config, args) -> None:
    print(ops.clean(cfg, args.id, args.force))


def cmd_ui(cfg: Config, args) -> None:
    from .ui.server import serve
    serve(cfg, open_browser=not args.no_browser, readonly=True if args.readonly else None)


def cmd_driver(cfg: Config, args) -> None:
    act = args.driver_cmd
    if act == "init":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.message or args.title)
        print(json.dumps(driver.init_lane(cfg, args.project, args.title, text, args.tier), ensure_ascii=False, indent=2))
    elif act == "stage":
        print(json.dumps(driver.set_stage(cfg, args.id, args.stage, args.status, args.note or "", args.commit, args.result),
                         ensure_ascii=False, indent=2))
    elif act == "preflight":
        rc, res = driver.run_preflight_cmd(cfg, args.id, skip_verify=args.skip_verify)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(rc)
    elif act == "review":
        rc, res = driver.run_review(cfg, args.id, kind=args.kind, agent=args.agent, model=args.model, effort=args.effort)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(rc)
    elif act == "approve":
        print(ops.approve(cfg, args.id, args.message or ""))
    elif act == "finish":
        print(driver.finish_lane(cfg, args.id, clean=args.clean))
    elif act == "lock":
        print(json.dumps(driver.acquire_lock(cfg, args.resource, args.id), ensure_ascii=False))
    elif act == "unlock":
        print(driver.release_lock(cfg, args.resource, args.id, force=args.force))


def cmd_profile(cfg: Config, args) -> None:
    print(driver.switch_profile(cfg, args.name) if args.name else driver.format_profile_table(cfg))


def cmd_rules(cfg: Config, args) -> None:
    cfg.project(args.project)
    target = cfg.home / "config" / "rules" / f"{args.project}.md"
    if target.exists():
        print(f"规则文件已存在，直接编辑即可：{target}")
        return
    source = Path(args.source) if args.source else cfg.home / "templates" / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8")
    print(f"已从 {source} 创建 {target}")
    print(f"请按项目情况编辑它，并确认 config/local.toml 的 [projects.{args.project}] 里有：")
    print(f'  rules = "config/rules/{args.project}.md"')


def cmd_doctor(cfg: Config, args) -> None:
    ok = True
    for name, agent in cfg.data["agents"].items():
        exe = shutil.which(agent["command"][0])
        print(f"{'OK  ' if exe else '缺失'} {name}: {exe or agent['command'][0] + ' 不在 PATH 里'}")
        ok &= bool(exe)
    print(f"{'OK  ' if shutil.which('git') else '缺失'} git")
    root = cfg.worktree_root()
    anchor = next(p for p in (root, *root.parents) if p.exists())   # 目录可能还没建：看它最近的已存在的上级
    writable = os.access(anchor, os.W_OK)
    print(f"{'OK  ' if writable else '问题'} 工作空间（各需求的工作树）：{root}" + ("" if writable else f"（{anchor} 不可写）"))
    ok &= writable
    for stage in AUTO_STAGES + ("diagnose",):
        for f in ([f"{stage}.md"] + (["itest-coveragehub.md"] if stage == "itest" else [])):
            if not (cfg.home / "prompts" / f).exists():
                print(f"缺失 prompts/{f}")
                ok = False
    if not cfg.projects:
        print("提示 config/local.toml 里还没有登记项目（[projects.xxx]，写法见 config/local.toml.example）")
    for name, proj in cfg.projects.items():
        repo = Path(proj["repo"])
        good = (repo / ".git").exists()
        base = proj.get("base", "main")
        if good:
            good = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", base],
                                  capture_output=True).returncode == 0
        print(f"{'OK  ' if good else '问题'} 项目 {name}: {repo}（基线 {base}）")
        ok &= good
        rules = proj.get("rules")
        if not rules:
            print(f"提示 项目 {name} 没有配置规则文件（lane rules {name}）")
        elif not (cfg.home / rules).exists() and not Path(rules).exists():
            print(f"缺失 项目 {name} 的规则文件 {rules}（lane rules {name}）")
            ok = False
    sys.exit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="lane", description="多 Agent 工作台：每个需求一条流水线")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="创建需求")
    s.add_argument("project")
    s.add_argument("title")
    s.add_argument("-m", "--message", help="需求描述")
    s.add_argument("-f", "--file", help="从文件读需求描述")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("run", help="启动调度器（含 Jenny），持续推进所有需求")
    s.add_argument("--once", action="store_true", help="把能推进的都推进完就退出")
    s.set_defaults(fn=lambda cfg, a: scheduler(Config, once=a.once))

    sub.add_parser("status", help="在终端里查看全部需求和当前告警").set_defaults(fn=cmd_status)

    s = sub.add_parser("approve", help="批准规格，或确认汇总")
    s.add_argument("id")
    s.add_argument("-m", "--message", help="批准规格时附带的意见，编码阶段会读到")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reject", help="退回规格，或退回去修复")
    s.add_argument("id")
    s.add_argument("-m", "--message", required=True)
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("resume", help="让卡住的需求继续")
    s.add_argument("id")
    s.add_argument("--stage", help="从指定阶段继续")
    s.add_argument("--reset-rounds", action="store_true", help="修复轮次清零")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("open", help="显示需求的文件位置和交互规划的方法")
    s.add_argument("id")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("log", help="查看需求的经过和最近日志")
    s.add_argument("id")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("clean", help="移除已完成需求的工作树")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("rules", help="从模板为项目创建规则文件（config/rules/<项目>.md）")
    s.add_argument("project")
    s.add_argument("--from", dest="source", help="用指定文件代替 templates/AGENTS.md 作为起点")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("ui", help="启动网页界面（看板、统计、操作、配置）")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--readonly", action="store_true", help="以只读监控模式启动（隐藏操作按钮并禁止写接口）")
    s.set_defaults(fn=cmd_ui)

    d = sub.add_parser("driver", help="AGY 入口（multiagents-lane）使用的确定性原子命令")
    ds = d.add_subparsers(dest="driver_cmd", required=True)
    p1 = ds.add_parser("init", help="创建独立 worktree 并登记为 AGY 驱动需求")
    p1.add_argument("project")
    p1.add_argument("title")
    p1.add_argument("-m", "--message", help="需求描述")
    p1.add_argument("-f", "--file", help="从文件读需求描述")
    p1.add_argument("--tier", choices=("standard", "light"), default="standard")
    p2 = ds.add_parser("stage", help="更新阶段、状态和心跳")
    p2.add_argument("id")
    p2.add_argument("stage")
    p2.add_argument("status")
    p2.add_argument("--note", default="")
    p2.add_argument("--commit", action="store_true")
    p2.add_argument("--result", default="ok")
    p3 = ds.add_parser("preflight", help="执行 0-Token 预检门禁并生成评审包")
    p3.add_argument("id")
    p3.add_argument("--skip-verify", action="store_true")
    p4 = ds.add_parser("review", help="受控调用评审 Agent 执行规划或代码评审（支持临时覆写工具/模型/推理深度）")
    p4.add_argument("id")
    p4.add_argument("--kind", choices=("plan", "code"), default="code")
    p4.add_argument("--agent", help="临时覆盖该阶段使用的工具（如 claude / agy）")
    p4.add_argument("--model", help="临时覆盖该阶段使用的模型 ID")
    p4.add_argument("--effort", help="临时覆盖推理深度（如 low / medium / high）")
    p5 = ds.add_parser("approve", help="通过人工闸门")
    p5.add_argument("id")
    p5.add_argument("-m", "--message", help="批准规格时附带的意见")
    p6 = ds.add_parser("finish", help="确认完成（可选清理 worktree 并归档）")
    p6.add_argument("id")
    p6.add_argument("--clean", action="store_true")
    p7 = ds.add_parser("lock", help="获取共享资源锁（如 itest）")
    p7.add_argument("resource")
    p7.add_argument("id")
    p8 = ds.add_parser("unlock", help="释放共享资源锁")
    p8.add_argument("resource")
    p8.add_argument("id")
    p8.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_driver)

    sp = sub.add_parser("profile", help="查看当前各阶段生效的模型配置，或切换方案（如 claudeplus / agy-only）")
    sp.add_argument("name", nargs="?", help="要切换到的方案名；留空仅查看")
    sp.set_defaults(fn=cmd_profile)

    sub.add_parser("doctor", help="检查工具、提示词和项目配置").set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    args.fn(Config(), args)


if __name__ == "__main__":
    main()
