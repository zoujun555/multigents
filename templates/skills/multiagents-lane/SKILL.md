---
name: multiagents-lane
description: >-
  使用 AGY 作为主入口驱动 B+ 多 Agent 研发流水线（独立 worktree 隔离、Gemini 主导规划与编码、
  0-Token 确定性预检与增量 Lint、受控 Claude 轻评审、对话内人工闸门确认、Web 只读看板同步）。
  当用户要求按 multiagents-lane / B+ 流水线开发新需求时激活。
---

# multiagents-lane 工作流（AGY 主控 + 确定性门禁 + Claude 轻评审）

你是这条需求的主控执行 Agent。`multigents` 的 `lane driver` 命令负责确定性底座（worktree、状态看板同步、0-Token 预检门禁、受控 Claude 评审与 Token 记账），Web 页面（`lane ui --readonly`）只做只读监控。

## 前置约定

- **命令入口（零路径配置）**：
  - **在需求 worktree 内运行（推荐）**：`lane driver init` 会在 worktree 的 `.lane/`（已写入 `.git/info/exclude`，不进 Git）自动生成 `.lane/context.json`、`.lane/lane.cmd`（Windows）与 `.lane/lane.sh`（Linux/macOS）。进入 worktree 后，**先读取 `.lane/context.json`** 自动获取当前需求的 `id`、`project`、`tier`，后续命令一律用相对路径调用（记为 `<lane>` = Windows 下 `.lane/lane.cmd`，Linux/macOS 下 `.lane/lane.sh`），无需配置系统 `PATH` 或记忆 `multigents` 绝对路径。
  - **在 `multigents` 仓库根目录运行**（例如执行第 1 步 `init` 建新 worktree 时）：`<lane>` = `.\lane.cmd`（Windows）或 `./lane.sh`（Linux/macOS）。
- **方案与模型配置**：
  - 用 `<lane> profile` 查看各阶段生效的工具、模型与推理深度；
  - 有 Claude 额度用 `<lane> profile claudeplus`（`plan_review` = Opus high，`review` = Sonnet high）；
  - 无 Claude 额度手动切 `<lane> profile agy-only`（`plan_review` = `gemini-4-argon-high`，`review` = `gemini-4-neon-high`）；
  - 也可在 `config/local.toml` 中按 `[stages.<阶段>]` 单独覆写，或在 `<lane> driver review` 时通过 `--agent / --model / --effort` 临时覆写。
- **两档路由**：
  - `standard`（默认）：跨模块/中复杂需求，执行规划轻审 + 代码评审。
  - `light`：简单单点修复/明确小改动，跳过规划评审，仅保留代码评审。
- **独立 Worktree（默认规范）**：每条需求在独立 git worktree 中开发，避免多会话并行冲突。

---

## 执行步骤

### 第 1 步：确认上下文或初始化 Worktree

- **若当前目录已有 `.lane/context.json`**（用户已用 `lane driver init` 建好 worktree 并直接打开它）：读取 `.lane/context.json` 获取 `id`、`project`、`tier`，直接进入第 2 步。
- **否则**，先确认项目名（与 `config/local.toml` 中 `[projects.<name>]` 一致）、标题、需求描述和档位（`standard` 或 `light`），在 `multigents` 目录执行：
  ```bash
  <lane> driver init <project> "<title>" -m "<description>" --tier <standard|light>
  ```
  命令会输出 JSON（含 `id`、`worktree`、`branch`、`req_dir`、`launcher`），并在新 worktree 内自动生成 `.lane/context.json` 与 `.lane/lane.cmd` / `.lane/lane.sh`。
  - **重要**：如果当前 AGY 会话打开的不是返回的 `worktree` 目录，请提示用户直接打开该 `worktree`（IDE 打开文件夹，或终端 `cd <worktree>` 后运行 `agy`）继续，后续所有代码与文档读写都必须限定在该 `worktree` 内。

### 第 2 步：规划起草（AGY 亲自执行）

1. 先读取 `docs/reqs/<id>/request.md` 与项目规则文件，再阅读相关代码。
2. 写出 `docs/reqs/<id>/spec.md`（第一行 `STATUS: READY` 或 `STATUS: BLOCKED`），并**额外包含**：
   - **代码事实锚点**：列出本次改动涉及的文件路径、关键行号和主要调用方；
   - **备选方案与取舍**：说明为什么选当前方案、否决了什么方案（便于高阶模型快速审出架构盲区）。
3. 同步状态并提交快照：
   ```bash
   <lane> driver stage <id> plan running --note "初版规格已起草" --commit
   ```

### 第 3 步：规划轻评审（受控外部评审，`light` 档自动跳过）

1. 执行：
   ```bash
   <lane> driver review <id> --kind plan
   ```
   （需要单次覆写模型/推理深度时，可追加 `--agent ... --model ... --effort ...`）
2. 按返回的 `verdict` 处理：
   - `SKIP`（light 档）或 `PASS`：直接进入第 4 步。
   - `REVISE`：阅读 `docs/reqs/<id>/spec-review.md`，逐条吸收架构与风险意见修订 `spec.md`（单向收敛，**不再调用第二次规划评审**），并执行 `<lane> driver stage <id> plan_review running --note "已按评审意见修订规格" --commit`。

### 第 4 步：规格人工闸门（对话内直接确认）

1. 更新看板为等待确认：
   ```bash
   <lane> driver stage <id> spec_gate waiting --note "规格已就绪，等待对话内确认" --commit
   ```
2. 在当前对话中直接向用户展示 `spec.md` 的「给负责人的摘要」（以及经评审后的关键修订点），**停下来等待用户回复**。
3. 用户回复批准（可附带补充意见）后执行：
   ```bash
   <lane> driver approve <id> -m "<用户附带的意见，无则省略>"
   ```
   若用户要求修改，修订 `spec.md` 后再次确认。

### 第 5 步：编码 + 0-Token 预检 + 代码轻评审闭环

1. 更新状态：`<lane> driver stage <id> code running`
2. 严格按 `spec.md` 与 `feedback.md` 编码、补基础单元测试，并写出 `docs/reqs/<id>/selftest.md`。
3. **强制先跑 0-Token 预检门禁（不唤醒外部评审模型）**：
   ```bash
   <lane> driver preflight <id>
   ```
   - 若退出码非 0（构建/单测/Lint/BOM/300 行/受保护文件未通过）：阅读 `.lane/preflight.md` 输出的报错，**直接本地修复**后重跑 `<lane> driver preflight <id>`，直到返回 `PASS`。
4. 预检通过后提交快照并调用受控代码评审（第 1 轮全量包，第 2 轮起自动增量 diff）：
   ```bash
   <lane> driver stage <id> review running --commit
   <lane> driver review <id> --kind code
   ```
5. 按返回 `verdict` 处理：
   - `PASS`：进入第 6 步。
   - `FAIL`：阅读 `docs/reqs/<id>/review.md`，修复全部「必须修复」项 → 重跑 `<lane> driver preflight <id>`（至 `PASS`）→ 再次提交并调 `<lane> driver review <id> --kind code`（最多 `max_rounds` 轮，超限则标 `blocked` 请用户裁决）。

### 第 6 步：独立测试设计与集成运行

1. **测试设计（`test`）**：更新 `<lane> driver stage <id> test running`。**强烈建议使用 AGY 原生 `invoke_subagent`（干净上下文）**，仅基于 `spec.md` 与当前代码编写完整测试并产出 `docs/reqs/<id>/test-design.md`，避免“自己写代码自己写测试”的盲区。完成后 `--commit`。
2. **集成测试（`itest`）**：若项目存在共享资源（测试数据库 / CoverageHub），先加锁再执行，结束后无论成败必须解锁：
   ```bash
   <lane> driver lock itest <id>
   <lane> driver stage <id> itest running
   # 运行集成测试并写出 docs/reqs/<id>/test-report.md
   <lane> driver unlock itest <id>
   ```
   - 若 `test-report.md` 为 `FAIL_PRODUCT`：回到第 5 步修复产品代码并走预检 + 增量评审。

### 第 7 步：汇总与合并闸门

1. 更新 `<lane> driver stage <id> report running`，写出 `docs/reqs/<id>/summary.md`（0 Claude Token）。
2. 更新 `<lane> driver stage <id> merge_gate waiting --note "汇总已生成，等待确认" --commit`，在对话内向用户展示结论与合并命令并等待确认。
3. 用户确认完成后执行：
   ```bash
   <lane> driver finish <id>
   ```
   （需要同时清理 worktree 并归档时加 `--clean`；本工具不替用户执行 `git merge`。）
