## 与 CoverageHub 对接

本项目在 CoverageHub 里的项目 ID 是 `{{coveragehub_project}}`。CoverageHub 通过 MCP 工具（服务名 `coveragehub`）提供，配套的 skills 应已安装在本仓库的 `.agents/skills/` 下。

先完成上面的集成运行并得到结果，再做这一节。开始前确认 `coveragehub` 的 MCP 工具可用、skills 存在。任何一项不满足时，不要改用别的办法凑结果：在报告的"覆盖情况"里写明 CoverageHub 这部分没有做以及缺什么，集成运行的结果照常报告。

按 CoverageHub 的流程使用这些 skill，每个 skill 的具体步骤以它自己的 SKILL.md 为准：

1. `analyse-point` / `analyse-function`：分析本次改动涉及的功能点（每次会话都要先通过构建一致性检查）。
2. `design-cases` / `design-function`：为这些功能点补充测试用例设计。
3. `automate-cases`（如果已安装）：把已确认的用例实现为自动化测试并运行。

只处理与本次改动相关的功能点，不要顺带分析整个项目。需求模型尚未建立（需要 `map-functions`）、用例需要另一个 Agent 评审（`review-cases`、`review-automation`）、或 CoverageHub 返回"需要人确认"时，把它们记入报告，不要自己代替人或另一个 Agent 确认。
