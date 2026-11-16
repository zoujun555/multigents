"""对需求的操作：创建、批准、退回、继续、清理。命令行和网页界面共用。

每个函数返回给人看的结果文字；不能执行时抛 SystemExit（带原因）。
"""
from __future__ import annotations

from pathlib import Path

from .core import (AUTO_STAGES, STAGE_LABEL, Config, Lane, commit_all, copy_local_files, create_worktree, git,
                   load_lane, next_id, now)


def new(cfg: Config, project: str, title: str, text: str = "") -> str:
    proj = cfg.project(project)
    repo = Path(proj["repo"])
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} 不是 Git 仓库")
    if not title.strip():
        raise SystemExit("标题不能为空")
    lane_id = next_id(cfg)
    branch = cfg.get("workbench", "branch_prefix", "lane/") + lane_id
    worktree = cfg.worktree_root() / project / lane_id
    create_worktree(repo, worktree, branch, proj.get("base", "main"))
    copied = copy_local_files(repo, worktree, proj.get("copy_files", []))
    lane = Lane(cfg, {
        "id": lane_id, "project": project, "title": title, "branch": branch,
        "worktree": str(worktree), "stage": "plan", "status": "queued", "round": 0,
        "note": "", "created": now(), "since": now(), "history": [],
    })
    lane.req_dir.mkdir(parents=True, exist_ok=True)
    (lane.req_dir / "request.md").write_text(f"# {lane_id} {title}\n\n{text or title}\n", encoding="utf-8")
    commit_all(worktree, f"{lane_id}: request")
    lane.save()
    lines = [f"已创建 {lane_id}，分支 {branch}，工作树 {worktree}"]
    if copied:
        lines.append("已复制本地文件：" + "、".join(copied))
    lines.append("运行 lane run 后会自动起草规格；想自己和 Opus 讨论规格，见 lane open " + lane_id)
    return "\n".join(lines)


def approve(cfg: Config, lane_id: str) -> str:
    lane = load_lane(cfg, lane_id)
    if lane.data["status"] != "waiting":
        raise SystemExit(f"{lane.id} 现在不在等待确认（{STAGE_LABEL[lane.data['stage']]}）")
    if lane.data["stage"] == "spec_gate":
        commit_all(lane.worktree, f"{lane.id}: spec approved")  # 你可能手改过 spec.md
        lane.event(stage="spec_gate", result="approved")
        lane.set(stage="code", status="queued", note="")
        return f"{lane.id} 规格已批准，进入编码队列"
    lane.event(stage="merge_gate", result="approved")
    lane.set(stage="done", status="done", note="")
    proj = cfg.project(lane.data["project"])
    return (f"{lane.id} 已确认完成。本工具不会替你合并，分支在 {lane.data['branch']}：\n"
            f"  git -C \"{proj['repo']}\" merge --no-ff {lane.data['branch']}\n"
            f"合并后清理：lane clean {lane.id}")


def reject(cfg: Config, lane_id: str, message: str) -> str:
    lane = load_lane(cfg, lane_id)
    if lane.data["status"] != "waiting":
        raise SystemExit(f"{lane.id} 现在不在等待确认")
    if not message.strip():
        raise SystemExit("退回必须写明意见")
    with open(lane.req_dir / "feedback.md", "a", encoding="utf-8") as f:
        f.write(f"\n## {now().replace('T', ' ')}（{STAGE_LABEL[lane.data['stage']]}时退回）\n\n{message}\n")
    if lane.data["stage"] == "spec_gate":
        nxt = "plan"
    else:
        nxt, lane.data["round"] = "fix", 0  # 人工退回后重新计轮次
        (lane.req_dir / "review.md").write_text(f"VERDICT: FAIL\n\n# 人工退回\n\n{message}\n", encoding="utf-8")
    commit_all(lane.worktree, f"{lane.id}: rejected at {lane.data['stage']}")
    lane.event(stage=lane.data["stage"], result="rejected", note=message)
    lane.set(stage=nxt, status="queued", note="")
    return f"{lane.id} 已退回到{STAGE_LABEL[nxt]}，意见记在 feedback.md"


def resume(cfg: Config, lane_id: str, stage: str | None = None, reset_rounds: bool = False) -> str:
    lane = load_lane(cfg, lane_id)
    if lane.data["status"] == "running":
        raise SystemExit(f"{lane.id} 正在运行")
    stage = stage or lane.data["stage"]
    if stage not in AUTO_STAGES:
        raise SystemExit(f"只能从这些阶段继续：{', '.join(AUTO_STAGES)}")
    if reset_rounds:
        lane.data["round"] = 0
    lane.event(stage=stage, result="resumed")
    lane.set(stage=stage, status="queued", note="")
    return f"{lane.id} 已重新排队，从{STAGE_LABEL[stage]}继续"


def clean(cfg: Config, lane_id: str, force: bool = False) -> str:
    lane = load_lane(cfg, lane_id)
    if lane.data["status"] == "running":
        raise SystemExit(f"{lane.id} 正在运行")
    if lane.data["status"] != "done" and not force:
        raise SystemExit(f"{lane.id} 还没完成。确实要放弃它，加 --force（分支会保留）")
    repo = Path(cfg.project(lane.data["project"])["repo"])
    git(repo, "worktree", "remove", "--force", str(lane.worktree), check=False)
    archive = cfg.state_dir / "archive"
    archive.mkdir(exist_ok=True)
    lane.path.replace(archive / lane.path.name)
    return f"已移除 {lane.id} 的工作树并归档状态。分支 {lane.data['branch']} 保留，需要时自己删除。"
