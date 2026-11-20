"""在 GitHub Actions 里运行一个检查。失败时把输出的最后 30 行写成错误标注。

标注直接显示在运行页面上，也能通过接口读到，不用登录、也不用点进日志。
用法：python tests/ci_run.py tests/run_ui_check.py
"""
import subprocess
import sys

result = subprocess.run([sys.executable, *sys.argv[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace")
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
if result.returncode:
    tail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-30:])
    tail = tail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={sys.argv[1]} failed::{tail}")
sys.exit(result.returncode)
