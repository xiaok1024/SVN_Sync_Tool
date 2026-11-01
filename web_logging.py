"""Web 诊断日志：显式字段、JSON Lines、有限轮转，不记录请求或异常原文。"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
import traceback
from datetime import datetime, timezone

from svn_sync_core import redact_sensitive_text


LOGGER = logging.getLogger("svn_sync.audit")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
FIELDS = frozenset({
    "job_id", "request_id", "actor", "state", "stage", "message", "revision",
    "checkout_revision", "cleanup_status", "attempt", "duration_ms", "elapsed_ms",
    "stage_elapsed_ms", "file_count", "changed_count", "error_code", "error_type",
    "errno", "frames", "method", "route", "status", "reason",
})
_commit = ""


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def purge_expired(self):
        cutoff = time.time() - 30 * 24 * 3600
        path = Path(self.baseFilename)
        for candidate in path.parent.glob(path.name + ".*"):
            if (candidate.name.removeprefix(path.name + ".").isdigit()
                    and not candidate.is_symlink() and candidate.stat().st_mtime < cutoff):
                candidate.unlink()

    def doRollover(self):
        super().doRollover()
        self.purge_expired()

    def handleError(self, record):
        # logging 默认会把 record 和 traceback 输出到 stderr；这里仅报告固定提示。
        try:
            sys.stderr.write("SVN Sync diagnostic log write failed; check log disk and permissions.\n")
        except OSError:
            pass


def configure_logging(directory=None, commit="", max_bytes=50 * 1024 * 1024, backups=30):
    global _commit
    if directory is None:
        directory = os.environ.get("SVN_SYNC_WEB_LOG_DIR")
    if not directory:
        directory = (Path("/var/log/svn-sync-tool")
                     if sys.platform.startswith("linux") and os.geteuid() == 0
                     else Path.home() / ".local/state/svn-sync-tool/log")
    root = Path(directory).expanduser().resolve()
    project = Path(__file__).resolve().parent
    if root == project or project in root.parents:
        raise ValueError("Web 日志目录必须位于部署目录之外")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateRotatingHandler(root / "web.jsonl", maxBytes=max_bytes,
                                     backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.purge_expired()
    for old in list(LOGGER.handlers):
        LOGGER.removeHandler(old)
        old.close()
    _commit = commit if re.fullmatch(r"[a-f0-9]{7,40}", commit) else "development"
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return root / "web.jsonl"


def exception_fields(exc):
    """保留异常类型、errno 和代码位置，舍弃可能携带凭据的消息、源码和局部变量。"""
    if not isinstance(exc, BaseException):
        return {}
    frames = traceback.extract_tb(exc.__traceback__)[-12:]
    return {"error_type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "frames": [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                       for frame in frames]}


def log_event(event, *, level=logging.INFO, secrets=(), **fields):
    if not LOGGER.isEnabledFor(level):
        return
    payload = {"time": datetime.now(timezone.utc).isoformat(),
               "level": logging.getLevelName(level), "event": event,
               "pid": os.getpid(), "commit": _commit or "development"}
    for key, value in fields.items():
        if key not in FIELDS:
            continue
        if isinstance(value, str):
            value = redact_sensitive_text(value, tuple(s for s in secrets if s))[:1000]
        payload[key] = value
    LOGGER.log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
