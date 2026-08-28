# -*- coding: utf-8 -*-
"""版本号路径生成的 Web 适配层。

浏览器只提交仓库地址、个人凭据、版本号和排序方式。版本号解析、SVN 查询、
``(V版本)`` URL 拼接与三种排序始终复用 :mod:`svn_path_generator` 的纯逻辑 API；
本模块只负责输入限制、凭据隔离、并发与时间上限，以及可安全返回浏览器的响应组装。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from svn_path_generator import (
    build_revision_url_rows,
    parse_revision_spec,
    query_revision_paths,
    sort_existing_urls,
)
from svn_sync_core import redact_sensitive_text
from web_svn_common import (
    HostAuthSvnEngine,
    WebSvnEngine,
    WebSvnError,
    normalize_svn_url,
    read_allowed_svn_prefixes,
    supports_password_from_stdin,
    validate_svn_credential,
)


SORT_KEYS = ("rev", "path", "name")
MAX_REVISION_SPEC_BYTES = 4 * 1024
MAX_REVISIONS = 200
MAX_REVISION_NUMBER = 2_000_000_000
MAX_RESULT_ROWS = 20_000
MAX_SORT_BYTES = 1024 * 1024
MAX_SORT_LINES = 20_000
MAX_ERROR_ITEMS = 20
MAX_ERROR_LENGTH = 240
PER_REVISION_TIMEOUT = 60
QUERY_TIME_BUDGET = 240
ORPHAN_MAX_AGE = 60 * 60
_BUDGET_MARKER = "__lzr_time_budget_exceeded__"
_REVISION_SPEC_CHARS = re.compile(r"^[0-9,\s-]+$")


class PathWebError(WebSvnError):
    """版本路径生成的业务错误；结构与其他 Web SVN 功能保持一致。"""


def _normalize_sort(value):
    sort_key = str(value or "").strip()
    if sort_key not in SORT_KEYS:
        raise PathWebError("invalid_sort", "排序方式只支持 rev、path 或 name")
    return sort_key


def normalize_revision_spec(spec, max_revisions=MAX_REVISIONS):
    """校验版本号输入并返回实际要查询的版本号列表。

    解析规则完全交给 :func:`svn_path_generator.parse_revision_spec`；这里只在
    Web 边界上拒绝异常输入，并限制单次查询的版本数量（每个版本都是一次
    ``svn log``）。
    """
    value = str(spec or "")
    if not value.strip():
        raise PathWebError("empty_revision_spec", "请填写 SVN 版本号")
    if len(value.encode("utf-8")) > MAX_REVISION_SPEC_BYTES:
        raise PathWebError("revision_spec_too_large", "版本号输入超过大小限制", 413)
    normalized = (value.strip().replace("，", ",").replace("－", "-")
                  .replace("–", "-").replace("—", "-"))
    if not _REVISION_SPEC_CHARS.match(normalized):
        raise PathWebError(
            "invalid_revision_spec",
            "版本号只能包含数字、逗号、空格和连字符（示例: 123 / 123,456 / 123-456）")
    revisions = parse_revision_spec(value)
    if not revisions:
        raise PathWebError(
            "invalid_revision_spec",
            "无法解析版本号，请检查格式（示例: 123 / 123,456 / 123 456 / 123-456）")
    if revisions[0] < 1:
        raise PathWebError("invalid_revision_spec", "版本号必须是正整数")
    if revisions[-1] > MAX_REVISION_NUMBER:
        raise PathWebError("invalid_revision_spec", "版本号超出合理范围")
    if len(revisions) > max_revisions:
        raise PathWebError(
            "too_many_revisions",
            "单次最多查询 %d 个版本，当前为 %d 个，请缩小版本范围"
            % (max_revisions, len(revisions)))
    return revisions


def _safe_error_text(message, password=""):
    text = redact_sensitive_text(str(message or ""), (password,) if password else ())
    text = "".join(" " if ord(char) < 32 else char for char in text).strip()
    if len(text) > MAX_ERROR_LENGTH:
        text = text[:MAX_ERROR_LENGTH] + "…"
    return text


def sort_revision_path_text(text, sort):
    """对用户粘贴或已生成的 ``(V版本)`` 路径做本地排序，不访问 SVN。"""
    sort_key = _normalize_sort(sort)
    value = str(text or "")
    if not value.strip():
        raise PathWebError("empty_path_text", "请先填写或生成需要排序的文件路径")
    if len(value.encode("utf-8")) > MAX_SORT_BYTES:
        raise PathWebError("path_text_too_large", "待排序内容超过 1 MiB 限制", 413)
    if "\x00" in value:
        raise PathWebError("invalid_path_text", "待排序内容包含不支持的空字符")
    lines = value.splitlines()
    if len(lines) > MAX_SORT_LINES:
        raise PathWebError("too_many_lines", "待排序内容最多支持 %d 行" % MAX_SORT_LINES)
    sorted_lines = sort_existing_urls(lines, sort_key)
    return {
        "ok": True,
        "mode": "sort",
        "sort": sort_key,
        "text": "\n".join(sorted_lines),
        "stats": {"file_count": len(sorted_lines), "error_count": 0},
        "errors": [],
    }


class PathQueryService:
    """按版本号查询 SVN 文件路径；每次查询使用独立 SVN 配置目录。"""

    def __init__(
            self,
            allowed_svn_prefixes=(),
            temp_root=None,
            max_workers=2,
            engine_factory=WebSvnEngine,
            host_engine_factory=HostAuthSvnEngine,
            allow_host_auth_cache=True,
            allow_file_urls=False,
            require_password_stdin=True,
            max_revisions=MAX_REVISIONS,
            revision_timeout=PER_REVISION_TIMEOUT,
            time_budget=QUERY_TIME_BUDGET,
    ):
        self.allowed_svn_prefixes = tuple(
            prefix.rstrip("/") for prefix in allowed_svn_prefixes if prefix)
        self.temp_root = Path(
            temp_root or (Path(tempfile.gettempdir()) / "lzr-svn-path-queries-v1"))
        self.engine_factory = engine_factory
        self.host_engine_factory = host_engine_factory
        self.allow_host_auth_cache = allow_host_auth_cache
        self.allow_file_urls = allow_file_urls
        self.require_password_stdin = require_password_stdin
        self.max_revisions = max_revisions
        self.revision_timeout = revision_timeout
        self.time_budget = time_budget
        self._slots = threading.BoundedSemaphore(max_workers)
        self.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.temp_root.chmod(0o700)
        except OSError:
            pass
        self._cleanup_orphan_directories()

    def _cleanup_orphan_directories(self, max_age=ORPHAN_MAX_AGE):
        """清理上次进程被强杀后遗留的查询目录。

        单次查询有 ``time_budget`` 秒的硬上限，因此超过一小时的目录一定不属于
        正在执行的查询，多进程并发下也不会误删。
        """
        cutoff = time.time() - max_age
        for entry in self.temp_root.glob("query-*"):
            try:
                if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue

    @classmethod
    def from_environment(cls, environ=None):
        env = environ if environ is not None else os.environ
        configured = str(env.get("SVN_SYNC_WEB_ALLOW_HOST_SVN_CACHE", "") or "").strip().lower()
        return cls(
            allowed_svn_prefixes=read_allowed_svn_prefixes(env),
            allow_host_auth_cache=configured not in {"0", "false", "no", "off"},
        )

    def query(self, *, svn_url, username, password, revision_spec, sort):
        sort_key = _normalize_sort(sort)
        clean_url = normalize_svn_url(
            svn_url, self.allowed_svn_prefixes, allow_file_urls=self.allow_file_urls,
            error_type=PathWebError, label="SVN 仓库地址")
        clean_username = validate_svn_credential(
            username, "username", PathWebError, max_length=256)
        clean_password = validate_svn_credential(
            password, "password", PathWebError, max_length=1024)
        use_host_cache = not clean_username and not clean_password
        if not use_host_cache and not (clean_username and clean_password):
            raise PathWebError(
                "incomplete_credentials",
                "请同时填写 SVN 账号和密码；两者都留空则使用本机 SVN 缓存认证")
        if use_host_cache and not self.allow_host_auth_cache:
            raise PathWebError(
                "host_auth_cache_disabled",
                "服务端已禁用本机缓存认证，请填写 SVN 账号和密码", 403)
        if (clean_password and self.require_password_stdin
                and not supports_password_from_stdin()):
            raise PathWebError(
                "svn_password_stdin_unsupported",
                "当前 SVN CLI 不支持安全的 stdin 密码输入，已拒绝执行查询",
                503,
            )
        revisions = normalize_revision_spec(revision_spec, self.max_revisions)

        if not self._slots.acquire(blocking=False):
            raise PathWebError("too_many_queries", "当前查询任务较多，请稍后再试", 429)
        try:
            if use_host_cache:
                # 只读查询：不接收凭据，也就没有需要隔离的临时配置目录。
                engine = self.host_engine_factory()
                try:
                    return self._run_query(engine, clean_url, revisions, sort_key, "",
                                           auth_mode="host-cache")
                finally:
                    engine.release_credentials()
            work_dir = Path(tempfile.mkdtemp(prefix="query-", dir=self.temp_root))
            try:
                engine = self.engine_factory(
                    clean_username, clean_password, work_dir / "svn-config")
                try:
                    return self._run_query(
                        engine, clean_url, revisions, sort_key, clean_password,
                        auth_mode="supplied")
                finally:
                    engine.release_credentials()
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        finally:
            self._slots.release()

    def _run_query(self, engine, svn_url, revisions, sort_key, password, auth_mode="supplied"):
        deadline = time.monotonic() + self.time_budget

        def runner(args):
            if time.monotonic() >= deadline:
                return -1, _BUDGET_MARKER
            try:
                return engine._run_svn_bytes(*args, timeout=self.revision_timeout)
            except TimeoutError:
                return -1, "命令执行超时（%d 秒）" % self.revision_timeout
            except OSError as exc:
                return -1, _safe_error_text(exc, password)

        try:
            results, raw_errors = query_revision_paths(
                svn_url, ",".join(str(revision) for revision in revisions), runner=runner)
        except RuntimeError as exc:
            raise PathWebError("invalid_revision_spec", _safe_error_text(exc, password)) from None

        rows = build_revision_url_rows(results, svn_url, sort_key)
        if len(rows) > MAX_RESULT_ROWS:
            raise PathWebError(
                "result_too_large",
                "本次结果超过 %d 个文件，请缩小版本范围" % MAX_RESULT_ROWS,
                413,
            )
        errors, skipped = [], 0
        for message in raw_errors:
            if _BUDGET_MARKER in message:
                skipped += 1
                continue
            errors.append(_safe_error_text(message, password))
        if skipped:
            errors.append(
                "查询超过 %d 秒时间上限，已停止；%d 个版本未查询"
                % (self.time_budget, skipped))
        truncated_errors = errors[:MAX_ERROR_ITEMS]
        if len(errors) > MAX_ERROR_ITEMS:
            truncated_errors.append("… 还有 %d 条提示未显示" % (len(errors) - MAX_ERROR_ITEMS))
        return {
            "ok": True,
            "mode": "query",
            "sort": sort_key,
            "auth_mode": auth_mode,
            "svn_url": svn_url,
            "requested_revisions": revisions,
            "rows": [{"url": url, "revision": revision, "path": path}
                     for url, revision, path in rows],
            "text": "\n".join(row[0] for row in rows),
            "errors": truncated_errors,
            "stats": {
                "file_count": len(rows),
                "revision_count": len(revisions),
                "matched_revisions": sorted({row[1] for row in rows}),
                "error_count": len(errors),
            },
        }


__all__ = [
    "MAX_ERROR_ITEMS",
    "MAX_RESULT_ROWS",
    "MAX_REVISIONS",
    "MAX_SORT_LINES",
    "PathQueryService",
    "PathWebError",
    "normalize_revision_spec",
    "sort_revision_path_text",
]
