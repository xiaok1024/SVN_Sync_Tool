# -*- coding: utf-8 -*-
"""Web 入口共用的 SVN 安全外壳。

Web 端每个 SVN 功能都必须满足同一组边界：地址来自浏览器必须重新校验，
凭据只在单次请求内存在，且不得读写主机的 SVN 认证缓存。这些约束与具体
功能无关，因此集中在本模块，由 :mod:`web_standard_service` 与
:mod:`web_path_service` 共用；真正的 SVN 执行仍然只有 ``SyncEngine`` 一处实现。
"""

from __future__ import annotations

import subprocess
import urllib.parse
from pathlib import Path

from svn_sync_core import SVN_EXECUTABLE, SyncEngine


class WebSvnError(Exception):
    """可安全返回给浏览器的 Web SVN 业务错误。"""

    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def supports_password_from_stdin():
    """确认当前 SVN CLI 支持 ``--password-from-stdin``，避免密码进入命令行。"""
    try:
        result = subprocess.run(
            [SVN_EXECUTABLE, "help", "checkout", "--verbose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return b"--password-from-stdin" in result.stdout


def read_allowed_svn_prefixes(environ):
    """读取服务端允许连接的 SVN 前缀白名单（逗号分隔）。"""
    configured = str(environ.get("SVN_SYNC_WEB_ALLOWED_SVN_PREFIXES", "") or "")
    return tuple(part.strip() for part in configured.split(",") if part.strip())


def normalize_svn_url(value, allowed_prefixes=(), allow_file_urls=False,
                      error_type=WebSvnError, label="客户 SVN 地址"):
    """校验并归一化浏览器提交的 SVN 地址。

    ``allow_file_urls`` 只供测试使用；``error_type`` 让各功能返回自己的错误类型。
    """
    text = str(value or "").strip().rstrip("/")
    if not text or len(text) > 2048 or any(ord(char) < 32 for char in text):
        raise error_type("invalid_svn_url", "%s格式不正确" % label)
    parsed = urllib.parse.urlsplit(text)
    try:
        parsed.port
    except ValueError:
        raise error_type("invalid_svn_url", "%s端口格式不正确" % label) from None
    if any(ord(char) < 32 or ord(char) == 127
           for char in urllib.parse.unquote(text)):
        raise error_type("invalid_svn_url", "%s包含不安全字符" % label)
    valid_http = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    valid_test_file = allow_file_urls and parsed.scheme.lower() == "file" and bool(parsed.path)
    if not (valid_http or valid_test_file):
        raise error_type("invalid_svn_url", "%s只支持 http 或 https" % label)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise error_type("invalid_svn_url", "SVN 地址不能包含账号、查询参数或片段")
    if allowed_prefixes:
        accepted = any(
            text == prefix.rstrip("/") or text.startswith(prefix.rstrip("/") + "/")
            for prefix in allowed_prefixes
        )
        if not accepted:
            raise error_type("svn_url_not_allowed", "该 SVN 地址不在服务端允许范围内", 403)
    return text


def validate_svn_credential(value, field, error_type=WebSvnError, max_length=1024,
                            required=False):
    """校验浏览器提交的账号或密码，拒绝换行注入并保持原文不落日志。"""
    text = str(value or "")
    if field == "username":
        text = text.strip()
    if required and not text:
        raise error_type("invalid_%s" % field, "请填写有效的 SVN %s" % (
            "账号" if field == "username" else "密码"))
    if (len(text) > max_length or "\n" in text or "\r" in text or "\x00" in text):
        raise error_type("invalid_%s" % field, "SVN %s格式不正确" % (
            "账号" if field == "username" else "密码"))
    return text


READ_ONLY_SVN_SUBCOMMANDS = frozenset({
    "log", "info", "list", "ls", "cat", "proplist", "propget", "blame",
})


class HostAuthSvnEngine(SyncEngine):
    """只读查询专用：复用主机 ``~/.subversion`` 的缓存认证。

    浏览器不提交任何凭据，服务端也不设置 ``--config-dir``，因此走的是运行该
    服务的账号自己的 SVN 认证缓存。为保证这条通道永远不可能写入仓库，
    这里用子命令白名单硬性拦截，新增写操作会直接失败而不是静默放行。
    """

    def __init__(self):
        super().__init__()
        self.svn_user = ""
        self.svn_pass = ""

    def _assert_read_only(self, args):
        if not args or str(args[0]) not in READ_ONLY_SVN_SUBCOMMANDS:
            raise WebSvnError(
                "read_only_engine_violation",
                "主机缓存认证通道只允许只读 SVN 命令",
                500,
            )

    def _run_svn(self, _log_widget, *args):
        self._assert_read_only(args)
        return self._run_svn_bytes(*args, timeout=180)

    def _run_svn_bytes(self, *args, **kwargs):
        self._assert_read_only(args)
        return super()._run_svn_bytes(*args, **kwargs)

    def release_credentials(self):
        pass


class WebSvnEngine(SyncEngine):
    """强制使用独立配置、禁用缓存并通过 stdin 传递密码。"""

    def __init__(self, username, password, config_dir):
        super().__init__()
        self.svn_user = username
        self.svn_pass = password
        self.svn_config_dir = str(config_dir)
        self.svn_no_auth_cache = True
        self.svn_password_from_stdin = True
        self.commit_process_started = False
        config_path = Path(config_dir)
        config_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            config_path.chmod(0o700)
        except OSError:
            pass
        (config_path / "config").write_text(
            "[auth]\npassword-stores =\nstore-passwords = no\nstore-auth-creds = no\n",
            encoding="utf-8",
        )
        (config_path / "servers").write_text(
            "[global]\nstore-passwords = no\nstore-plaintext-passwords = no\nstore-auth-creds = no\n",
            encoding="utf-8",
        )

    def _run_svn(self, _log_widget, *args):
        return self._run_svn_bytes(*args, timeout=180)

    def _svn_process_started(self, args):
        if args and args[0] == "commit":
            self.commit_process_started = True

    def reset_commit_tracking(self):
        self.commit_process_started = False

    def release_credentials(self):
        self.svn_user = ""
        self.svn_pass = ""


__all__ = [
    "READ_ONLY_SVN_SUBCOMMANDS",
    "HostAuthSvnEngine",
    "WebSvnEngine",
    "WebSvnError",
    "normalize_svn_url",
    "read_allowed_svn_prefixes",
    "supports_password_from_stdin",
    "validate_svn_credential",
]
