"""0-Token 确定性预检门禁（构建、单测、增量 Lint 插槽、机械红线）与评审包生成。"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from .core import Config, Lane, changed_files, git

BOM = b"\xef\xbb\xbf"
DEFAULT_BOM_EXTS = (".cs", ".cshtml", ".js", ".aspx", ".ascx")
NON_CODE_EXTS = {".md", ".json", ".txt", ".toml", ".yml", ".yaml", ".csv", ".svg", ".lock", ".png", ".jpg", ".ico"}
COMMENT_LINE = re.compile(r"^\s*(?:#|//|/\*|\*|\"\"\"|''')")
MAX_DIFF_CHARS = 60000


def run_shell(cmd: str, cwd: Path, timeout_sec: int = 900) -> tuple[int, str]:
    """按平台执行一条验证或 Lint 命令，返回 (退出码, 合并输出)。"""
    argv = ["powershell", "-NoProfile", "-Command", cmd] if os.name == "nt" else ["sh", "-c", cmd]
    try:
        res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout_sec)
        return res.returncode, (res.stdout + ("\n" + res.stderr if res.stderr else "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout_sec}s）：{cmd}"
    except OSError as exc:
        return 127, f"无法执行命令：{exc}"


def changed_paths(worktree: Path, base: str) -> tuple[list[str], list[str]]:
    """返回 (所有改动文件, 新增文件)，合并 base...HEAD 与工作树未提交改动。"""
    all_set: set[str] = set()
    added: set[str] = set()
    out = git(worktree, "diff", "--name-status", f"{base}...HEAD", check=False)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status, path = parts[0], parts[-1].replace("\\", "/")
            all_set.add(path)
            if status.startswith("A"):
                added.add(path)
    for code, raw in changed_files(worktree):
        path = raw.replace("\\", "/")
        all_set.add(path)
        if code.strip() in ("??", "A"):
            added.add(path)
    return sorted(all_set), sorted(added)


def _format_paths(paths: list[str]) -> str:
    if os.name == "nt":
        return " ".join(f'"{p}"' if any(c in p for c in " ()") else p for p in paths)
    return " ".join(shlex.quote(p) for p in paths)


def _is_excluded_from_line_limit(path: str, test_paths: list[str]) -> bool:
    norm = path.replace("\\", "/")
    if norm.startswith(("docs/", ".lane/")) or Path(norm).suffix.lower() in NON_CODE_EXTS:
        return True
    return any(norm.startswith(tp.replace("\\", "/")) for tp in test_paths)


def _check_bom(worktree: Path, paths: list[str], exts: tuple[str, ...]) -> list[str]:
    errors = []
    for rel in paths:
        p = worktree / rel
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        raw = p.read_bytes()[:6]
        if raw.startswith(BOM + BOM):
            errors.append(f"UTF-8 双 BOM：{rel}")
        elif not raw.startswith(BOM):
            errors.append(f"缺少 UTF-8 BOM：{rel}")
    return errors


def _comment_deletion_warning(worktree: Path, base: str) -> str | None:
    diff = git(worktree, "diff", "-U0", base, check=False)
    added = removed = 0
    in_code = False
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:]
            in_code = not p.startswith(("docs/", ".lane/")) and Path(p).suffix.lower() not in NON_CODE_EXTS
            continue
        if not in_code or line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+") and COMMENT_LINE.match(line[1:]):
            added += 1
        elif line.startswith("-") and COMMENT_LINE.match(line[1:]):
            removed += 1
    net = removed - added
    if net >= 10:
        return f"代码注释/文档串净减少 {net} 行（删除 {removed} / 新增 {added}），请确认非误删已有注释。"
    return None


def _run_verify_and_lint(lane: Lane, proj: dict, existing_changed: list[str], skip_verify: bool) -> tuple[list[str], list[str]]:
    errors, notes = [], []
    verify = proj.get("verify", {})
    base = proj.get("base", "main")
    port = str(verify.get("port_base", 64000) + int(lane.id.split("-")[1]))

    def fill(cmd: str) -> str:
        return (cmd.replace("{port}", port)
                   .replace("{worktree}", str(lane.worktree))
                   .replace("{base}", base)
                   .replace("{changed_files}", _format_paths(existing_changed)))

    verify_chunks: list[str] = []
    if not skip_verify:
        for key, label in (("build_cmd", "构建"), ("unit_cmd", "单元测试")):
            raw = verify.get(key)
            if not raw:
                continue
            cmd = fill(raw)
            rc, out = run_shell(cmd, lane.worktree)
            verify_chunks.append(f"## {label} (`{cmd}`)\nexit_code: {rc}\n```\n{out[-4000:]}\n```\n")
            if rc != 0:
                errors.append(f"{label}失败（exit={rc}）：{cmd}")
            else:
                notes.append(f"{label}通过")
    if verify_chunks:
        (lane.worktree / ".lane" / "verify-result.txt").write_text("\n".join(verify_chunks), encoding="utf-8")

    for raw in verify.get("lint_cmds", []):
        if "{changed_files}" in raw and not existing_changed:
            continue
        cmd = fill(raw)
        rc, out = run_shell(cmd, lane.worktree)
        if rc != 0:
            tail = "\n".join(out.splitlines()[-20:])
            errors.append(f"Lint 失败（exit={rc}）：`{cmd}`\n{tail}")
        else:
            notes.append(f"Lint 通过：`{raw}`")
    return errors, notes


def build_review_pack(cfg: Config, lane: Lane, preflight_md: str = "") -> Path:
    """生成 .lane/review-pack.md：第 1 轮全量，第 2 轮起基于 last_review_sha 生成增量 diff 包。"""
    proj = cfg.project(lane.data["project"])
    base = proj.get("base", "main")
    lane_dir = lane.worktree / ".lane"
    lane_dir.mkdir(exist_ok=True)
    prev_sha = lane.data.get("last_review_sha") or ""
    incremental = bool(prev_sha and subprocess.run(
        ["git", "cat-file", "-e", f"{prev_sha}^{{commit}}"], cwd=lane.worktree, capture_output=True).returncode == 0)
    diff_ref = prev_sha if incremental else base
    mode = "INCREMENTAL" if incremental else "FULL"
    stat = git(lane.worktree, "diff", "--stat", diff_ref, check=False).strip()
    diff = git(lane.worktree, "diff", diff_ref, check=False)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n...（diff 过长已截断，请按需 Read 具体改动文件）\n"

    parts = [f"# 评审上下文包（{mode}）— {lane.id}\n", f"- 对比基线：`{diff_ref}`\n"]
    if not preflight_md and (lane_dir / "preflight.md").exists():
        preflight_md = (lane_dir / "preflight.md").read_text(encoding="utf-8", errors="replace")
    if preflight_md:
        parts.append("## 0-Token 预检结果\n\n" + preflight_md.strip() + "\n")
    if incremental:
        prev_rev = _latest_review_text(lane)
        if prev_rev:
            parts.append("## 上一轮评审意见（重点复核是否已修复）\n\n" + prev_rev.strip() + "\n")
    else:
        for fname, title in (("spec.md", "规格 spec.md"), ("feedback.md", "补充/退回意见 feedback.md")):
            fp = lane.req_dir / fname
            if fp.exists():
                parts.append(f"## {title}\n\n" + fp.read_text(encoding="utf-8", errors="replace").strip() + "\n")
    parts.append(f"## 改动概览 (`git diff --stat {diff_ref}`)\n\n```\n{stat or '(无改动)'}\n```\n")
    parts.append(f"## 改动 Diff (`git diff {diff_ref}`)\n\n```diff\n{diff.strip()}\n```\n")
    pack = lane_dir / "review-pack.md"
    pack.write_text("\n".join(parts), encoding="utf-8")
    return pack


def _latest_review_text(lane: Lane) -> str:
    rev = lane.req_dir / "review.md"
    if rev.exists():
        return rev.read_text(encoding="utf-8", errors="replace")
    hist = sorted((lane.req_dir / "history").glob("*-review.md")) if (lane.req_dir / "history").exists() else []
    return hist[-1].read_text(encoding="utf-8", errors="replace") if hist else ""


def run_preflight(cfg: Config, lane: Lane, skip_verify: bool = False) -> dict:
    """运行全部 0-Token 确定性预检并产出 .lane/preflight.md 与 .lane/review-pack.md。"""
    proj = cfg.project(lane.data["project"])
    verify = proj.get("verify", {})
    base = proj.get("base", "main")
    lane_dir = lane.worktree / ".lane"
    lane_dir.mkdir(exist_ok=True)

    all_changed, added = changed_paths(lane.worktree, base)
    existing = [p for p in all_changed if (lane.worktree / p).is_file()]
    errors, notes = _run_verify_and_lint(lane, proj, existing, skip_verify)
    warnings: list[str] = []

    # 1. 新增非测试代码文件行数上限
    max_lines = int(verify.get("max_new_file_lines", 300))
    test_paths = proj.get("test_paths", [])
    for rel in added:
        p = lane.worktree / rel
        if not p.is_file() or _is_excluded_from_line_limit(rel, test_paths):
            continue
        lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        if lines > max_lines:
            errors.append(f"新增文件超出行数上限（{lines} > {max_lines}）：{rel}")

    # 2. 保护文件检查（未在 spec.md 中明确提及不得改动）
    spec_text = (lane.req_dir / "spec.md").read_text(encoding="utf-8", errors="replace") if (lane.req_dir / "spec.md").exists() else ""
    for prot in verify.get("protected_paths", ["AGENTS.md"]):
        norm = prot.replace("\\", "/")
        if norm in all_changed and norm not in spec_text:
            errors.append(f"修改了未在 spec 中提及的受保护文件：{norm}")

    # 3. 可选 UTF-8 单 BOM 检查
    if verify.get("require_utf8_bom", False):
        exts = tuple(verify.get("bom_extensions", DEFAULT_BOM_EXTS))
        errors.extend(_check_bom(lane.worktree, existing, exts))

    # 4. 注释/文档串异常净删除预警
    warn = _comment_deletion_warning(lane.worktree, base)
    if warn:
        warnings.append(warn)

    status = "FAIL" if errors else "PASS"
    md = [f"RESULT: {status}", "", f"- 改动文件数：{len(all_changed)}（新增 {len(added)}）"]
    if notes:
        md.append("- 已通过项：" + "；".join(notes))
    if errors:
        md.extend(["", "### 必须修复（阻塞）", *[f"- {e}" for e in errors]])
    if warnings:
        md.extend(["", "### 警告（请核实）", *[f"- {w}" for w in warnings]])
    preflight_md = "\n".join(md) + "\n"
    (lane_dir / "preflight.md").write_text(preflight_md, encoding="utf-8")
    pack = build_review_pack(cfg, lane, preflight_md)
    return {"status": status, "errors": errors, "warnings": warnings, "notes": notes,
            "preflight_path": str(lane_dir / "preflight.md"), "pack_path": str(pack)}
