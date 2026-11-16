# 多 Agent 工作台（lane）

给 Vibe Coding 项目补测试、做质量把关。每条需求走一条固定流水线，不同阶段交给不同的工具，你只在两个地方把关。多条需求可以并行。

```
                你批准规格                                                          你确认合并
                    │                                                                  │
需求 → 规划 ───→ [闸门1] → 编码 → 评审 ─通过→ 测试设计 → 集成测试 ─通过→ 汇总 → [闸门2] → 完成
     Claude Opus          agy   Claude Sonnet    agy       agy（排队）      Claude
                           ▲       │不通过        ▲          │
                           │       │              └─测试问题─┤
                           └─ 修复 ←┴────────产品缺陷─────────┘
                              agy        （各种退回合计最多 2 轮，超过就停下或直接汇总）

并行：规划、编码、评审、修复、测试设计、汇总        排队（一次一个）：集成测试

Jenny（随调度器运行）：每轮检查所有需求 → 新出现的问题追加到告警历史（state/alerts.jsonl）并通知
```

## 设计要点

| 问题 | 做法 |
|---|---|
| 阶段之间怎么交接 | 只靠文件。每条需求在自己的分支里有 `docs/reqs/REQ-xxx/`，各阶段读上一阶段的文件、写自己的文件。每个阶段都是全新的无头会话，不继承上下文。 |
| 工作台怎么知道下一步 | 结果文件第一行的标记：`selftest.md`、`test-design.md`、`test-report.md` 写 `RESULT: ...`，`review.md` 写 `VERDICT: PASS/FAIL`。 |
| 多条需求怎么隔离 | 每条需求一个分支 `lane/REQ-xxx` 加一个 Git worktree（在 `worktrees/<项目>/REQ-xxx`），互不影响，弄乱了删掉重建。 |
| 并行和预算 | `max_parallel` 限制同时运行的阶段总数；每个工具有自己的并发上限和每日启动次数上限。到上限的需求排队，不算卡住。 |
| 评审独立性 | 评审用的是另一家模型的新会话；它改了产品代码会被工作台自动撤销并记录。测试设计和集成测试阶段改了测试目录以外的文件会停下等你。 |
| 无人可问时 | 提示词要求 Agent 不提问，把问题写进结果文件并标 BLOCKED。超时会被终止。两种情况都由 Jenny 上报。 |
| 谁提交、谁合并 | 工作台在每个阶段结束后提交到需求分支。Agent 不提交、不推送。合并到主干始终由你手动做。 |
| Jenny | 纯脚本，不耗额度。上报：卡住、超时、调度器停了（没有心跳）、等你确认太久、进入最后一轮修复、额度将满。可选让 Claude Haiku 读日志写 `diagnosis.md`。 |

## 目录

```
config/rules/      每个项目的规则文件（由 lane rules 从模板创建，你来编辑）
templates/AGENTS.md  规则模板
config/lane.toml   全部配置：并发、预算、每个阶段用哪个工具和模型、项目登记
prompts/           每个阶段的提示词（plan / code / fix / review / test / report / diagnose）
lane/              工具本身（Python 标准库，无第三方依赖）
tests/run_demo.py  用假 Agent 跑完整流程的自检，不耗额度
tests/run_ui_check.py  网页界面的自检（登录、接口、操作、配置校验），不耗额度
lane/ui/           网页界面（服务、接口、页面和中英文资源）
config/server.toml 网页界面的地址、端口、用户名和密码（首次运行 lane ui 时生成）
state/             运行状态和日志（自动生成）
worktrees/         各需求的工作树（自动生成）
state/alerts.jsonl 告警历史（自动生成，所有项目一份，每条带项目字段；在看板的"告警历史"里查看和按项目筛选）
```

## 开始使用

1. **登记项目**：在 `config/lane.toml` 末尾照示例加一段 `[projects.xxx]`。
2. **项目规则**：`.\lane.cmd rules <项目>` 把 `templates/AGENTS.md` 复制成 `config/rules/<项目>.md`，按项目情况编辑（`--from <文件>` 可以改用别的起点，比如项目自己的 AGENTS.md）。编码、修复、评审、测试四个阶段的提示词会带上它。规则里"先给思路等确认""停下提问"这类要求，在流水线里统一变成"写进结果文件并标 BLOCKED"。
3. **本地文件**：工作树只包含已提交的内容。被 git 忽略但运行需要的文件（如 `.agents/`、测试用的 `.env`）写进项目的 `copy_files`，建需求时会复制过去。复制的是建需求那一刻的内容。
4. **检查**：`.\lane.cmd doctor`，全部 OK 再继续。
5. **如果测试要走 CoverageHub**：在目标仓库里把 skill 包解压到 `.agents\skills\`，并给 agy 配好 `coveragehub` 的 MCP 连接（`agy mcp add`）。然后在项目配置里填 `coveragehub_project`。不填则只跑项目自己的自动化测试。

日常：

```powershell
.\lane.cmd new hronetime "加班申请增加附件" -m "需求描述……"   # 或 -f 需求.md
.\lane.cmd run                 # 开一个终端一直挂着：调度 + Jenny
.\lane.cmd status              # 在终端里看全部需求和当前告警（网页看板上是同样的内容）
.\lane.cmd approve REQ-001     # 批准规格 / 确认汇总
.\lane.cmd reject REQ-001 -m "意见"   # 退回规格重写 / 退回去修复
.\lane.cmd resume REQ-001 --stage code   # 卡住的需求处理完后继续
.\lane.cmd log REQ-001         # 经过和最近的日志
.\lane.cmd open REQ-001        # 文件在哪；怎么自己和 Opus 交互讨论规格
.\lane.cmd clean REQ-001       # 合并后移除工作树
```

用 PowerShell 或 cmd 运行。Git Bash 里 `lane.cmd` 会因为路径中的括号报错，改用 `PYTHONPATH=. python -m lane ...`。

## 网页界面

命令行能做的事，网页上也能做，两者操作的是同一份状态。

```powershell
.\lane.cmd ui
```

启动后在浏览器打开 `http://127.0.0.1:8770/`。第一次启动会创建 `config/server.toml`，用户名是 `chris`，密码随机生成，**打开这个文件就能看到**，也可以直接改。

| 页面 | 内容 | 能做的事 |
|---|---|---|
| 看板 | 每条需求所处的阶段、需要你处理的事项、当前告警、告警历史（可按项目筛选）、今日用量和预算、调度器状态 | 启动和停止调度器、新建需求 |
| 需求 | 各阶段产出的文件（规格、评审、测试报告、汇总等）、经过和耗时、最近的日志 | 批准、带意见退回、从指定阶段继续、清理或放弃 |
| 统计 | 各阶段运行次数和耗时、评审一次通过率、平均修复轮次、卡住的原因、每天各工具的会话数 | 按项目和创建日期筛选 |
| 配置 | `lane.toml`、各项目的规则文件、规则模板 | 文本编辑并保存；新建项目规则 |

- **语言**：右上角切换中文和英文。界面文字在 `lane/ui/static/i18n/zh.json` 和 `en.json` 里。后端返回的提示和需求的说明目前只有中文。
- **配置保存**：`lane.toml` 保存前会校验，格式不对或缺少必要内容时不保存并给出原因；上一版保留为同目录的 `.bak`。`server.toml` 不能在网页上查看或修改。
- **远程访问**：默认监听 `0.0.0.0`，其他机器用 `http://<这台机器>:8770/` 打开；连不上时在 Windows 防火墙放行 8770 端口。只想本机使用，把 `server.toml` 里的 `host` 改成 `127.0.0.1`。同一地址连续输错 5 次密码会被锁定 5 分钟。
- **安全**：这是明文 HTTP，登录后能启动 Agent、能修改它们可以执行的命令。只在可信的网络里开放；要经公网访问，放在 HTTPS 反向代理后面。
- 在网页上启动的调度器在后台运行，输出写到 `state/scheduler.log`。同一时间只能有一个调度器，命令行的 `lane run` 和网页上的"启动"互斥。

## 每个项目怎么构建和验证

每条需求都要用**自己工作树里构建出来的版本**来验证，而不是机器上已经部署的版本。每个项目在 `config/lane.toml` 里用 `[projects.<名称>.verify]` 声明做法，各类项目回答同样的几个问题，用不到的留空：

| 配置项 | 回答的问题 |
|---|---|
| `setup_cmd` | 新工作树里怎么还原依赖 |
| `build_cmd` | 怎么构建 |
| `unit_cmd` | 怎么跑单元测试 |
| `serve_cmd` | 怎么从工作树启动被测应用 |
| `ready_url` | 怎么判断应用已就绪 |
| `ta_cmd` | 怎么跑自动化测试 |
| `[...verify.env]` | 自动化测试需要哪些环境变量才能找到这个应用 |
| `port_base` | 端口起点。`{port}` = `port_base` + 需求编号，每条需求一个端口 |

命令里可以用 `{port}` 和 `{worktree}`。配置文件里有 Node 项目和 HROneTime（.NET Framework + IIS Express）两个例子。

目前这些命令由 Agent 执行：编码、修复、评审阶段拿到还原、构建、单元测试；测试设计阶段只拿到还原和构建；集成测试阶段拿到全部步骤，包括启动应用、设置环境变量、跑自动化测试、结束后停掉应用。工作台自己不执行它们，所以结果仍以 Agent 写的结果文件为准。以后可以扩展为由工作台执行并留下证据。

自测失败（`selftest.md` 写 `RESULT: FAIL`）不再直接卡住，而是退回修复，并算作一轮；轮次用完才停下等你。

## agy 的命令权限

agy 在无头模式下不能询问权限。不在允许列表里的命令会被拒绝，而且**一条命令被拒，整个会话就立刻结束**，没有任何输出。所以每个项目要列出 agy 可以执行的命令：

```toml
[projects.myapp]
allow_commands = [
    'npm (ci|test|run build)',     # 正则，从命令开头匹配；点号写成 [.]
    'npx playwright test.*',
]
```

- 工作台把这些规则写成 `command(regex:...)`，通过 agy 的 `--settings` 只对本次会话生效，不改 agy 的全局配置。你在 agy 里已有的全局允许列表（git、python、dir 等）照常生效，两者合并。
- 所有项目通用的规则可以写在 `[agents.agy]` 的 `allow_commands` 里。
- 允许的命令会列在 agy 各阶段的提示词里，并告诉它不要尝试列表之外的命令，需要时写 BLOCKED。
- 命令被拒时，需求会卡住，说明里会提示去 `allow_commands` 补规则。agy 不报告被拒的是哪条命令，要到 `lane log <编号>` 的日志或 agy 自己的日志里找线索。
- 这套规则只管 agy。Claude Code 的阶段（规划、评审、汇总）目前只读文件和写结果文件，没有配置命令权限。

沙箱模式（`--sandbox`）实测过：命令不能写文件（包括工作树内）、不能联网，无法用来构建，所以没有采用。

## 测试设计和集成测试分开，集成测试排队

测试分成两个阶段：

| 阶段 | 做什么 | 占用共享资源 | 执行方式 | 结果文件 |
|---|---|---|---|---|
| 测试设计（`test`） | 设计并编写自动化测试，保证能编译。不启动应用，不连测试数据库 | 否 | 并行 | `test-design.md`：`RESULT: PASS / BLOCKED` |
| 集成测试（`itest`） | 从工作树构建并启动应用，运行自动化测试，对接 CoverageHub，分析失败原因 | 是 | 排队，一次一个（`[stages.itest] max_parallel = 1`） | `test-report.md`：`RESULT: PASS / FAIL_PRODUCT / FAIL_TEST / BLOCKED` |

这样排队名额只在测试真正运行的那段时间被占用，写测试的时间不占队列。排队中的需求状态显示为"排队"，不会被当成卡住。任何阶段都可以用 `max_parallel` 设自己的并发上限。

集成测试失败后的去向由结果标记决定：

- `FAIL_PRODUCT`（产品缺陷）：退回修复，之后重新评审、确认测试、再排队运行。
- `FAIL_TEST`（测试本身有问题）：退回测试设计，改好后再排队运行。
- 两种退回都算一轮。轮次用完后不再退回，直接进入汇总，由你在合并闸门决定。

集成测试目前由一个专门的 Agent 会话执行，不是编码的那个会话。配了 `coveragehub_project` 的项目，CoverageHub 的对接也在这个排队的阶段里做。

## 已验证和未验证

已验证：

- `python tests/run_demo.py` 用假 Agent 跑通：三条需求并行、项目规则进入提示词、本地文件复制进工作树、退回规格、评审不通过后修复再通过、评审者越界改动被撤销、超时终止、Jenny 上报、确认和清理。
- 本机 agy 1.5.7 的无头调用方式可用：`agy -p "<指向提示词文件的一句话>" --mode accept-edits --print-timeout <秒>s` 能读提示词文件并写文件。这个账号不支持 `--mode auto`。
- 本机 Claude Code 2.1.325 的无头调用方式可用：`claude -p "<同上>" --model sonnet --permission-mode acceptEdits --output-format text`。

未验证：

- **HROneTime 的允许规则是否覆盖 agy 实际发出的命令**：规则机制本身已用真实 agy 验证（放行了 NuGet，全局列表照常生效），但 agy 写命令的方式可能和规则不一致，要在试跑中调整。
- **agy 通过 MCP 使用 CoverageHub 的 skills**：提示词按 CoverageHub 的 HOWTOUSE 写的，没有实跑。
- **真实模型是否稳定地按格式写标记行**：写错时需求会卡住并上报，不会走错方向。

## 还没做的

- **把工作树的构建部署到被测站点**：UI 和 API 测试对着 `.env` 里的站点地址跑。工作台不做部署，测到的是站点当前部署的版本，不一定是需求分支的代码。

- **权限隔离**：现在 agy 以你的 Windows 账号运行，能碰到你能碰到的一切。按之前的方案，下一步是建专用低权限账号、只给 worktree 目录读写权限。在此之前不要用 `--dangerously-skip-permissions`。
- **GitHub 同步**：状态只在本机。本机也没装 `gh`。
- **版本批量测试**（几条需求合入 RC 后统一测，再拆分覆盖率）。

## 参考的开源项目

2026-11-16 核对过各项目的 README。结论是不整套采用任何一个，只借用做法：

| 项目 | 借用了什么 | 为什么不直接用 |
|---|---|---|
| [mraza007/baton](https://github.com/mraza007/baton) | 轮询、每个任务一个 worktree、`claude -p` 调用、并发上限 | 只有 3 次提交，只支持 Claude Code |
| [andyyaro/orkestra](https://github.com/andyyaro/orkestra) | 工具适配器、跨模型评审后才能合并、用假适配器测试流程 | README 写明 Windows 未测试；项目很新 |
| [github/spec-kit](https://github.com/github/spec-kit) | 规格的章节结构 | 可以叠加使用：想要更细的规格流程时，在目标仓库里装它，让规划阶段产出它的格式。它的 README 没提 agy，是否支持要查它的 integrations 文档 |
| [kdlbs/kandev](https://github.com/kdlbs/kandev) | 没有借用 | 功能最接近（每步换工具的流水线，原生支持 Windows，agy 通过 ACP 接入），但它是带界面的整套系统，AGPL 许可证，agy 的 ACP 服务要手动安装。以后想要看板界面时值得试 |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 没有借用 | README 开头写明项目正在停止维护 |
