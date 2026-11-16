"""网页界面的登录：用户名和密码放在 config/server.toml，不写在代码里。"""
from __future__ import annotations

import hmac
import secrets
import time
import tomllib
from pathlib import Path

SESSION_HOURS = 12
MAX_FAILURES = 5
LOCK_SECONDS = 300

TEMPLATE = '''# 网页界面（lane ui）的设置。改完立即生效；改了用户名或密码后，已登录的人需要重新登录。

[server]
host = "0.0.0.0"     # 0.0.0.0 = 允许其他机器访问；只限本机用 "127.0.0.1"
port = 8770

[login]
username = "chris"
password = "{password}"
'''


def ensure_server_config(home: Path) -> tuple[Path, bool]:
    """没有 server.toml 时创建一份，密码随机生成。返回 (路径, 是否新建)。"""
    path = home / "config" / "server.toml"
    if path.exists():
        return path, False
    path.write_text(TEMPLATE.format(password=secrets.token_urlsafe(12)), encoding="utf-8")
    return path, True


def read_server_config(home: Path) -> dict:
    data = tomllib.loads((home / "config" / "server.toml").read_text(encoding="utf-8"))
    login = data.get("login", {})
    if not login.get("username") or not login.get("password"):
        raise SystemExit("config/server.toml 的 [login] 里必须有 username 和 password")
    return data


class Sessions:
    """内存里的登录会话。服务重启后所有人需要重新登录。"""

    def __init__(self, home: Path):
        self.home = home
        self.tokens: dict[str, tuple[float, str]] = {}   # 令牌 -> (过期时间, 登录时的凭据指纹)
        self.failures: dict[str, list[float]] = {}        # 来源地址 -> 最近失败的时间

    def _fingerprint(self) -> str:
        login = read_server_config(self.home)["login"]
        return f"{login['username']}\0{login['password']}"

    def locked(self, addr: str) -> int:
        """该地址还要等多少秒才能再试；0 表示可以试。"""
        recent = [t for t in self.failures.get(addr, []) if time.time() - t < LOCK_SECONDS]
        self.failures[addr] = recent
        if len(recent) >= MAX_FAILURES:
            return int(LOCK_SECONDS - (time.time() - recent[0])) + 1
        return 0

    def login(self, addr: str, username: str, password: str) -> str | None:
        login = read_server_config(self.home)["login"]
        ok_user = hmac.compare_digest(username.encode(), str(login["username"]).encode())
        ok_pass = hmac.compare_digest(password.encode(), str(login["password"]).encode())
        if not (ok_user and ok_pass):
            self.failures.setdefault(addr, []).append(time.time())
            return None
        self.failures.pop(addr, None)
        token = secrets.token_urlsafe(32)
        self.tokens[token] = (time.time() + SESSION_HOURS * 3600, self._fingerprint())
        return token

    def valid(self, token: str | None) -> bool:
        entry = self.tokens.get(token or "")
        if not entry:
            return False
        expires, fingerprint = entry
        if time.time() > expires or fingerprint != self._fingerprint():
            self.tokens.pop(token, None)   # 过期，或用户名密码已被修改
            return False
        return True

    def logout(self, token: str | None) -> None:
        self.tokens.pop(token or "", None)
