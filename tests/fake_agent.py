"""假 Agent：不调用任何模型，按阶段写出预期的文件。用来验证工作台本身的流转。

用法（由 run_demo.py 写进临时配置）：python fake_agent.py <工具名> <prompt 参数>
行为：
  - 第一次评审给 FAIL，并故意改一个产品文件（应被工作台撤销）；之后的评审给 PASS。
  - 标题里带"超时"的需求在编码阶段睡死（应被工作台按超时终止）。
"""
import re
import sys
import time
from pathlib import Path

tool, prompt_arg = sys.argv[1], sys.argv[2]
stage = re.search(r"prompt-(\w+)\.md", prompt_arg).group(1)
prompt = Path(f".lane/prompt-{stage}.md").read_text(encoding="utf-8")
req_id = re.search(r"REQ-\d+", prompt).group(0)
title = prompt.splitlines()[0]
req = Path("docs/reqs") / req_id
print(f"[fake {tool}] {stage} {req_id}")
time.sleep(1.5)  # 让并行可见

if "额度用完" in title and stage == "code" and not Path(".lane/limit-seen").exists():
    Path(".lane/limit-seen").write_text("x")   # 第一次模拟额度用完，之后正常
    print("You've hit your 5-hour limit · resets 6pm")
    sys.exit(1)

if stage == "plan":
    status = "BLOCKED" if "第二个" in title else "READY"  # 规划提出阻塞问题：闸门说明里要体现
    (req / "spec.md").write_text(f"STATUS: {status}\n\n# 规格\n\n" + "AC1：页面显示问候语。\n" * 5, encoding="utf-8")
elif stage in ("code", "fix"):
    if "超时" in title:
        time.sleep(600)
    if "权限被拒" in title:  # 模拟 agy：命令被拒后会话直接结束，没有结果文件
        print('{"conversation_id":"x","status":"SUCCESS","response":"","denied_actions":[{"action":"command"}]}')
        sys.exit(0)
    Path("app.txt").write_text(f"hello from {stage}\n", encoding="utf-8")
    result = "FAIL" if stage == "code" and "自测失败" in title else "PASS"  # 应被退回修复，算一轮
    (req / "selftest.md").write_text(f"RESULT: {result}\n\n构建结果见上。\n", encoding="utf-8")
elif stage == "review":
    first = not list((req / "history").glob("*-review.md")) if (req / "history").exists() else True
    if first:
        Path("app.txt").write_text("评审者不该改这个文件\n", encoding="utf-8")
        Path("stray.txt").write_text("越界的新文件\n", encoding="utf-8")
        (req / "review.md").write_text("**VERDICT**: FAIL\n\n## 必须修复\n1. 示例问题\n", encoding="utf-8")
    else:
        (req / "review.md").write_text("VERDICT: PASS\n", encoding="utf-8")
elif stage == "test":
    Path("tests").mkdir(exist_ok=True)
    Path("tests/test_app.txt").write_text("用例\n", encoding="utf-8")
    (req / "test-design.md").write_text("RESULT: PASS\n\n3 个用例已就绪。\n", encoding="utf-8")
elif stage == "itest":
    time.sleep(2)  # 拉长一点，排队失效时两个集成运行会重叠
    first = not list((req / "history").glob("*-test-report.md"))
    if "集成失败" in title and first:  # 第一次集成运行报产品缺陷，应退回修复
        (req / "test-report.md").write_text("RESULT: FAIL_PRODUCT\n\n1 个用例失败。\n", encoding="utf-8")
    else:
        (req / "test-report.md").write_text("RESULT：PASS\n\n3 个用例全部通过。\n", encoding="utf-8")
elif stage == "report":
    (req / "summary.md").write_text("# 汇总\n\n可以合并。\n", encoding="utf-8")
