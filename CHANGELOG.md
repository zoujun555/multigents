# 变更记录

## 2026-11-20 B+ 核心闭环：AGY 入口、0-Token 预检门禁、Claude 轻评审与 Web 只读监控

改动：

- **Token 精确记账（`lane/tokens.py`）**：解析 Claude 与 `agy` 的 JSON 日志（`input_tokens`、`cache_creation_tokens`、`cache_read_tokens`、`output_tokens`、`thinking_tokens`、`cost_usd`），追加写入 `state/tokens.json`，并在网页统计页（`lane/ui/stats.py` + `pages.js`）展示聚合明细；原有 `state/usage.json` 保持向后兼容。
- **0-Token 确定性预检与评审打包（`lane/preflight.py`）**：在调用 Claude 评审之前，由工作台实跑项目 `build_cmd` / `unit_cmd`、按需执行配置化增量 Lint 插槽（`verify.lint_cmds`，支持 `{changed_files}`、`{base}`、`{worktree}`）、检查新增非测试文件行数上限、受保护文件（默认 `AGENTS.md`）、可选单 UTF-8 BOM（`require_utf8_bom`）以及注释大面积净删除预警；同时生成 `.lane/review-pack.md`（第 1 轮全量，第 2 轮起基于 `last_review_sha` 生成增量 diff 包）。
- **AGY 主控原子命令与零路径引导器（`lane/driver.py`、`lane/cli.py`）**：新增 `lane driver <init|stage|preflight|review|approve|finish|lock|unlock>`（已统一为通用 `review` 并移除 `claude-review`），默认创建独立 `worktree`，并在 `.lane/`（已加入 `.git/info/exclude`，不进 Git）自动生成 `context.json`、`lane.cmd` 与 `lane.sh` 引导器，使 worktree 内无需配置系统 `PATH` 或写死 `multigents` 绝对路径即可调用并同步状态；支持 `standard` / `light` 两档与单次 `--agent/--model/--effort` 覆写，提供跨会话共享资源锁（如 `itest`）；原有 `lane run` 无头调度器完整保留。
- **按阶段自定义模型与方案预设（`lane/core.py`、`config/lane.toml`、`lane profile`）**：将 `default_mode` 下沉至 `[agents.claude]` / `[agents.agy]` 以消除跨工具切换时的模式不匹配；新增 `claudeplus`（`plan_review`=Opus `high`、`review`=Sonnet `high`）与 `agy-only`（`plan_review`=`gemini-4-argon-high`、`review`=`gemini-4-neon-high`）两套方案预设，合并优先级为 `lane.toml` → `profile` → `local.toml [stages.*]` → CLI 参数；新增 `lane profile [name]` 查看生效配置与手动切换，并在 Web 看板头部展示当前激活方案。
- **Claude 轻评审与配置瘦身（`config/lane.toml`、`lane/agents.py`、`prompts/plan_review.md`、`prompts/review.md`）**：Claude 切换 `--output-format json`，支持阶段级 `effort` 与 `--max-turns` 覆盖；新增 `[stages.plan_review]`（轻审规划，只提问题与改造方向）；`review` 去掉无头不可执行的构建命令段、优先读取预打包上下文与增量 diff；`report` 与默认 `diagnose` 迁给 `agy`；无头规则禁止写完结果文件后在 stdout 重复输出双语长摘要。
- **AGY 入口 Skill 模板（`templates/skills/multiagents-lane/SKILL.md`）**：给出在 Antigravity 会话内驱动 B+ 流水线、对话内完成 `spec_gate` / `merge_gate` 确认、并用原生 subagent 隔离 `test` 阶段的标准流程。
- **Web 只读监控模式与新建 AGY 需求（`lane/ui/server.py`、`api.py`、`auth.py`、前端与多语言）**：新增 `lane ui --readonly` 与 `config/server.toml` 的 `readonly = true` 配置（只读模式下后端对除登录/登出外的 `POST` 写接口返回 HTTP `403`，前端隐藏写控件并显示多语言监控标识）；非只读模式下，网页「新建需求」弹窗新增「执行方式」选项（`AGY 驱动 standard` / `AGY 驱动 light` / `旧调度器队列`，API 缺省保持旧队列兼容），选 AGY 驱动时调用 `driver.init_lane()` 生成 `.lane/context.json` 与 `.lane/lane.cmd`，并在弹窗内展示可一键复制的 `worktree` 路径。
- **自检与 CI（`tests/test_bplus_driver.py` 等）**：新增 B+ 核心闭环测试（Token 解析、预检门禁 + `lint_cmds`、两档轻评审与增量二轮复审、资源锁、Web 只读 403），同步更新 `check_agent_commands.py`、`run_demo.py`、`run_ui_check.py` 与 `.github/workflows/tests.yml`。

经验：

- Windows 用户名或路径里带括号（如 `Users\Name(Tag)`）时，传给 PowerShell 的路径必须加引号（并用 `& "..."` 调用可执行文件），否则 PowerShell 会把括号当子表达式解析报错；`_format_paths` 因此同时对空格和括号加引号。
- 把 `readonly` 放在 `/api/board`、`/api/lane`、`/api/config` 而非 `/api/session`，既满足前端判断，又保持已有 `/api/session` 契约完全兼容。


## 2026-11-17 支持在 Linux / macOS 上运行

改动：

- 新增 `lane.sh`（Linux / macOS 的启动脚本，对应 Windows 的 `lane.cmd`）和 `.gitattributes`（脚本强制 LF 换行、`.cmd` 用 CRLF，避免 Linux 上报 "bad interpreter"）。启动脚本不能叫 `lane`，因为 `lane/` 是包的文件夹。
- Python 版本检查：低于 3.11 时直接说明，不再报看不懂的 `tomllib` 导入错误（Ubuntu 22.04 自带 3.10）。检查放在 `lane/__init__.py`，所有入口都会经过。
- 测试里去掉 Windows 专属写法：网页检查收尾改用工作台自己的 `kill_tree`；仓库检查也认 `/home/<名字>`、`/Users/<名字>` 这样的家目录路径。
- 新增跨平台的进程测试：启动一个带子进程的进程，结束整棵进程树，确认父子都没了。Windows 上走 `taskkill`，Linux 上走进程组。
- README 增加 Linux 一节：安装要求、命令、防火墙、通知、systemd 用户服务的例子；`local.toml.example` 增加 Linux 上 Node 项目的例子。
- 新增 `.github/workflows/tests.yml`：在 ubuntu-latest 和 windows-latest 上各跑全部自检（用 claude / agy 的替身）。

没有验证的：

- 在开发机（Windows）上没有 Linux 或 WSL，Linux 部分没有真正运行过。进程树测试在 Windows 上通过，但 Linux 的分支（`os.killpg`）要等工作流或你的 Linux 机器来验证。
- 工作流文件本身没有运行过，可能有第一次运行才会暴露的小问题（比如 Windows 上的 claude 替身）。

经验：

- 启动脚本的名字不能和包文件夹同名，写完才发现；同样，写 Linux 脚本前先确认这台机器写出的换行符，用 `.gitattributes` 让仓库里始终是 LF。
- 用 `grep -c` 数 `$''` 在这个环境里被当成了字母 r，数字全是错的；检查行尾要用 Python 数字节。


## 2026-11-17 自检提速；阶段之间的空档

改动：

- 新增 `tests/unit_tests.py`：28 个单元测试，约 7 秒。故意去掉心跳修复后，对应的测试会失败。
- 完整流水线自检从 145 秒降到约 41 秒：假 Agent 的延迟可配置（`LANE_FAKE_DELAY`），自检里设 0.1 秒。
- 修复两处真实的空档：`run_agent` 每 3 秒才检查一次进程，现在每 0.2 秒检查一次（心跳仍然每 3 秒写一次）；调度器每轮最多睡 15 秒，现在阶段一结束就被唤醒，下一个阶段马上开始。真实需求的每两个阶段之间原来最多白等 15 秒。

经验：

- 测试慢，往往是因为里面写死了等待，而不是逻辑复杂。先数一数等了多久，再决定怎么提速。
- 同一个写死的等待也出现在真实代码里，所以提速测试顺带修了真实使用中的延迟。
- 每改一处就把全部检查跑一遍是浪费。按改动的范围选检查。


## 2026-11-16 通知、标签页数量、调度器日志

改动：

- 新模块 `lane/notify.py`：需求到了闸门（等待批准规格、等待确认合并）时发通知，可选地每个阶段完成都通知（`[jenny] notify_gates` 默认开、`notify_stage_done` 默认关）。Jenny 的告警也走同一个发送函数。
- 通知消息放在环境变量 `LANE_MESSAGE`，不再拼进命令行里。原来的写法把消息文字直接替换进 PowerShell 命令字符串，消息里有一个引号就会让命令出错，甚至被当作命令执行；`{message}` 占位符还保留，但文档里说明不要写进 shell 字符串。
- 网页标签页标题显示"需要你处理"的数量，每 8 秒更新。
- 调度器日志不再被缓冲：从网页启动时用 `python -u`。之前 `state/scheduler.log` 长时间是 0 字节。
- 修复：`lane run --once` 在某个工具被短暂暂停（额度用完后的等待）时会提前退出，因为它把"暂停"当成"额度用完"。暂停的需求现在留在队列里等恢复。

经验：

- 把文字替换进要被 shell 解释的命令里，等于允许消息内容写代码。凡是传给外部命令的文字，用环境变量或标准输入，不要拼进命令行。
- 自检失败时临时目录没有清理，跑几十次就留下几十个。测试应该放在 `try/finally` 里清理。
- 自检因为演示场景越加越多而偶发失败：调度器的暂停和 `--once` 退出条件互相影响，只在某种时间顺序下才出现，所以多跑几次才发现。


## 2026-11-16 修复"没有心跳"误报

现象：需求在闸门等了一段时间之后再被派出去，Jenny 立刻报"某某阶段已 N 小时没有心跳，调度器可能已停止"，但那个阶段随后正常跑完。

原因：调度器派出阶段时先把状态写成"运行中"，新的心跳要等阶段线程启动之后才写。Jenny 紧接着在同一轮检查，看到的是上一次运行留下的旧心跳（REQ-003 是 2.7 小时前，REQ-004 是 10 分钟前），就误报了。两条告警的时间都和阶段开始的时间在同一秒。

改动：派出阶段时和"运行中"状态一起写下新的心跳（`runner.mark_running`）。

验证：流水线自检新增回归检查——一条心跳是 3 小时前的需求被派出后，不再产生"没有心跳"告警；去掉修复后这个检查会失败。

注意：

- 已经记进告警历史的几条误报不会自动消失，它们只是历史。
- 正在运行的调度器还在用旧代码，重启一次调度器才会生效。重启会终止正在运行的阶段，它们在下次启动时重新排队。
- 真正的"没有心跳"（调度器进程死了）仍然会被报出：运行中的阶段每 3 秒写一次心跳，超过 `[jenny] stale_minutes`（默认 10 分钟）没有更新才报。


## 2026-11-17 规划提示词再收紧三处

改动（`prompts/plan.md`）：

- 「现状」最多五行，只写理解设计决策所必需的，不逐个文件介绍。
- 「风险和待确认问题」只写摘要里没有的内容，不重复摘要。
- 摘要的「需要你过目的」增加一类：这次改动很可能违反的项目规则（如文件行数上限）。规划阶段要把选择和建议摆出来，而不是留给编码者事后报告。

经验（REQ-004 的规格）：

- 改成"决策、契约、验收标准"之后，规格的核心部分写得好，但「现状」写成了 10 行的文件导览，「风险」重复了摘要里的三条。
- runner.py 已 297 行，改动后很可能超过 300 行；规格只要求编码者"不重构、写进自测"，等于把决定推迟到编码之后。这类规则冲突应在规格闸门由人决定。

## 2026-11-17 规格只写决策、契约和验收标准

改动：

- 规划提示词（`prompts/plan.md`）重写：规格开头加一屏以内的「给负责人的摘要」（改完之后、关键决策、不做的、需要你过目的、建议拆分），负责人只读这一部分就能批准或退回。
- 规格正文改为"设计决策和契约"，去掉"实现步骤"一节。新增判断规则：编码者换一种做法评审者会判不合格的才写；逐文件修改清单、可按规则生成的完整清单、非契约的代码片段、修改顺序都不写。给了长度指引（小改动约 80 行，一般约 200 行），超过时建议拆分。
- 编码提示词：规格写明的决策和契约必须照做，认为有错时写 BLOCKED 而不是自行改掉；规格没规定的由编码者决定，并在自测报告里列出主要的实现选择。
- 评审提示词：核对决策和契约是否照做、按规则补全的清单是否齐全；对规格没规定的实现选择，只按正确性、仓库做法和简单程度来评；认为规格的决策有错时写进"建议"交给负责人。

经验（REQ-003 的规格约 400 行）：

- 规格越详细，批准闸门越形同虚设：负责人审不动，最需要人判断的假设被排在最后。
- 规划阶段没有运行过的逐行改法和字面字符串容易出错，编码者会照抄，评审者还得判断"偏离了一条错误指令"算不算缺陷。
- 规格给出确切代码并不能挡住缺陷：REQ-002 第一轮评审不通过的三处都是规格没提到的，靠的是评审阶段。
- 完整的翻译键清单这类内容由一条验收标准和检查脚本保证齐全即可，写进规格只增加长度。

未验证：新提示词还没有用真实需求跑过，规格实际会变短多少、编码质量是否受影响，要看下一条需求。

## 2026-11-16 审规格：编辑规格、带意见批准

改动：

- 等待批准规格时，规格标签页有"编辑规格"：直接改 `spec.md`，上一版存到 `history/NN-spec.md`，空的或超过 200 KB 的不保存，没有变化不产生历史版本。批准之后不能再改。接口 `POST /api/lane/spec`。
- 批准规格时可以附带意见：写进 `feedback.md`，小标题固定为"批准规格时附带的意见"。命令行 `lane approve ID -m "意见"` 也支持；确认合并时带意见会被拒绝。
- 编码、修复、评审的提示词会读这段意见：编码和修复照做，评审检查有没有照做，没照做算必须修复。意见与规格冲突时以规格为准，并在自测里指出。
- 需求的"经过"里多了"已编辑"。

经验：

- 之前编码阶段不读 `feedback.md`，所以即使允许"带意见批准"，意见也会被忽略。加输入框之前先确认下游阶段真的会读。
- 退回的意见和批准时的意见都存在 `feedback.md` 里，靠小标题区分；提示词里写明"只看标题含……的部分"，免得 Agent 把早已被规格吸收的退回意见当成新要求。
- 用浏览器真的点了一遍：编辑并保存规格 → 上一版留在历史里 → 带意见批准 → 需求进入编码队列，经过里依次是 完成、已编辑、已批准。


## 2026-11-16 工作空间可配置

改动：

- 各需求的工作树统一放在一个"工作空间"下，由 `[workbench] worktree_root` 指定。这个设置原来就有，但藏在 lane.toml 里一行注释中，也没有说明。现在：注释写清楚用法；README 增加一节；`config/local.toml.example` 里有示例。
- 本机路径应写在不提交的 `config/local.toml` 里。`worktree_root` 支持 `~` 和环境变量。
- `lane doctor` 显示工作空间位置并检查可写。自检覆盖：工作空间放到别处后，新需求的工作树都在那里，默认位置不再被使用。
- 只对新建的需求生效，已有需求的工作树不动。

经验：

- 工作树里的路径可以很深（NuGet 包的 `.targets` 文件就有 165 个字符），再加上工作空间本身的路径，离 Windows 的 260 字符上限不远。工作空间越浅越好。


## 2026-11-16 网页登录：默认用户 admin，密码自己填

改动：

- 默认用户名改为 `admin`。不再随机生成密码：`server.toml` 和 `server.toml.example` 里的 `password` 默认为空，由你自己填。
- 密码为空时 `lane ui` 拒绝启动并说明怎么办。原因：页面默认监听 `0.0.0.0`，能启动 Agent、能改它们可以执行的命令，所以不允许无密码访问。要无密码只在本机用，需要另外做（比如 `host` 为 `127.0.0.1` 时才允许），目前没有做。
- 去掉了占位密码 `change-me` 的检查（模板里不再有任何密码）。`check_repo_hygiene.py` 改为检查模板密码为空、首次启动创建的文件与模板一致、空密码被拒绝。


## 2026-11-16 本机配置和提交内容整理

改动：

- 项目登记（含本机路径、构建命令、agy 允许规则）从 `config/lane.toml` 移到 `config/local.toml`，启动时合并到 lane.toml 上（表逐层合并，列表整个替换）。`local.toml` 不提交，模板是 `config/local.toml.example`。网页配置页可以编辑 `local.toml`，保存前校验。
- 新增 `config/server.toml.example`（占位密码 `change-me`）。`server.toml` 仍由首次启动生成；密码还是占位值时拒绝启动和登录，避免复制模板后忘记改。
- `.gitignore` 增加 `config/local.toml` 和 `config/rules/*.md`；`config/rules/hronetime.md` 已从版本库里取消跟踪（文件还在本机）。
- 新增 `tests/check_repo_hygiene.py`：检查不该提交的文件没有被跟踪、提交的文件里没有本机路径、两个模板有效且与代码一致。

注意：

- 之前提交的 `config/lane.toml`（提交 77ab480，已推送到 fork 的 `add-lane-workbench` 分支）里带有本机路径 `C:/Users/<用户名>/…`。这次改动只改了工作树，提交历史里仍然有。要从历史里去掉，需要重写提交并强制推送，这一步没有做，由你决定。

经验：

- 把"通用设置"和"本机设置"分成两个文件，比在一个文件里放占位符更不容易泄露：真实内容根本不在被跟踪的文件里。
- "提交里没有个人信息"要有自动检查，靠人记不住；检查要扫描被跟踪文件的内容，而不是只看文件名。


## 2026-11-16 Claude 阶段加 --effort

改动：

- `lane/agents.py` 的 `build_command` 新增占位符 `{effort}`，取自 `[agents.xxx]` 的 `effort` 字段，为空时连同 `--effort` 一起省略。
- `config/lane.toml`：Claude 命令加 `--effort {effort}`，`[agents.claude] effort = "high"`。规划、评审、汇总和诊断共用这一个级别。agy 不设此字段（级别已在模型 ID 里）。

- 新增 `tests/check_agent_commands.py`：直接检查生成的 Claude / agy 命令行（含 effort 改值、留空省略、诊断步骤、agy 四个阶段），不启动 Agent，不耗额度。
- `tests/run_demo.py` 里替换 Claude 命令的匹配串同步改成新命令。

经验：

- 演示脚本靠完整匹配命令文本换成假 Agent，改命令模板后匹配会悄悄失败并跑起真的 Claude（耗额度）。改 `[agents.*]` 的 command 时要同步改这个匹配串。

注意：

- 诊断步骤（haiku）没有豁免，也带 `--effort high`。Haiku 是否接受该参数未验证，若报错，把 `diagnose = true` 打开前先试一次。

## 2026-11-16 打开 Jenny 诊断

改动：

- `config/lane.toml` 的 `[jenny] diagnose` 改为 `true`：需求卡住时，用 haiku 读日志尾部写 `diagnosis.md`。没有改代码。

经验：

- 试跑期调 `allow_commands` 时容易卡住，诊断能省去翻日志的时间；没用就改回 `false`。

## 2026-11-16 去掉 STATUS.md 和 ALERTS.md

改动：

- 不再生成 `STATUS.md`：看板和 `lane status` 实时算出同样的内容，没有别的地方读它。
- 告警历史改存 `state/alerts.jsonl`，所有项目合在一份里，每条记录带 `project` 和 `lane` 字段（与具体项目无关的额度告警两个字段为空）。看板新增可展开的"告警历史"，可按项目筛选；接口 `GET /api/alerts?project=`。
- 旧的 `STATUS.md`、`ALERTS.md` 已删除，也从 `.gitignore` 里去掉。旧 `ALERTS.md` 里的历史没有迁移（当时只有试跑期间的几条）。

经验：

- 一份带项目字段的日志加筛选，比每个项目一个文件好：跨项目的告警（额度）有地方放，也不用在新增项目时建文件。
- 页面每几秒整个重画，筛选条件和"是否展开"要记在页面外面，否则每次刷新都会被重置。
- 自检里往 Python 源文件里写带换行的字符串时，用 `"
".join(...)` 比在字符串里写转义更不容易出错。


## 2026-11-16 额度用完时自动等待；网页界面收尾

改动：

- 工具报告用量额度用完时，需求不再卡住：留在原阶段排队，暂停该工具 `limit_pause_minutes`（默认 30 分钟）后自动重试。历史里记为"额度"。
- 阶段开始时刷新心跳，修复恢复运行后 Jenny 误报"没有心跳"。
- 编码和修复阶段的提示词写明不要启动被测应用或跑 UI/API 集成测试，那是排队的集成测试阶段的工作。
- 网页界面：在浏览器里走通了新建需求、启动调度器、退回（无意见被拒、有意见成功）、批准、英文界面、配置保存和格式错误的拦截。修复"从这个阶段继续"在闸门处默认选成"规划"的问题。README 增加网页界面一节。

经验（REQ-002 编码阶段成功，评审阶段遇到额度上限）：

- 允许规则生效后，agy 完成了还原、构建、单元测试，约 25 分钟。
- agy 在编码阶段自己启动了 IIS Express 并跑了 UI 用例，用的是默认端口而不是分配给需求的端口。阶段没说不许做的事，Agent 可能会主动去做，所以要写明边界。
- 额度用完时工具只输出一句提示就退出，和"没产出结果"看起来一样，必须单独识别。

## 2026-11-16 agy 默认模型

改动：

- `config/lane.toml` 中 agy 的 code、fix、test、itest 四个阶段的 `model` 统一设为 `gemini-3.8-flash-high`。Claude 阶段不变。

经验：

- agy 的模型 ID 已带推理级别（如 `-high`），不需要单独的 effort 参数，所以没有改代码。

## 2026-11-16 网页界面（lane ui）

改动：

- 新增命令 `lane ui`：看板、需求页（产出文件、经过、日志、批准/退回/继续/清理）、统计、配置编辑（lane.toml 保存前校验并留 .bak，规则文件）。只用标准库。
- 登录：用户名和密码在 `config/server.toml`（首次启动自动创建，用户名 chris，密码随机生成）。默认允许其他机器访问；连续输错 5 次锁定 5 分钟。
- 界面文字走资源文件 `lane/ui/static/i18n/zh.json`、`en.json`，页面右上角切换。
- 对需求的操作抽到 `lane/ops.py`，命令行和网页共用。
- 新增自检 `tests/run_ui_check.py`（登录、越界读取、操作、统计、配置校验）。

经验：

- 自检发现连续两次"启动调度器"会拉起两个进程：第二次请求到达时第一个还没写锁文件。现在启动接口加锁并等锁文件出现后才返回。
- `replaceChildren(null)` 会把 null 显示成文字，统一用 `put()` 过滤。
- Windows 上 .js 的 MIME 类型可能被注册表登记成 text/plain，模块脚本会加载失败，所以静态文件类型写死在代码里。

未完成：

- 英文界面、新建需求对话框、配置保存、批准/退回按钮只经过接口自检，没有在浏览器里逐个点过。
- 后端返回的提示文字和需求说明仍是中文，没有走资源文件。
- README 还没有加入网页界面的说明。

## 2026-11-16 agy 的命令允许规则

改动：

- 项目配置新增 `allow_commands`（正则列表），`[agents.agy]` 也可以配通用的。工作台按 `command(regex:...)` 写成每次会话的 settings 文件，通过 `--settings` 传给 agy，和全局允许列表合并。
- agy 的命令模板加了 `--output-format json` 和 `--settings`。
- agy 各阶段的提示词列出允许的命令，并说明列表之外的命令会让会话直接结束。
- 命令被拒导致卡住时，需求说明里提示去补 `allow_commands`。
- HROneTime 配了 12 条规则：NuGet 还原、MSBuild、单元测试 exe、dotnet test、IIS Express 的启动和停止、TEST_BASE_URL、就绪检查。

经验（REQ-002 第一次编码阶段失败）：

- agy 无头模式下，一条不在允许列表里的命令被拒，整个会话立刻结束且没有输出。之前只测了写文件，没测执行命令，这个风险当时标为"未验证"，试跑时果然出现。
- 规则写法是 `command(regex:模式)`，从命令开头匹配。第一次试 `command(nuget.*)` 没有 `regex:` 前缀所以不匹配。
- `--settings` 可以传文件路径；规则和全局允许列表是合并关系，不是替换。
- 沙箱模式下命令不能写工作树、不能联网、不能读用户目录：边界是真的，但没法构建。
- 测试权限时，一个预期会被拒的步骤会让整个测试没有输出，容易误判成"规则没生效"。每次只测预期能通过的命令。

## 2026-11-16 规划阶段的阻塞问题显示在规格闸门上

改动：

- `spec.md` 第一行规定为 `STATUS: READY` 或 `STATUS: BLOCKED`。标了 BLOCKED 时需求仍停在规格闸门等人，但 STATUS.md 的说明会写明"规划阶段提出了需要你决定的阻塞问题"。
- HROneTime 的 `base` 改为 `feat/fp11-password-reset-tests`，因为 TC27 用例只在这个分支上。

经验（第一次真实试跑 REQ-001）：

- 建需求前要确认 `base` 分支里真的有需求依赖的内容。这次 TC27 用例在功能分支上而不在 main 上，规划阶段发现了，但需求得重建。查看仓库最新提交时要先看当前检出的是哪个分支。
- 规划阶段（Claude Opus 无头）约 3 分钟完成，能查明根因，并主动标出范围外的同类缺陷和阻塞问题。
- 规划阶段自己发明了 `STATUS: BLOCKED` 这个标记，因为提示词只说"标记为 BLOCKED"而没给格式。每个阶段的结果标记都要在提示词里写明。

## 2026-11-16 测试阶段拆成测试设计和集成测试

改动：

- 原来的测试阶段拆成两个：`test`（测试设计，只写测试并编译，并行）和 `itest`（集成测试，从工作树启动应用并运行，排队，一次一个）。排队上限从 `test` 移到 `itest`。
- 新增结果文件 `test-design.md`。`test-report.md` 的标记改为 `PASS / FAIL_PRODUCT / FAIL_TEST / BLOCKED`。
- 集成测试失败自动退回：产品缺陷退回修复，测试问题退回测试设计，都算一轮；轮次用完直接进入汇总。
- CoverageHub 的对接移到排队的集成测试阶段。提示词 `test-plain.md`、`test-coveragehub.md` 由 `itest.md`、`itest-coveragehub.md` 取代。

经验：

- 排队的阶段要尽量短。写测试和跑测试放在同一个会话里时，队列名额会被写测试的时间占住，其他需求白等。
- 锁应该由工作台持有，不交给 Agent：会话卡死时工作台能立刻释放名额，Agent 自己持锁则要等超时。
- 失败原因由 Agent 判断（产品缺陷还是测试问题），这个判断可能出错。判断不了时提示词要求按产品缺陷处理，轮次上限保证不会无限来回。

## 2026-11-16 按项目声明构建和验证方式，集成测试排队

改动：

- 项目配置新增 `[projects.<名称>.verify]`（还原、构建、单元测试、启动应用、就绪判断、自动化测试、环境变量、端口），取代原来的 `build_cmd` / `test_cmd`。命令写进编码、修复、评审、测试阶段的提示词，由 Agent 执行。
- 每条需求分配独立端口（`port_base` + 需求编号），测试针对本工作树启动的应用运行。
- 阶段可以设 `max_parallel`。测试阶段设为 1，集成测试（含 CoverageHub）排队执行。
- 自测 `RESULT: FAIL` 改为退回修复并算一轮，不再直接卡住。
- HROneTime 配好了 verify：NuGet 还原、MSBuild、`HROneTime.Tests.exe`、IIS Express、`TEST_BASE_URL`、只跑 FP11 的 `dotnet test`。

经验：

- 机器上已部署的站点指向主检出目录，需求分支的修复不在里面。测试必须先从工作树构建并启动，否则修对了用例也会失败。
- 自检里"不重叠"的断言要先证明它能失败：第一版在去掉排队上限后仍然通过，因为两条需求恰好没有同时到达测试阶段。改成让两条需求同时排到测试阶段后，去掉上限才会失败。
- HROneTime 的 verify 命令还没有在真实工作树里跑过，第一次试跑时要留意还原和 IIS Express 这两步。

## 2026-11-16 项目规则和本地文件

改动：

- 新增 `templates/AGENTS.md`（规则模板）和命令 `lane rules <项目>`，为每个项目生成可编辑的 `config/rules/<项目>.md`。
- 编码、修复、评审、测试四个阶段的提示词带上项目规则，取代原来的 `conventions` 配置项。规划和汇总阶段不带。
- 项目配置新增 `copy_files`：建需求时把被 git 忽略的本地文件复制进工作树。列出的路径如果没有被忽略会报错，避免被提交。
- 登记试点项目 `hronetime`，规则文件以 HROneTime 自己的 `AGENTS.md` 为起点。
- `runner.py` 拆出 `agents.py`（提示词渲染和工具调用），每个文件保持在 300 行以内。
- 修复：两个阶段同时更新 `state/usage.json` 时偶发读到半个文件，调度器异常退出。现在读写都加锁。

经验：

- 工作树只有已提交的内容。`.agents/`（CoverageHub skills 和 MCP 配置）、`.env`、NuGet `packages/` 都被忽略，新工作树里没有，要么复制、要么还原。
- 面向对话的规则（等确认、停下提问）不能原样用于无头会话，否则 Agent 会停在原地没有产出。在提示词里统一映射为 BLOCKED，比改每个项目的规则文件稳妥。
- 用假 Agent 的自检要多跑几遍，并发问题不是每次都出现。

## 2026-11-16 初版

- `lane` 命令行、六个阶段的提示词、并行调度、Jenny 跟踪、用假 Agent 的端到端自检。
