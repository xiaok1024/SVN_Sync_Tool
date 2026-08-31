# -*- coding: utf-8 -*-
"""本地 Web 的账号、会话与 SVN 凭据存储。

设计取舍（由使用者明确决定）：

- **SVN 密码以明文保存**在一个 JSON 文件里，因为 ``svn`` 命令需要真实密码，
  无法像登录密码那样只存哈希。拿到该文件即等同拿到全部人的 SVN 账号，
  因此文件默认放在**仓库之外**、权限 ``0600``，且绝不回传浏览器、绝不进日志。
- **登录密码仍然做哈希**（``hashlib.scrypt``，标准库）。它不需要还原成明文，
  没有理由存明文，也就不必引入额外的加密依赖。

会话保存在内存中：服务重启即全部失效，需要重新登录。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from web_svn_common import WebSvnError, validate_svn_credential


USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MAX_DISPLAY_NAME = 32
MAX_USERS = 200
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "lzr_session"
# scrypt 参数：n=2**15 时单次校验约几十毫秒，足以拖慢离线爆破且不影响交互。
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# 128 * n * r = 32 MiB，正好顶到 OpenSSL 的默认 maxmem，需显式放宽。
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthError(WebSvnError):
    """账号相关的业务错误；结构与其他 Web 功能保持一致。"""


def _now():
    return int(time.time())


def hash_password(password):
    """返回 ``scrypt$n$r$p$salt$hash``（salt 与 hash 为 base64）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password, encoded):
    """校验密码；任何格式异常都按验证失败处理，不抛出细节。"""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = str(encoded or "").split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(hash_b64)),
            maxmem=_SCRYPT_MAXMEM)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def normalize_username(value):
    text = str(value or "").strip().lower()
    if not USERNAME_RE.match(text):
        raise AuthError(
            "invalid_username",
            "账号需 2-32 位，以字母或数字开头，只能包含字母、数字、下划线、点或连字符")
    return text


def validate_login_password(value):
    text = str(value or "")
    if len(text) < MIN_PASSWORD_LENGTH:
        raise AuthError("weak_password", "登录密码至少 %d 位" % MIN_PASSWORD_LENGTH)
    if len(text) > MAX_PASSWORD_LENGTH:
        raise AuthError("invalid_password", "登录密码过长")
    if "\x00" in text:
        raise AuthError("invalid_password", "登录密码包含不支持的字符")
    return text


def normalize_display_name(value, fallback):
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    if len(text) > MAX_DISPLAY_NAME:
        raise AuthError("invalid_display_name", "显示名称最长 %d 个字符" % MAX_DISPLAY_NAME)
    if any(ord(char) < 32 for char in text):
        raise AuthError("invalid_display_name", "显示名称包含不支持的字符")
    return text


class AuthService:
    """账号存储与会话管理。

    所有写操作都在锁内「读—改—整体重写」，并通过临时文件 + ``os.replace``
    原子落盘，避免并发写坏文件。
    """

    def __init__(self, store_path=None):
        self.store_path = Path(store_path or default_store_path()).expanduser()
        self._lock = threading.RLock()
        self._sessions = {}
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.store_path.exists():
            self._write({"version": 1, "users": {}})
        self._harden_permissions()

    @classmethod
    def from_environment(cls, environ=None):
        env = environ if environ is not None else os.environ
        return cls(store_path=env.get("SVN_SYNC_WEB_USER_STORE") or None)

    # ── 存储 ────────────────────────────────────────────────

    def _harden_permissions(self):
        """凭据文件含明文 SVN 密码，收紧到仅属主可读写。"""
        for path, mode in ((self.store_path.parent, 0o700), (self.store_path, 0o600)):
            try:
                path.chmod(mode)
            except OSError:
                pass

    def _read(self):
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "users": {}}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthError(
                "user_store_corrupt",
                "账号存储文件格式不正确，请联系服务维护者", 500) from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise AuthError("user_store_corrupt", "账号存储文件结构不正确", 500)
        return data

    def _write(self, data):
        temp = self.store_path.with_name(self.store_path.name + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.store_path)
        self._harden_permissions()

    # ── 账号 ────────────────────────────────────────────────

    def register(self, *, username, password, display_name=""):
        clean_username = normalize_username(username)
        clean_password = validate_login_password(password)
        clean_display = normalize_display_name(display_name, clean_username)
        with self._lock:
            data = self._read()
            if clean_username in data["users"]:
                raise AuthError("username_taken", "该账号已存在", 409)
            if len(data["users"]) >= MAX_USERS:
                raise AuthError("too_many_users", "账号数量已达上限", 403)
            data["users"][clean_username] = {
                "display_name": clean_display,
                "password_hash": hash_password(clean_password),
                "created_at": _now(),
                "svn_username": "",
                "svn_password": "",
            }
            self._write(data)
        return self.public_profile(clean_username)

    def authenticate(self, *, username, password):
        try:
            clean_username = normalize_username(username)
        except AuthError:
            # 账号格式错误与密码错误返回同一提示，避免枚举已存在的账号。
            raise AuthError("invalid_credentials", "账号或密码不正确", 401) from None
        with self._lock:
            record = self._read()["users"].get(clean_username)
        if record is None or not verify_password(
                str(password or ""), record.get("password_hash", "")):
            raise AuthError("invalid_credentials", "账号或密码不正确", 401)
        return clean_username

    def change_password(self, username, *, current_password, new_password):
        self.authenticate(username=username, password=current_password)
        clean_new = validate_login_password(new_password)
        with self._lock:
            data = self._read()
            record = data["users"].get(username)
            if record is None:
                raise AuthError("user_not_found", "账号不存在", 404)
            record["password_hash"] = hash_password(clean_new)
            self._write(data)
        self.revoke_all_sessions(username)

    # ── SVN 凭据 ────────────────────────────────────────────

    def set_svn_credentials(self, username, *, svn_username, svn_password):
        clean_user = validate_svn_credential(
            svn_username, "username", AuthError, max_length=256, required=True)
        clean_pass = validate_svn_credential(
            svn_password, "password", AuthError, max_length=1024, required=True)
        with self._lock:
            data = self._read()
            record = data["users"].get(username)
            if record is None:
                raise AuthError("user_not_found", "账号不存在", 404)
            record["svn_username"] = clean_user
            record["svn_password"] = clean_pass
            self._write(data)

    def clear_svn_credentials(self, username):
        with self._lock:
            data = self._read()
            record = data["users"].get(username)
            if record is None:
                raise AuthError("user_not_found", "账号不存在", 404)
            record["svn_username"] = ""
            record["svn_password"] = ""
            self._write(data)

    def get_svn_credentials(self, username):
        """返回 ``(svn_username, svn_password)``，供服务端执行 SVN 时使用。

        调用方必须只把它传给 SVN 引擎，不得写入响应体或日志。
        """
        with self._lock:
            record = self._read()["users"].get(username)
        if record is None:
            raise AuthError("user_not_found", "账号不存在", 404)
        svn_user = record.get("svn_username") or ""
        svn_pass = record.get("svn_password") or ""
        if not svn_user or not svn_pass:
            raise AuthError(
                "svn_credentials_missing",
                "尚未保存 SVN 账号，请先在「我的 SVN 账号」中填写", 428)
        return svn_user, svn_pass

    def public_profile(self, username):
        """可安全返回浏览器的账号信息：绝不包含任何密码。"""
        with self._lock:
            record = self._read()["users"].get(username)
        if record is None:
            raise AuthError("user_not_found", "账号不存在", 404)
        return {
            "username": username,
            "display_name": record.get("display_name") or username,
            "svn_username": record.get("svn_username") or "",
            "has_svn_credentials": bool(
                record.get("svn_username") and record.get("svn_password")),
        }

    # ── 会话 ────────────────────────────────────────────────

    def create_session(self, username):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "username": username, "expires_at": _now() + SESSION_TTL_SECONDS}
        return token

    def resolve_session(self, token):
        """返回会话对应的账号；无效或过期返回 ``None``。"""
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session["expires_at"] <= _now():
                self._sessions.pop(token, None)
                return None
            # 滑动续期：持续使用不会在操作中途掉线。
            session["expires_at"] = _now() + SESSION_TTL_SECONDS
            return session["username"]

    def destroy_session(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all_sessions(self, username):
        with self._lock:
            for token in [t for t, s in self._sessions.items()
                          if s["username"] == username]:
                self._sessions.pop(token, None)

    def purge_expired_sessions(self):
        now = _now()
        with self._lock:
            for token in [t for t, s in self._sessions.items()
                          if s["expires_at"] <= now]:
                self._sessions.pop(token, None)

    def user_count(self):
        with self._lock:
            return len(self._read()["users"])


def default_store_path():
    """默认放在仓库之外，避免明文凭据被误提交进 Git。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "svn_sync_tool", "web_users.json")


__all__ = [
    "AuthError",
    "AuthService",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "default_store_path",
    "hash_password",
    "normalize_username",
    "verify_password",
]
