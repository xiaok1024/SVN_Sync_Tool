# -*- coding: utf-8 -*-
"""Web 端 SVN 标准文件提交任务。

浏览器只提交仓库地址、个人凭据和文件清单。标准/历史来源由服务端配置，
每个任务使用独立的稀疏工作副本和 SVN config-dir；提交完成后立即清理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.parse
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from svn_standard_file_core import StandardFileService
from svn_sync_core import SVN_EXECUTABLE, SyncEngine, redact_sensitive_text
from svn_sync_workflow import (
    get_repository_head_revision,
    list_repository_files,
    prepare_sparse_working_copy,
)


JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_FILE_LIST_BYTES = 512 * 1024
MAX_FILE_LINES = 1000
MAX_FILE_PATHS = 1000
MAX_REPOSITORY_FILES = 1000000
MAX_PREVIEW_ITEMS = 1000
MAX_FILE_LINE_BYTES = 8192
MAX_COMMIT_MESSAGE = 500
SVN_WC_SIZE_MULTIPLIER = 2
SVN_WC_FILE_OVERHEAD = 64 * 1024
ACTIVE_STATES = {"queued", "preparing", "preview_ready", "commit_queued", "committing"}
TERMINAL_STATES = {"committed", "no_changes", "failed", "expired", "cancelled", "commit_unknown"}
DEFAULT_STANDARD_UNC_PREFIX = r"\\192.168.7.215\ECOLOGY_customer"


class StandardWebError(Exception):
    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SourceProfile:
    profile_id: str
    label: str
    standard_path: str = ""
    historical_path: str = ""
    unc_prefix: str = DEFAULT_STANDARD_UNC_PREFIX
    smb_credentials_file: str = ""

    @property
    def available(self):
        if self.standard_path and os.path.isdir(self.standard_path):
            return True
        return bool(self.unc_prefix and self.smb_credentials_file
                    and os.path.isfile(self.smb_credentials_file))

    def public_dict(self):
        return {
            "id": self.profile_id,
            "label": self.label,
            "available": self.available,
            "has_standard": bool(self.standard_path),
            "has_historical": False,
            "accepts_customer_path": bool(self.unc_prefix),
            "unc_prefix": self.unc_prefix,
            "priority": "固定标准共享根；每个任务选择客户 QC 的 ecology 目录",
        }


def _configured_path(value):
    text = os.path.expanduser(str(value or "").strip())
    if not text:
        return ""
    if not os.path.isabs(text):
        raise ValueError("Web 标准文件来源必须配置为绝对路径")
    return os.path.realpath(text)


def _discover_smb_credentials_file(env, allow_workspace_default=False):
    explicit = (env.get("SVN_SYNC_WEB_SMB_CREDENTIALS_FILE")
                or env.get("E9_SMB_CREDENTIALS_FILE"))
    if explicit:
        return _configured_path(explicit)
    secrets_root = str(env.get("E9_SECRETS_ROOT", "") or "").strip()
    if secrets_root:
        return _configured_path(Path(secrets_root).expanduser() / "e9-smb-credentials.toml")
    config_value = str(env.get("E9_PATHS_FILE", "") or "").strip()
    config_path = Path(config_value).expanduser() if config_value else None
    if config_path is None and allow_workspace_default:
        project_root = Path(__file__).resolve().parent
        config_path = (
            project_root.parents[2] / "workspaces" / "e9" / "ecology-9-dev"
            / ".ai-data" / "local" / "e9-paths.json"
        )
    if not config_path or not config_path.is_file():
        return ""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    root = payload.get("secrets.root") if isinstance(payload, dict) else ""
    return (_configured_path(Path(root).expanduser() / "e9-smb-credentials.toml")
            if isinstance(root, str) and root.strip() else "")


def load_source_profiles(environ=None):
    env = environ or os.environ
    raw = str(env.get("SVN_SYNC_WEB_SOURCE_PROFILES", "") or "").strip()
    profiles = []
    default_unc_prefix = str(
        env.get("SVN_SYNC_WEB_STANDARD_UNC_PREFIX", DEFAULT_STANDARD_UNC_PREFIX) or "").strip()
    default_credentials_file = _discover_smb_credentials_file(
        env, allow_workspace_default=environ is None)
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SVN_SYNC_WEB_SOURCE_PROFILES 不是有效 JSON") from exc
        if not isinstance(values, list):
            raise ValueError("SVN_SYNC_WEB_SOURCE_PROFILES 必须是 JSON 数组")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("来源 Profile 必须是对象")
            profile_id = str(value.get("id", "")).strip().lower()
            if not PROFILE_ID_RE.fullmatch(profile_id):
                raise ValueError("来源 Profile id 格式不正确")
            profiles.append(SourceProfile(
                profile_id=profile_id,
                label=str(value.get("label", "")).strip() or profile_id,
                standard_path=_configured_path(value.get("standard_path")),
                historical_path="",
                unc_prefix=str(value.get("unc_prefix", default_unc_prefix) or "").strip(),
                smb_credentials_file=_configured_path(
                    value.get("smb_credentials_file") or default_credentials_file),
            ))
    else:
        standard_path = _configured_path(env.get("SVN_SYNC_WEB_STANDARD_PATH"))
        if standard_path or default_credentials_file:
            profiles.append(SourceProfile(
                "default",
                str(env.get("SVN_SYNC_WEB_SOURCE_LABEL", "E9 标准文件共享") or "").strip()
                or "E9 标准文件共享",
                standard_path,
                "",
                default_unc_prefix,
                default_credentials_file,
            ))
    if len({profile.profile_id for profile in profiles}) != len(profiles):
        raise ValueError("来源 Profile id 不能重复")
    return profiles


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _token_hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _path_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(path):
    total = 0
    for root, directories, filenames in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories
                          if not os.path.islink(os.path.join(root, name))]
        for filename in filenames:
            try:
                total += os.lstat(os.path.join(root, filename)).st_size
            except OSError:
                continue
    return total


def _supports_password_from_stdin():
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


def _strip_version_suffix(value):
    return re.sub(r"\([Vv]\d+\)(?:\s*[-—].*)?\s*$", "", value).strip()


def _normalize_svn_url(value, allowed_prefixes=(), allow_file_urls=False):
    text = str(value or "").strip().rstrip("/")
    if not text or len(text) > 2048 or any(ord(char) < 32 for char in text):
        raise StandardWebError("invalid_svn_url", "客户 SVN 地址格式不正确")
    parsed = urllib.parse.urlsplit(text)
    try:
        parsed.port
    except ValueError:
        raise StandardWebError("invalid_svn_url", "客户 SVN 地址端口格式不正确") from None
    if any(ord(char) < 32 or ord(char) == 127
           for char in urllib.parse.unquote(text)):
        raise StandardWebError("invalid_svn_url", "客户 SVN 地址包含不安全字符")
    valid_http = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    valid_test_file = allow_file_urls and parsed.scheme.lower() == "file" and bool(parsed.path)
    if not (valid_http or valid_test_file):
        raise StandardWebError("invalid_svn_url", "客户 SVN 地址只支持 http 或 https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise StandardWebError("invalid_svn_url", "SVN 地址不能包含账号、查询参数或片段")
    if allowed_prefixes:
        accepted = any(
            text == prefix.rstrip("/") or text.startswith(prefix.rstrip("/") + "/")
            for prefix in allowed_prefixes
        )
        if not accepted:
            raise StandardWebError("svn_url_not_allowed", "该 SVN 地址不在服务端允许范围内", 403)
    return text


def parse_web_file_list(file_list, _svn_url=None):
    """解析 Web 清单；这里只接受 ``ecology`` 下的普通相对文件路径。"""
    value = str(file_list or "")
    if len(value.encode("utf-8")) > MAX_FILE_LIST_BYTES:
        raise StandardWebError("file_list_too_large", "文件清单超过 512 KiB 限制", 413)
    raw_lines = value.splitlines()
    if len(raw_lines) > MAX_FILE_LINES:
        raise StandardWebError("too_many_files", "文件清单最多支持 1000 行")

    relative_paths = []
    for raw_line in raw_lines:
        line = unicodedata.normalize("NFC", raw_line.strip())
        if not line:
            continue
        if len(line.encode("utf-8")) > MAX_FILE_LINE_BYTES:
            raise StandardWebError("file_line_too_long", "文件清单中存在过长行")
        if line.startswith("[") or line.startswith("$/") or "://" in line:
            raise StandardWebError(
                "invalid_file_path", "文件清单只接受 ecology 目录下的相对文件路径")
        if line.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", line):
            raise StandardWebError(
                "local_path_not_allowed", "文件清单不能包含绝对路径或网络共享路径")
        relative = line.replace("\\", "/")
        if relative.lower().startswith("ecology/"):
            raise StandardWebError(
                "invalid_file_path", "文件路径应相对于 ecology 目录，请去掉开头的 ecology/")
        parts = PurePosixPath(relative).parts
        if (not parts or relative.endswith("/") or any(part in {"", ".", ".."} for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in relative)):
            raise StandardWebError("invalid_file_path", "文件路径不能包含控制字符")
        relative_paths.append("/".join(parts))
    unique = list(dict.fromkeys(relative_paths))
    if len(unique) > MAX_FILE_PATHS:
        raise StandardWebError("too_many_files", "手工填写的文件清单最多支持 1000 个文件")
    return unique


def parse_customer_standard_path(value, unc_prefix=DEFAULT_STANDARD_UNC_PREFIX):
    """验证固定共享根下的 ``分组/客户/QC/ecology`` 目录并返回安全后缀。"""
    text = unicodedata.normalize("NFC", str(value or "").strip().strip('"'))
    prefix = unicodedata.normalize("NFC", str(unc_prefix or "").strip()).rstrip("\\/")
    if (not text or len(text.encode("utf-8")) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        raise StandardWebError("invalid_customer_path", "请填写有效的客户标准文件 ecology 目录")
    normalized = text.replace("/", "\\").rstrip("\\")
    if not prefix:
        raise StandardWebError("source_profile_unavailable", "服务端未配置标准共享根", 503)
    prefix_normalized = prefix.replace("/", "\\")
    if not normalized.lower().startswith(prefix_normalized.lower() + "\\"):
        raise StandardWebError(
            "customer_path_not_allowed", "客户标准文件路径不在服务端允许的固定共享根下", 403)
    suffix = normalized[len(prefix_normalized):].lstrip("\\")
    parts = suffix.split("\\")
    if (len(parts) != 4 or any(part in {"", ".", ".."} for part in parts)
            or any("%" in part or ":" in part for part in parts)
            or not re.fullmatch(r"QC\d+", parts[2], flags=re.I)
            or parts[3].lower() != "ecology"):
        raise StandardWebError(
            "invalid_customer_path",
            "客户标准文件路径格式应为 固定共享根\\分组\\客户\\QC编号\\ecology")
    return "/".join(parts)


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


@dataclass
class StandardJob:
    job_id: str
    access_token_hash: str
    created_at: float
    expires_at: float
    svn_url: str
    username: str
    password: str | None
    profile_id: str
    source_relative: str
    selection_mode: str
    relative_paths: list[str]
    commit_message: str
    job_dir: Path
    wc_dir: Path
    config_dir: Path
    state: str = "queued"
    stage_label: str = "等待处理"
    progress: int = 0
    can_commit: bool = False
    checkout_revision: int | None = None
    repo_uuid: str = ""
    repo_root: str = ""
    preview_items: list[dict] = field(default_factory=list)
    preview_summary: dict = field(default_factory=dict)
    blocking_issues: list[str] = field(default_factory=list)
    status_entries: list[dict] = field(default_factory=list)
    commit_targets: list[str] = field(default_factory=list)
    preview_fingerprint: str = ""
    confirmation_token_hash: str = ""
    confirmation_token: str = ""
    commit_idempotency_key: str = ""
    revision: int | None = None
    committed_urls: list[str] = field(default_factory=list)
    committed_paths: list[str] = field(default_factory=list)
    error: dict | None = None
    cleanup_status: str = "pending"
    cancel_requested: bool = False
    finished_at: float | None = None
    events: list[dict] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def event(self, message):
        self.events.append({"time": _iso(time.time()), "message": message})
        if len(self.events) > 80:
            del self.events[:-80]


class StandardJobManager:
    def __init__(
            self,
            profiles=None,
            temp_root=None,
            allowed_svn_prefixes=(),
            max_workers=2,
            max_live_jobs=10,
            preview_ttl=15 * 60,
            result_ttl=24 * 60 * 60,
            cleanup_interval=15,
            min_free_bytes=2 * 1024 * 1024 * 1024,
            max_root_bytes=5 * 1024 * 1024 * 1024,
            max_job_bytes=1024 * 1024 * 1024,
            require_password_stdin=True,
            engine_factory=WebSvnEngine,
            allow_file_urls=False,
    ):
        self.profiles = {profile.profile_id: profile for profile in (profiles or [])}
        self.temp_root = Path(
            temp_root or (Path(tempfile.gettempdir()) / "lzr-svn-standard-jobs-v1"))
        self.allowed_svn_prefixes = tuple(prefix.rstrip("/") for prefix in allowed_svn_prefixes if prefix)
        self.max_live_jobs = max_live_jobs
        self.preview_ttl = preview_ttl
        self.result_ttl = result_ttl
        self.cleanup_interval = cleanup_interval
        self.min_free_bytes = min_free_bytes
        self.max_root_bytes = max_root_bytes
        self.max_job_bytes = max_job_bytes
        self.require_password_stdin = require_password_stdin
        self.engine_factory = engine_factory
        self.allow_file_urls = allow_file_urls
        self.jobs = {}
        self._jobs_lock = threading.RLock()
        self._capacity_lock = threading.Lock()
        self._capacity_reservations = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lzr-standard")
        self._work_slots = threading.BoundedSemaphore(max_workers)
        self._stop_event = threading.Event()
        self._cleanup_thread = None
        self._root_lock_handle = None
        self._source_mount_engine = SyncEngine()
        self._source_mount_lock = threading.Lock()
        self._started = False
        self.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.temp_root.chmod(0o700)
        except OSError:
            pass

    @classmethod
    def from_environment(cls, environ=None):
        env = environ or os.environ
        prefixes = [part.strip() for part in
                    str(env.get("SVN_SYNC_WEB_ALLOWED_SVN_PREFIXES", "") or "").split(",")
                    if part.strip()]
        # 保留 ``None``，让 load_source_profiles 能区分正常启动与测试显式传入
        # 的环境映射，并在正常启动时按 ecology-9-dev 路径注册表查找凭据引用。
        return cls(load_source_profiles(environ), allowed_svn_prefixes=prefixes)

    def start(self):
        with self._jobs_lock:
            if self._started:
                return
            self._acquire_root_lock()
            self._started = True
        self._cleanup_orphan_directories()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="lzr-standard-cleanup", daemon=True)
        self._cleanup_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            # 根目录锁必须同时覆盖清理线程；清理尚未退出时绝不能让新实例接管。
            self._cleanup_thread.join()
        with self._jobs_lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            with job.lock:
                if job.state in {"queued", "preparing"}:
                    job.cancel_requested = True
        try:
            # 根目录锁必须覆盖仍在运行的 worker。否则 reload/滚动重启时，
            # 新进程会把旧 worker 正在使用的目录误判为 orphan 并删除。
            self._executor.shutdown(wait=True, cancel_futures=True)
            for job in jobs:
                with job.lock:
                    self._release_capacity_reservation(job)
                    self._clear_credentials(job)
                    self._delete_job_directory(job)
        finally:
            with self._source_mount_lock:
                self._source_mount_engine._cleanup_temp_mounts()
            self._release_root_lock()

    def _acquire_root_lock(self):
        lock_path = self.temp_root / ".lzr-standard-manager.lock"
        handle = open(lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            raise RuntimeError("标准文件临时任务目录正被另一个 Web 进程使用") from None
        self._root_lock_handle = handle

    def _release_root_lock(self):
        handle = self._root_lock_handle
        if not handle:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        handle.close()
        self._root_lock_handle = None

    def public_profiles(self):
        values = [profile.public_dict() for profile in self.profiles.values()]
        return {"ok": True, "configured": bool(values), "profiles": values}

    def _cleanup_loop(self):
        while not self._stop_event.wait(self.cleanup_interval):
            self.cleanup_expired()

    def _write_marker(self, job_id, job_dir, created_at):
        marker = job_dir / ".lzr-standard-job.json"
        marker.write_text(json.dumps({
            "schema": 1,
            "job_id": job_id,
            "created_at": _iso(created_at),
        }, ensure_ascii=False), encoding="utf-8")

    def _safe_job_directory(self, job_id, job_dir):
        if not JOB_ID_RE.fullmatch(job_id) or job_dir.name != job_id:
            return False
        try:
            root = self.temp_root.resolve(strict=True)
            target = job_dir.resolve(strict=True)
        except OSError:
            return False
        if self.temp_root.is_symlink() or job_dir.is_symlink() or target.parent != root:
            return False
        if root in {Path(root.anchor), Path.home().resolve()}:
            return False
        marker = job_dir / ".lzr-standard-job.json"
        if not marker.is_file() or marker.is_symlink():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return payload.get("schema") == 1 and payload.get("job_id") == job_id

    def _delete_job_directory(self, job):
        if not job.job_dir.exists():
            job.cleanup_status = "cleaned"
            return True
        if not self._safe_job_directory(job.job_id, job.job_dir):
            job.cleanup_status = "failed"
            return False
        try:
            shutil.rmtree(job.job_dir)
        except OSError:
            job.cleanup_status = "failed"
            return False
        job.cleanup_status = "cleaned"
        return True

    def _cleanup_orphan_directories(self):
        with self._jobs_lock:
            try:
                children = list(self.temp_root.iterdir())
            except OSError:
                return
            active_ids = set(self.jobs)
            for child in children:
                if child.name in active_ids or not child.is_dir() or child.is_symlink():
                    continue
                if not JOB_ID_RE.fullmatch(child.name):
                    continue
                if self._safe_job_directory(child.name, child):
                    try:
                        shutil.rmtree(child)
                    except OSError:
                        pass

    def _clear_credentials(self, job):
        job.password = None
        job.username = ""

    def _safe_error_message(self, job, value):
        message = redact_sensitive_text(str(value or ""), (job.password or "",))
        replacements = {
            str(job.job_dir): "<临时任务目录>",
            str(job.wc_dir): "<临时工作副本>",
            str(job.config_dir): "<任务配置目录>",
        }
        profile = self.profiles.get(job.profile_id)
        if profile:
            if profile.standard_path:
                replacements[profile.standard_path] = "<标准文件来源>"
            if profile.historical_path:
                replacements[profile.historical_path] = "<历史文件来源>"
        for original, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            message = message.replace(original, replacement)
        return message.strip()[:1000] or "任务处理失败"

    def _fail_job(self, job, code, error):
        with job.lock:
            job.state = "failed"
            job.stage_label = "任务失败"
            job.progress = 100
            job.can_commit = False
            job.error = {"code": code, "message": self._safe_error_message(job, error)}
            job.finished_at = time.time()
            job.event("任务已停止，临时目录正在清理")
            self._clear_credentials(job)
            self._delete_job_directory(job)

    def _check_cancelled(self, job):
        with job.lock:
            expired = time.time() > job.expires_at
            if not job.cancel_requested and not expired:
                return
            was_commit_queued = job.state == "commit_queued"
            job.state = "expired" if expired else "cancelled"
            if expired and was_commit_queued:
                job.stage_label = "提交排队超时"
            else:
                job.stage_label = "任务已超时" if expired else "已取消"
            job.progress = 100
            job.finished_at = time.time()
            self._clear_credentials(job)
            self._delete_job_directory(job)
        raise StandardWebError(
            "job_expired" if expired else "job_cancelled",
            ("提交排队超时" if was_commit_queued else "任务准备超时")
            if expired else "任务已取消",
            409,
        )

    def _reserve_estimated_capacity(self, job, estimated_bytes, label):
        estimated = max(0, int(estimated_bytes or 0))
        with self._capacity_lock:
            if self._capacity_reservations.get(job.job_id, 0):
                raise RuntimeError("临时容量预留状态不一致")
            job_size = _directory_size(job.job_dir)
            root_size = _directory_size(self.temp_root)
            reserved = sum(self._capacity_reservations.values())
            if estimated > self.max_job_bytes or job_size + estimated > self.max_job_bytes:
                raise RuntimeError("%s会使临时工作副本超过单任务容量限制" % label)
            if root_size + reserved + estimated > self.max_root_bytes:
                raise RuntimeError("%s会使临时任务根目录超过容量限制" % label)
            try:
                free_bytes = shutil.disk_usage(self.temp_root).free
            except OSError as exc:
                raise RuntimeError("无法检查临时磁盘容量") from exc
            if reserved + estimated > max(0, free_bytes - self.min_free_bytes):
                raise RuntimeError("%s超过当前临时磁盘安全余量" % label)
            self._capacity_reservations[job.job_id] = estimated

    def _release_capacity_reservation(self, job):
        with self._capacity_lock:
            self._capacity_reservations.pop(job.job_id, None)

    def _check_actual_capacity(self, job):
        with self._capacity_lock:
            self._capacity_reservations.pop(job.job_id, None)
            reserved = sum(self._capacity_reservations.values())
            if _directory_size(job.job_dir) > self.max_job_bytes:
                raise RuntimeError("临时工作副本超过单任务容量限制")
            if _directory_size(self.temp_root) + reserved > self.max_root_bytes:
                raise RuntimeError("临时任务根目录超过容量限制")
            try:
                free_bytes = shutil.disk_usage(self.temp_root).free
            except OSError as exc:
                raise RuntimeError("无法检查临时磁盘容量") from exc
            if free_bytes - reserved < self.min_free_bytes:
                raise RuntimeError("临时磁盘剩余空间不足，已停止任务")

    def _prepare_safety_check(self, job, index, phase, remote_size=None):
        self._check_cancelled(job)
        if phase == "checkout_before":
            # depth-empty checkout 之前先独占预留该任务剩余的完整硬上限；远端
            # 根属性随后还会在真正 checkout 前严格门禁。
            remaining = max(0, self.max_job_bytes - _directory_size(job.job_dir))
            self._reserve_estimated_capacity(job, remaining, "临时工作副本元数据")
        if phase == "checkout_after":
            self._check_actual_capacity(job)
        if phase == "remote" and remote_size is not None:
            # SVN 工作副本通常同时保存工作文件与 pristine，再预留少量元数据。
            estimate = remote_size * SVN_WC_SIZE_MULTIPLIER + SVN_WC_FILE_OVERHEAD
            self._reserve_estimated_capacity(job, estimate, "仓库文件")
        if phase == "after":
            # 每个文件后都复核，避免小清单绕过容量限制。
            self._check_actual_capacity(job)

    def _check_source_capacity(self, job, items):
        source_bytes = 0
        for item in items:
            try:
                source_bytes += os.path.getsize(item.source_file)
            except (OSError, TypeError) as exc:
                raise RuntimeError("无法检查标准来源文件大小: %s" % item.rel_path) from exc
        self._reserve_estimated_capacity(job, source_bytes, "标准来源文件")

    def _check_capacity(self):
        self.cleanup_expired()
        with self._jobs_lock:
            live_count = sum(1 for job in self.jobs.values() if job.state in ACTIVE_STATES)
        if live_count >= self.max_live_jobs:
            raise StandardWebError("too_many_jobs", "当前待处理任务较多，请稍后再试", 429)
        try:
            disk = shutil.disk_usage(self.temp_root)
        except OSError as exc:
            raise StandardWebError("capacity_unavailable", "无法检查临时磁盘容量", 503) from exc
        if disk.free < self.min_free_bytes or _directory_size(self.temp_root) >= self.max_root_bytes:
            raise StandardWebError("capacity_exhausted", "临时磁盘空间不足，请稍后再试", 503)

    @staticmethod
    def _read_smb_credentials(profile):
        path = Path(profile.smb_credentials_file)
        if not path.is_file():
            raise RuntimeError("服务端 SMB 凭据配置不可用")
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError("服务端 SMB 凭据配置无法读取") from exc
        section = payload.get("standard")
        if not isinstance(section, dict):
            raise RuntimeError("服务端 SMB 凭据缺少 standard 配置")
        username = str(section.get("username") or "test").strip().strip("`'\" ")
        password = str(section.get("password") or "").strip().strip("`'\" ")
        if not username or not password or any(char in username + password for char in "\r\n\x00"):
            raise RuntimeError("服务端 SMB 凭据配置不完整")
        return username, password

    def _profile_source_root(self, profile):
        if profile.standard_path and os.path.isdir(profile.standard_path):
            return Path(profile.standard_path).resolve(strict=True)
        with self._source_mount_lock:
            if profile.standard_path and os.path.isdir(profile.standard_path):
                return Path(profile.standard_path).resolve(strict=True)
            unc = profile.unc_prefix.replace("/", "\\").lstrip("\\")
            parts = unc.split("\\")
            if len(parts) != 2 or not all(parts):
                raise RuntimeError("服务端固定 SMB 共享根配置不正确")
            existing = self._source_mount_engine._find_existing_smb_mount(
                parts[0], parts[1])
            if existing and os.path.isdir(existing):
                return Path(existing).resolve(strict=True)
            username, password = self._read_smb_credentials(profile)
            self._source_mount_engine.smb_user = username
            self._source_mount_engine.smb_pass = password
            try:
                mounted = self._source_mount_engine._mount_smb_macos(
                    profile.unc_prefix, readonly=True, no_prompt=True)
            except Exception as exc:
                raise RuntimeError("无法连接服务端标准文件共享") from exc
            finally:
                self._source_mount_engine.smb_user = ""
                self._source_mount_engine.smb_pass = ""
                username = ""
                password = ""
            return Path(mounted).resolve(strict=True)

    def _customer_source_path(self, job, profile):
        root = self._profile_source_root(profile)
        if job.source_relative == ".":
            candidate = root / "ecology" if (root / "ecology").is_dir() else root
        else:
            candidate = root.joinpath(*job.source_relative.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            inside = os.path.commonpath([str(root), str(resolved)]) == str(root)
        except (OSError, ValueError):
            inside = False
        if not inside or not resolved.is_dir():
            raise RuntimeError("客户标准文件 ecology 目录不存在或超出固定共享根")
        return resolved

    @staticmethod
    def _safe_source_file(source_root, relative):
        root = Path(source_root).resolve(strict=True)
        candidate = root.joinpath(*relative.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            return (resolved.is_file() and not candidate.is_symlink()
                    and os.path.commonpath([str(root), str(resolved)]) == str(root))
        except (OSError, ValueError):
            return False

    def create_job(
            self, *, svn_url, username, password, profile_id, file_list, commit_message,
            customer_standard_path="", cover_all_confirmed=False):
        if self.require_password_stdin and not _supports_password_from_stdin():
            raise StandardWebError(
                "svn_password_stdin_unsupported",
                "当前 SVN CLI 不支持安全的 stdin 密码输入，已拒绝创建任务",
                503,
            )
        profile = self.profiles.get(str(profile_id or "").strip())
        if not profile:
            raise StandardWebError("source_profile_not_found", "标准文件来源配置不存在", 404)
        if not profile.available:
            raise StandardWebError("source_profile_unavailable", "标准文件来源当前不可用", 503)
        clean_url = _normalize_svn_url(
            svn_url, self.allowed_svn_prefixes, allow_file_urls=self.allow_file_urls)
        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        if (not clean_username or len(clean_username) > 256 or "\n" in clean_username
                or "\r" in clean_username or "\x00" in clean_username):
            raise StandardWebError("invalid_username", "请填写有效的 SVN 账号")
        if (not clean_password or len(clean_password) > 1024 or "\n" in clean_password
                or "\r" in clean_password or "\x00" in clean_password):
            raise StandardWebError("invalid_password", "请填写有效的 SVN 密码")
        message = str(commit_message or "").strip()
        if (not message or len(message) > MAX_COMMIT_MESSAGE
                or any(ord(char) < 32 for char in message)):
            raise StandardWebError("invalid_commit_message", "提交说明不能为空且最多 500 个字符")
        source_relative = (
            parse_customer_standard_path(customer_standard_path, profile.unc_prefix)
            if str(customer_standard_path or "").strip() else ".")
        relative_paths = parse_web_file_list(file_list)
        selection_mode = "listed" if relative_paths else "intersection"
        if selection_mode == "intersection" and cover_all_confirmed is not True:
            raise StandardWebError(
                "cover_all_confirmation_required",
                "文件清单为空时必须确认只覆盖 SVN 与标准目录同时存在的全部文件")
        self._check_capacity()

        job_id = uuid.uuid4().hex
        access_token = secrets.token_urlsafe(32)
        created_at = time.time()
        job_dir = self.temp_root / job_id
        commit_message_with_marker = "%s [LZR-WEB:%s]" % (message, job_id)
        job = StandardJob(
            job_id=job_id,
            access_token_hash=_token_hash(access_token),
            created_at=created_at,
            expires_at=created_at + self.preview_ttl,
            svn_url=clean_url,
            username=clean_username,
            password=clean_password,
            profile_id=profile.profile_id,
            source_relative=source_relative,
            selection_mode=selection_mode,
            relative_paths=relative_paths,
            commit_message=commit_message_with_marker,
            job_dir=job_dir,
            wc_dir=job_dir / "wc",
            config_dir=job_dir / "svn-config",
        )
        job.event("任务已创建，等待临时检出")
        with self._jobs_lock:
            live_count = sum(1 for current in self.jobs.values()
                             if current.state in ACTIVE_STATES)
            if live_count >= self.max_live_jobs:
                self._clear_credentials(job)
                raise StandardWebError(
                    "too_many_jobs", "当前待处理任务较多，请稍后再试", 429)
            # 目录创建、marker 写入与任务登记必须在同一把锁内完成，避免
            # 定时 orphan 清理误删尚未登记的新任务目录。
            try:
                job_dir.mkdir(mode=0o700)
                try:
                    job_dir.chmod(0o700)
                except OSError:
                    pass
                self._write_marker(job_id, job_dir, created_at)
                self.jobs[job_id] = job
            except Exception:
                self._clear_credentials(job)
                try:
                    job_dir.rmdir()
                except OSError:
                    pass
                raise
        try:
            self._executor.submit(self._prepare_job, job_id)
        except RuntimeError:
            with self._jobs_lock:
                self.jobs.pop(job_id, None)
            self._clear_credentials(job)
            self._delete_job_directory(job)
            raise StandardWebError(
                "job_queue_unavailable", "任务队列当前不可用，请稍后再试", 503) from None
        return {
            "ok": True,
            "task": {
                "id": job_id,
                "access_token": access_token,
                "status": "queued",
                "expires_at": _iso(job.expires_at),
            },
        }

    def _new_engine(self, job):
        if not job.password or not job.username:
            raise StandardWebError("credentials_expired", "任务凭据已过期，请重新创建预览", 409)
        return self.engine_factory(job.username, job.password, job.config_dir)

    @staticmethod
    def _repository_identity(engine, wc_dir):
        rc, output = engine._run_svn_bytes(
            "info", "--xml", ".", cwd=wc_dir, force_utf8=True, timeout=60)
        if rc != 0:
            raise RuntimeError("无法读取 SVN 仓库信息: " + output.strip())
        try:
            root = ET.fromstring(output)
            return (
                (root.findtext(".//repository/uuid") or "").strip(),
                (root.findtext(".//repository/root") or "").strip(),
            )
        except ET.ParseError as exc:
            raise RuntimeError("SVN 仓库信息格式不正确") from exc

    @staticmethod
    def _read_node_properties(engine, wc_dir, relative):
        rc, output = engine._run_svn_bytes(
            "proplist", "--xml", relative + "@",
            cwd=wc_dir, force_utf8=True, timeout=60)
        if rc != 0:
            raise RuntimeError("读取 SVN 属性失败: " + output.strip())
        try:
            root = ET.fromstring(output)
        except ET.ParseError as exc:
            raise RuntimeError("SVN 属性格式不正确") from exc
        return {
            node.get("name", ""): "present"
            for node in root.findall(".//property")
            if node.get("name")
        }

    @staticmethod
    def _read_status_entries(engine, wc_dir):
        rc, output = engine._run_svn_bytes(
            "status", "--xml", ".", cwd=wc_dir, force_utf8=True, timeout=60)
        if rc != 0:
            raise RuntimeError("读取 SVN 状态失败: " + output.strip())
        root_path = Path(wc_dir).resolve()
        entries = []
        try:
            nodes = ET.fromstring(output).findall(".//entry")
        except ET.ParseError as exc:
            raise RuntimeError("SVN 状态格式不正确") from exc
        for node in nodes:
            status = node.find("wc-status")
            if status is None:
                continue
            item = status.get("item", "")
            props = status.get("props", "none")
            if item in {"", "normal", "none", "external"} and props in {"", "none", "normal"}:
                continue
            raw_path = node.get("path", "")
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root_path / candidate
            candidate = candidate.resolve(strict=False)
            try:
                relative = candidate.relative_to(root_path).as_posix()
            except ValueError as exc:
                raise RuntimeError("SVN 状态包含工作副本外路径") from exc
            if relative in {"", "."}:
                continue
            properties = {}
            if props not in {"", "none", "normal"}:
                properties = StandardJobManager._read_node_properties(
                    engine, wc_dir, relative)
            entries.append({
                "path": relative,
                "item": item,
                "props": props,
                "kind": "directory" if candidate.is_dir() else "file",
                "properties": properties,
            })
        return sorted(entries, key=lambda entry: (entry["path"].count("/"), entry["path"]))

    @staticmethod
    def _allowed_status_paths(relative_paths):
        allowed = set(relative_paths)
        for relative in relative_paths:
            parts = PurePosixPath(relative).parts
            for index in range(1, len(parts)):
                allowed.add("/".join(parts[:index]))
        return allowed

    def _validate_status(self, job, entries):
        allowed = self._allowed_status_paths(job.relative_paths)
        unsafe = []
        for entry in entries:
            if entry["path"] not in allowed:
                unsafe.append(entry["path"])
                continue
            if entry["item"] not in {"modified", "replaced"}:
                unsafe.append(entry["path"])
                continue
            if entry["props"] not in {"", "none", "normal"}:
                unsafe.append(entry["path"])
        if unsafe:
            raise RuntimeError("检测到预期清单之外或不安全的 SVN 变更: " + ", ".join(unsafe[:8]))
        changed_files = {entry["path"] for entry in entries if entry["kind"] == "file"}
        expected_changed = {
            item["path"] for item in job.preview_items if item["result"] == "已覆盖"
        }
        if changed_files != expected_changed:
            missing = sorted(expected_changed - changed_files)
            extra = sorted(changed_files - expected_changed)
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing[:5]))
            if extra:
                details.append("多出 " + ", ".join(extra[:5]))
            raise RuntimeError("SVN 变更与覆盖预览不一致: " + "；".join(details))

    def _fingerprint(self, job, entries):
        files = []
        for item in job.preview_items:
            if item["result"] != "已覆盖":
                continue
            target = job.wc_dir.joinpath(*item["path"].split("/"))
            files.append((item["path"], _path_hash(target)))
        payload = {
            "job_id": job.job_id,
            "repo_uuid": job.repo_uuid,
            "repo_root": job.repo_root,
            "revision": job.checkout_revision,
            "message": job.commit_message,
            "status": entries,
            "files": files,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")).hexdigest()

    def _set_no_changes(self, job):
        with job.lock:
            self._check_cancelled(job)
            job.state = "no_changes"
            job.stage_label = "无需提交"
            job.progress = 100
            job.can_commit = False
            job.preview_summary = {
                "requested": len(job.relative_paths),
                "changed": 0,
                "unchanged": len(job.relative_paths),
                "missing": 0,
            }
            job.finished_at = time.time()
            job.event("标准文件与仓库内容一致，无需创建新版本")
            self._clear_credentials(job)
            self._delete_job_directory(job)

    def _prepare_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return
        engine = None
        with self._work_slots:
            try:
                with job.lock:
                    job.state = "preparing"
                    job.stage_label = "正在临时检出"
                    job.progress = 12
                    job.event("正在创建独立稀疏工作副本")
                self._check_cancelled(job)
                engine = self._new_engine(job)
                profile = self.profiles[job.profile_id]
                source_root = self._customer_source_path(job, profile)
                revision = None
                if job.selection_mode == "intersection":
                    revision = get_repository_head_revision(engine, job.svn_url)
                    job.checkout_revision = revision
                    with job.lock:
                        job.stage_label = "正在计算交集文件"
                        job.progress = 18
                        job.event("清单为空，正在计算最新 SVN 与标准目录的文件交集")
                    repository_files = set(list_repository_files(
                        engine, job.svn_url, revision, MAX_REPOSITORY_FILES))
                    # 标准目录可能包含数万甚至更多文件。无需遍历整个来源树；
                    # 只检查当前 SVN 最新版本中的候选文件是否在标准目录存在，
                    # 既保持交集语义，也避免目录规模本身触发无意义的上限。
                    selected = sorted(
                        relative for relative in repository_files
                        if self._safe_source_file(source_root, relative)
                    )
                    job.relative_paths = selected
                    job.event("已识别 %d 个两端同时存在的文件" % len(selected))
                if not job.relative_paths:
                    self._set_no_changes(job)
                    return
                sparse_options = {
                    "safety_check": lambda index, phase, remote_size: self._prepare_safety_check(
                        job, index, phase, remote_size),
                }
                if revision is not None:
                    sparse_options["revision"] = revision
                checkout = prepare_sparse_working_copy(
                    engine, job.svn_url, str(job.wc_dir), job.relative_paths,
                    **sparse_options)
                job.checkout_revision = checkout.revision
                job.repo_uuid, job.repo_root = self._repository_identity(engine, job.wc_dir)
                if _directory_size(job.job_dir) > self.max_job_bytes:
                    raise RuntimeError("临时工作副本超过单任务容量限制")
                self._check_cancelled(job)

                with job.lock:
                    job.stage_label = "正在匹配标准文件"
                    job.progress = 38
                    job.event("临时检出完成，开始匹配服务端标准来源")
                service = StandardFileService(engine)
                remote_missing = set(checkout.missing_paths)
                source_missing = {
                    relative for relative in job.relative_paths
                    if not self._safe_source_file(source_root, relative)
                }
                missing_paths = remote_missing | source_missing
                valid_paths = [path for path in job.relative_paths if path not in missing_paths]
                if valid_paths:
                    items, parsed_count, _details = service.scan(
                        valid_paths,
                        job.svn_url,
                        str(job.wc_dir),
                        "upgrade",
                        str(source_root),
                        "",
                        allow_existing=True,
                    )
                else:
                    items, parsed_count = [], 0
                if parsed_count != len(valid_paths):
                    raise RuntimeError("部分文件路径无法解析")
                job.preview_items = [{
                    "path": item.rel_path,
                    "source": "客户标准文件",
                    "result": item.status,
                    "detail": item.detail,
                } for item in items]
                for relative in sorted(missing_paths):
                    if relative in remote_missing and relative in source_missing:
                        detail = "SVN 与客户标准目录中均不存在"
                    elif relative in remote_missing:
                        detail = "该文件在当前 SVN 最新版本中不存在"
                    else:
                        detail = "该文件在客户标准目录中不存在"
                    job.preview_items.append({
                        "path": relative,
                        "source": "未匹配",
                        "result": "不处理",
                        "detail": detail,
                    })
                missing = sorted(missing_paths)
                ready = [item for item in items if item.status == "待覆盖"]
                unchanged = [item for item in items if item.status == "内容相同"]
                self._check_cancelled(job)
                if missing:
                    job.blocking_issues.append(
                        "%d 个清单文件并非 SVN 与客户标准目录同时存在，已阻止整个任务" % len(missing))
                    with job.lock:
                        self._check_cancelled(job)
                        job.state = "preview_ready"
                        job.stage_label = "预览存在阻塞项"
                        job.progress = 70
                        job.can_commit = False
                        job.preview_summary = {
                            "requested": len(job.relative_paths), "changed": 0,
                            "unchanged": len(unchanged), "missing": len(missing),
                        }
                        job.expires_at = time.time() + self.preview_ttl
                        job.event("预览已生成；仅允许两端同时存在的文件，任务未执行覆盖")
                        self._clear_credentials(job)
                        self._delete_job_directory(job)
                        job.event("阻塞预览无需保留工作副本，临时目录已清理")
                    return
                if not ready:
                    self._set_no_changes(job)
                    return

                with job.lock:
                    job.stage_label = "正在覆盖并生成预览"
                    job.progress = 58
                    job.event("标准文件已匹配，正在写入临时工作副本")
                self._check_source_capacity(job, ready)
                try:
                    self._check_cancelled(job)
                    covered, errors = service.cover(ready)
                    if errors or len(covered) != len(ready):
                        raise RuntimeError("标准文件覆盖未全部成功，已阻止提交")
                except Exception:
                    self._release_capacity_reservation(job)
                    raise
                self._check_actual_capacity(job)
                self._check_cancelled(job)
                for preview in job.preview_items:
                    if preview["path"] in {item.rel_path for item in covered}:
                        preview["result"] = "已覆盖"

                ok, output, _status = service.prepare_commit(str(job.wc_dir), covered)
                if not ok:
                    if output == "目标目录没有可提交的 SVN 变更" and not missing:
                        self._set_no_changes(job)
                        return
                    raise RuntimeError(output)
                self._check_cancelled(job)
                status_entries = self._read_status_entries(engine, job.wc_dir)
                self._validate_status(job, status_entries)
                fingerprint = self._fingerprint(job, status_entries)
                confirmation_token = secrets.token_urlsafe(32)
                job.status_entries = status_entries
                job.commit_targets = [entry["path"] for entry in status_entries]
                job.preview_fingerprint = fingerprint
                job.confirmation_token = confirmation_token
                job.confirmation_token_hash = (
                    _token_hash(confirmation_token))
                job.preview_summary = {
                    "requested": len(items),
                    "changed": len(covered),
                    "unchanged": len(unchanged),
                    "missing": len(missing),
                }
                self._check_cancelled(job)
                with job.lock:
                    self._check_cancelled(job)
                    job.state = "preview_ready"
                    job.stage_label = "提交预览已就绪"
                    job.progress = 72
                    job.can_commit = True
                    job.expires_at = time.time() + self.preview_ttl
                    job.event("提交预览已生成，等待用户确认")
                if _directory_size(job.job_dir) > self.max_job_bytes:
                    raise RuntimeError("临时工作副本超过单任务容量限制")
            except StandardWebError as exc:
                if exc.code not in {"job_cancelled", "job_expired"}:
                    self._fail_job(job, exc.code, exc.message)
            except Exception as exc:
                self._fail_job(job, "prepare_failed", exc)
            finally:
                self._release_capacity_reservation(job)
                if engine:
                    engine.release_credentials()

    def _authorized_job(self, job_id, access_token):
        if not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise StandardWebError("job_not_found", "任务不存在或已过期", 404)
        with self._jobs_lock:
            job = self.jobs.get(job_id)
        if not job or not secrets.compare_digest(
                job.access_token_hash, _token_hash(access_token)):
            raise StandardWebError("job_not_found", "任务不存在或已过期", 404)
        return job

    def snapshot(self, job_id, access_token):
        job = self._authorized_job(job_id, access_token)
        with job.lock:
            if job.state == "preview_ready" and time.time() > job.expires_at:
                self._expire_job(job)
            task = {
                "id": job.job_id,
                "status": job.state,
                "stage_label": job.stage_label,
                "progress": job.progress,
                "can_commit": job.can_commit,
                "created_at": _iso(job.created_at),
                "expires_at": _iso(job.expires_at),
                "svn_url": job.svn_url,
                "commit_message": job.commit_message,
                "checkout_revision": job.checkout_revision,
                "selection_mode": job.selection_mode,
                "preview": None,
                "result": None,
                "error": job.error,
                "cleanup": {"status": job.cleanup_status},
                "events": list(job.events),
            }
            if job.state in {"preview_ready", "commit_queued", "committing"}:
                task["preview"] = {
                    "confirmation_token": job.confirmation_token if job.state == "preview_ready" else "",
                    "summary": dict(job.preview_summary),
                    "items": list(job.preview_items[:MAX_PREVIEW_ITEMS]),
                    "items_total": len(job.preview_items),
                    "items_truncated": len(job.preview_items) > MAX_PREVIEW_ITEMS,
                    "blocking_issues": list(job.blocking_issues),
                }
            if job.state in {"committed", "no_changes", "commit_unknown"}:
                task["result"] = {
                    "revision": job.revision,
                    "urls": list(job.committed_urls),
                    "paths": list(job.committed_paths),
                    "no_changes": job.state == "no_changes",
                    "uncertain": job.state == "commit_unknown",
                }
            return {"ok": True, "task": task}

    def request_commit(self, job_id, access_token, confirmation_token, idempotency_key):
        job = self._authorized_job(job_id, access_token)
        key = str(idempotency_key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", key):
            raise StandardWebError("invalid_idempotency_key", "提交幂等键格式不正确")
        with job.lock:
            if job.commit_idempotency_key:
                if secrets.compare_digest(job.commit_idempotency_key, key):
                    return self.snapshot(job_id, access_token)
                raise StandardWebError("commit_already_requested", "该任务已经发起过提交", 409)
            if job.state != "preview_ready" or not job.can_commit:
                raise StandardWebError("job_not_ready", "任务当前不能提交", 409)
            if time.time() > job.expires_at:
                self._expire_job(job)
                raise StandardWebError("preview_expired", "提交预览已过期，请重新创建任务", 409)
            if not job.confirmation_token_hash or not secrets.compare_digest(
                    job.confirmation_token_hash, _token_hash(confirmation_token)):
                raise StandardWebError("invalid_confirmation", "提交确认令牌不正确", 403)
            job.commit_idempotency_key = key
            job.confirmation_token_hash = ""
            job.confirmation_token = ""
            job.state = "commit_queued"
            job.stage_label = "等待 SVN 提交"
            job.progress = 78
            job.can_commit = False
            # 用户在有效预览内确认后，从此刻起获得完整的一段提交排队时间，
            # 不沿用可能只剩几秒的预览到期时间。
            job.expires_at = time.time() + self.preview_ttl
            job.event("用户已确认预览，提交请求已进入队列")
        try:
            self._executor.submit(self._commit_job, job_id)
        except RuntimeError:
            self._fail_job(job, "job_queue_unavailable", "提交队列当前不可用，请重新创建任务")
            raise StandardWebError(
                "job_queue_unavailable", "提交队列当前不可用，请重新创建任务", 503) from None
        return self.snapshot(job_id, access_token)

    def _commit_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return
        engine = None
        commit_started = False
        with self._work_slots:
            try:
                self._check_cancelled(job)
                engine = self._new_engine(job)
                with job.lock:
                    job.state = "committing"
                    job.stage_label = "正在复核并提交 SVN"
                    job.progress = 86
                    job.event("正在复核工作副本，预览发生变化将自动阻止提交")
                current_status = self._read_status_entries(engine, job.wc_dir)
                self._validate_status(job, current_status)
                if not secrets.compare_digest(
                        job.preview_fingerprint, self._fingerprint(job, current_status)):
                    raise StandardWebError(
                        "preview_stale", "提交预览已发生变化，已阻止提交", 409)
                service = StandardFileService(engine)
                if hasattr(engine, "reset_commit_tracking"):
                    engine.reset_commit_tracking()
                ok, output, revision, urls, paths = service.commit_selected_paths(
                    str(job.wc_dir), job.commit_targets, job.commit_message)
                commit_started = bool(getattr(engine, "commit_process_started", False))
                if not ok:
                    self._fail_job(job, "commit_failed", output or "SVN 提交失败")
                    return
                with job.lock:
                    job.state = "committed"
                    job.stage_label = "SVN 提交成功"
                    job.progress = 100
                    job.revision = revision
                    job.committed_urls = urls
                    job.committed_paths = paths
                    job.finished_at = time.time()
                    job.event("SVN 提交成功，临时工作副本正在清理")
                    self._clear_credentials(job)
                    self._delete_job_directory(job)
            except TimeoutError as exc:
                commit_started = commit_started or bool(
                    getattr(engine, "commit_process_started", False))
                with job.lock:
                    job.state = "commit_unknown" if commit_started else "failed"
                    job.stage_label = "提交结果待人工核验" if commit_started else "任务失败"
                    job.progress = 100
                    job.error = {
                        "code": "commit_unknown" if commit_started else "commit_timeout",
                        "message": "SVN 提交已启动但返回超时，请勿重复提交；请在仓库日志中核验本次提交说明。"
                        if commit_started else self._safe_error_message(job, exc),
                    }
                    job.finished_at = time.time()
                    job.event("提交结果无法确认，系统不会自动重试")
                    self._clear_credentials(job)
            except StandardWebError as exc:
                if exc.code not in {"job_cancelled", "job_expired"}:
                    self._fail_job(job, exc.code, exc.message)
            except Exception as exc:
                commit_started = commit_started or bool(
                    getattr(engine, "commit_process_started", False))
                if commit_started:
                    with job.lock:
                        job.state = "commit_unknown"
                        job.stage_label = "提交结果待人工核验"
                        job.progress = 100
                        job.error = {
                            "code": "commit_unknown",
                            "message": "SVN 提交已启动但结果无法确认，请勿重复提交；请在仓库日志中核验本次提交说明。",
                        }
                        job.finished_at = time.time()
                        job.event("提交结果无法确认，系统不会自动重试")
                        self._clear_credentials(job)
                else:
                    self._fail_job(job, "commit_failed", exc)
            finally:
                if engine:
                    engine.release_credentials()

    def _expire_job(self, job):
        if job.state != "preview_ready":
            return
        job.state = "expired"
        job.stage_label = "预览已过期"
        job.progress = 100
        job.can_commit = False
        job.finished_at = time.time()
        job.event("预览等待超时，临时目录已清理")
        self._clear_credentials(job)
        self._delete_job_directory(job)

    def cancel(self, job_id, access_token):
        job = self._authorized_job(job_id, access_token)
        with job.lock:
            if job.state in {"commit_queued", "committing", "committed", "commit_unknown"}:
                raise StandardWebError("job_busy", "任务已进入提交阶段，不能取消", 409)
            if job.state in {"cancelled", "expired", "failed", "no_changes"}:
                return self.snapshot(job_id, access_token)
            if job.state == "queued":
                job.cancel_requested = True
                job.state = "cancelled"
                job.stage_label = "已取消"
                job.progress = 100
                job.finished_at = time.time()
                job.event("排队任务已取消，临时目录已清理")
                self._clear_credentials(job)
                self._delete_job_directory(job)
            elif job.state == "preparing":
                job.cancel_requested = True
                job.stage_label = "正在取消"
                job.event("已收到取消请求，将在当前安全步骤结束后清理")
            else:
                job.state = "cancelled"
                job.stage_label = "已取消"
                job.progress = 100
                job.can_commit = False
                job.finished_at = time.time()
                job.event("任务已取消，临时目录已清理")
                self._clear_credentials(job)
                self._delete_job_directory(job)
        return self.snapshot(job_id, access_token)

    def cleanup_expired(self):
        now = time.time()
        remove_ids = []
        with self._jobs_lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            acquired = job.lock.acquire(blocking=False)
            if not acquired:
                continue
            try:
                if job.state == "preview_ready" and now > job.expires_at:
                    self._expire_job(job)
                if job.state in {"queued", "preparing"} and now > job.expires_at:
                    job.cancel_requested = True
                    job.event("任务准备超时，将在当前安全步骤结束后清理")
                if job.state == "commit_queued" and now > job.expires_at:
                    job.cancel_requested = True
                    job.event("提交排队超时，将在获得执行槽后停止并清理")
                if job.state == "commit_unknown" and job.finished_at and now - job.finished_at > 3600:
                    self._delete_job_directory(job)
                if (job.state in TERMINAL_STATES and job.state != "commit_unknown"
                        and job.cleanup_status == "failed"):
                    self._delete_job_directory(job)
                if (job.state in TERMINAL_STATES and job.finished_at
                        and now - job.finished_at > self.result_ttl):
                    self._delete_job_directory(job)
                    remove_ids.append(job.job_id)
            finally:
                job.lock.release()
        if remove_ids:
            with self._jobs_lock:
                for job_id in remove_ids:
                    self.jobs.pop(job_id, None)
        self._cleanup_orphan_directories()
