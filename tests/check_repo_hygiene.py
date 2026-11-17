"""检查哪些文件会被提交：不该提交的没有被跟踪，被提交的文件里没有本机路径或真实密码。

不启动任何 Agent，不耗额度。用法：python tests/check_repo_hygiene.py
"""
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout


tracked = [f for f in git("ls-files").splitlines() if (ROOT / f).exists()]

# 1. 这些不能被跟踪：真实登录文件、本机项目登记、各项目的规则、备份、运行状态、工作树
forbidden = [r"^config/server\.toml$", r"^config/local\.toml$", r"^config/rules/.*\.md$", r"\.bak$", r"\.tmp$",
             r"^state/", r"^worktrees/", r"(^|/)__pycache__/", r"(^|/)\.lane/"]
bad = [f for f in tracked if any(re.search(p, f) for p in forbidden)]
assert not bad, f"这些文件不该被提交，但已被跟踪：{bad}"

# 2. 这些路径必须被 .gitignore 忽略（文件不存在也检查规则本身）
for name in ("config/server.toml", "config/local.toml", "config/rules/x.md", "config/lane.toml.bak",
             "config/local.toml.bak", "config/rules/x.md.bak", "templates/AGENTS.md.bak", "state/a", "worktrees/a"):
    assert subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0, f"{name} 没有被 .gitignore 忽略"
# 反过来：要提交的模板不能被误忽略
for name in ("config/lane.toml", "config/server.toml.example", "config/local.toml.example", "templates/AGENTS.md"):
    assert subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 1, f"{name} 被误忽略了"

# 3. 被提交的文件里不能有本机路径（C:/Users/<真名>、/home/<真名>、/Users/<真名>）；占位请用 you、<名字> 或 C:/path/to
path_pattern = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|(?<![\w.])/(?:home|Users)/)(?!you\b|<|\$|\{|name\b)[^\\/\s\"'`]+", re.I)
leaks = []
for f in tracked:
    if f == "tests/check_repo_hygiene.py":
        continue
    try:
        text = (ROOT / f).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    leaks += [f"{f}: {m.group(0)}" for m in path_pattern.finditer(text)]
assert not leaks, f"提交的文件里有本机路径：{leaks}"

# 4. 模板：存在、能解析、密码为空（不带任何真实密码），并且和代码里生成的内容一致
from lane.ui.auth import TEMPLATE, ensure_server_config, read_server_config  # noqa: E402

example = (ROOT / "config" / "server.toml.example").read_text(encoding="utf-8")
assert example == TEMPLATE, "server.toml.example 和 auth.py 里的 TEMPLATE 不一致"
login = tomllib.loads(example)["login"]
assert login["username"] == "admin" and login["password"] == "", login
local_example = tomllib.loads((ROOT / "config" / "local.toml.example").read_text(encoding="utf-8"))
assert local_example["projects"], "local.toml.example 里应有一个完整的项目例子"
for name, proj in local_example["projects"].items():
    assert proj["repo"].startswith("C:/path/to/"), f"[projects.{name}] 的 repo 应是占位路径"
assert "projects" not in tomllib.loads((ROOT / "config" / "lane.toml").read_text(encoding="utf-8")), \
    "提交的 lane.toml 里不应有项目登记（它们在 local.toml 里）"

# 5. 密码为空不能启动：复制模板、或首次启动自动创建的 server.toml，没填密码都要被拒绝
with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    (home / "config").mkdir()
    path, created = ensure_server_config(home)
    assert created and path.read_text(encoding="utf-8") == example, "首次启动创建的文件应和模板一致"
    assert ensure_server_config(home)[1] is False, "已有文件不能被覆盖"
    try:
        read_server_config(home)
    except SystemExit as exc:
        assert "password 是空的" in str(exc), exc
    else:
        raise AssertionError("密码为空应被拒绝")
    path.write_text(example.replace('password = ""', 'password = "x"'), encoding="utf-8")
    assert read_server_config(home)["login"]["password"] == "x", "填了密码就能读"

print(f"全部通过（被跟踪的文件 {len(tracked)} 个）")
