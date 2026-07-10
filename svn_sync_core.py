# -*- coding: utf-8 -*-
"""无界面的 SVN 同步核心与平台适配逻辑。"""

import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

SVN_EXECUTABLE = shutil.which("svn")
if not SVN_EXECUTABLE:
    for _candidate in ("/opt/homebrew/bin/svn", "/usr/local/bin/svn", "/usr/bin/svn"):
        if os.path.isfile(_candidate) and os.access(_candidate, os.X_OK):
            SVN_EXECUTABLE = _candidate
            break
SVN_EXECUTABLE = SVN_EXECUTABLE or "svn"

_SYS_ENC = locale.getpreferredencoding()
SVN_ENCODING = "gbk" if _SYS_ENC.lower() in ("cp936", "gbk", "gb2312", "gb18030") else "utf-8"


def decode_svn_output(data, force_utf8=False):
    """兼容新版 SVN 的 UTF-8 输出和旧版中文 Windows 的本地编码输出。"""
    if not data:
        return ""
    encodings = ["utf-8"] if force_utf8 else ["utf-8", SVN_ENCODING, "gb18030"]
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(SVN_ENCODING, errors="replace")


def _value(value):
    return value.get() if hasattr(value, "get") else value


class SyncEngine:
    """GUI 与 CLI 共用的无界面业务引擎。"""

    def __init__(self):
        self.svn_user = ""
        self.svn_pass = ""
        self.smb_user = ""
        self.smb_pass = ""
        self._temp_mounts = []

    def _log(self, _widget, _message):
        pass

    def _build_svn_cmd(self, *args):
        cmd = [SVN_EXECUTABLE, "--non-interactive",
               "--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,not-yet-valid,other"]
        user = str(_value(self.svn_user) or "").strip()
        password = str(_value(self.svn_pass) or "").strip()
        if user:
            cmd.extend(["--username", user])
            if password:
                cmd.extend(["--password", password])
            else:
                cmd.append("--no-auth-cache")
        cmd.extend(args)
        return cmd

    def _svn_env(self):
        env = os.environ.copy()
        if not IS_WINDOWS:
            env.update({"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8", "LC_CTYPE": "zh_CN.UTF-8"})
            extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env["PATH"] = extra + ((":" + env["PATH"]) if env.get("PATH") else "")
        return env

    def _run_svn(self, log_widget, *args):
        cmd = self._build_svn_cmd(*args)
        safe_cmd = ["******" if i and cmd[i - 1] == "--password" else part for i, part in enumerate(cmd)]
        self._log(log_widget, ">> " + " ".join(safe_cmd) + "\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=self._svn_env(), creationflags=CREATE_NO_WINDOW)
        lines = []
        try:
            for raw_line in proc.stdout:
                line = decode_svn_output(raw_line)
                self._log(log_widget, line)
                lines.append(line)
        finally:
            if proc.stdout:
                proc.stdout.close()
        proc.wait()
        return proc.returncode, "".join(lines)

    def _run_svn_bytes(self, *args, force_utf8=False, cwd=None, timeout=30):
        proc = subprocess.Popen(self._build_svn_cmd(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self._svn_env(), cwd=cwd, creationflags=CREATE_NO_WINDOW)
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, decode_svn_output(out + err, force_utf8=force_utf8)

    def _find_locked_svn_paths(self, checkout_dir):
        rc, out = self._run_svn_bytes("status", "-u", "--xml", ".", force_utf8=True, cwd=checkout_dir)
        if rc != 0:
            raise RuntimeError(out.strip() or "svn status -u --xml 执行失败")
        locked, seen = [], set()
        for entry in ET.fromstring(out).findall(".//entry"):
            status = entry.find("wc-status")
            repos = entry.find("repos-status")
            if not ((status is not None and status.find("lock") is not None) or
                    (repos is not None and repos.find("lock") is not None)):
                continue
            path = (entry.get("path") or "").strip()
            target = os.path.abspath(os.path.join(checkout_dir, path))
            if path and path != "." and target not in seen:
                locked.append(target)
                seen.add(target)
        return locked

    def _unlock_svn_locks_before_commit(self, log_widget, checkout_dir):
        self._log(log_widget, "检查 SVN 锁状态...\n")
        try:
            locked = self._find_locked_svn_paths(checkout_dir)
        except Exception as exc:
            self._log(log_widget, "检查 SVN 锁状态失败: %s\n" % exc)
            return False
        if not locked:
            self._log(log_widget, "未发现 SVN 上锁文件\n")
            return True
        targets_file = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False) as handle:
                targets_file = handle.name
                for path in locked:
                    handle.write(path + "\n")
            rc, _ = self._run_svn(log_widget, "unlock", "--force", "--targets", targets_file)
            return rc == 0
        finally:
            if targets_file:
                try:
                    os.remove(targets_file)
                except OSError:
                    pass

    @staticmethod
    def _clean_share_text(path):
        value = (path or "").strip()
        for prefix in ("标准文件请到", "标准文件在", "请到", "文件请到"):
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
                break
        for suffix in ("下面提取", "里提取", "中提取", "提取", "下载"):
            if value.endswith(suffix):
                value = value[:-len(suffix)].strip()
                break
        return value

    def _is_share_address(self, path):
        value = self._clean_share_text(path)
        return value.lower().startswith(("smb://", "smb:")) or value.startswith(("\\\\", "//"))

    @staticmethod
    def _share_to_smb_url(path):
        value = path.strip()
        if value.lower().startswith("smb://"):
            return value
        if value.lower().startswith("smb:"):
            return "smb://" + value[4:].lstrip("/")
        return "smb://" + value.replace("\\", "/").lstrip("/")

    @staticmethod
    def _share_to_unc(path):
        value = path.strip()
        if value.lower().startswith("smb://"):
            value = value[6:]
        elif value.lower().startswith("smb:"):
            value = value[4:].lstrip("/")
        return "\\\\" + value.replace("/", "\\").lstrip("\\")

    def _precheck_source(self, source):
        if not source:
            return "请先选择/填写来源目录"
        if not self._is_share_address(source) and not os.path.isdir(source):
            return "来源目录不存在: " + source
        return None

    def _resolve_source_path(self, address, log=None):
        raw = (address or "").strip()
        if not self._is_share_address(raw):
            return raw
        address = self._clean_share_text(raw)
        if IS_WINDOWS:
            return self._share_to_unc(address)
        if IS_MACOS:
            return self._mount_smb_macos(address, log)
        return address

    def _find_existing_smb_mount(self, server, rel_path):
        normalize = lambda parts: [unicodedata.normalize("NFC", part) for part in parts if part]
        target = normalize(rel_path.split("/"))
        try:
            output = subprocess.run(["mount"], capture_output=True, text=True).stdout
        except Exception:
            return None
        for line in (output or "").splitlines():
            if "smbfs" not in line or " on " not in line:
                continue
            source, rest = line.split(" on ", 1)
            mount_path = rest.split(" (", 1)[0].strip()
            body = source.strip()[2:] if source.strip().startswith("//") else ""
            slash = body.find("/")
            if slash < 0 or body[:slash].split("@")[-1].lower() != server.lower():
                continue
            mounted = normalize(urllib.parse.unquote(body[slash + 1:]).split("/"))
            if mounted and target[:len(mounted)] == mounted:
                remaining = target[len(mounted):]
                return os.path.join(mount_path, *remaining) if remaining else mount_path
        return None

    def _mount_smb_macos(self, address, log=None):
        smb_url = self._share_to_smb_url(address)
        raw = smb_url[6:]
        if "/" not in raw:
            raise ValueError("SMB 地址需包含 server/share：" + address)
        server, rel = raw.split("/", 1)
        server = server.split("@")[-1]
        share, _, sub = rel.partition("/")
        existing = self._find_existing_smb_mount(server, rel)
        if existing and os.path.isdir(existing):
            return existing
        mount_point = tempfile.mkdtemp(prefix="svn_sync_smb_")
        user = str(_value(self.smb_user) or "").strip()
        password = str(_value(self.smb_pass) or "").strip()
        auth = urllib.parse.quote(user, safe="")
        if password:
            auth += ":" + urllib.parse.quote(password, safe="")
        source = "//%s@%s/%s" % (auth, server, share) if user else "//%s/%s" % (server, share)
        if log:
            self._log(log, "正在挂载 SMB: //%s/%s -> %s\n" % (server, share, mount_point))
        try:
            cmd = ["mount_smbfs", source, mount_point] if user else ["mount_smbfs", "-N", source, mount_point]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self._safe_rmdir(mount_point)
            raise RuntimeError("挂载超时，请检查网络与共享地址")
        if result.returncode != 0:
            self._safe_rmdir(mount_point)
            raise RuntimeError("挂载失败（%s）：%s" % (smb_url, (result.stderr or "").strip()))
        self._temp_mounts.append(mount_point)
        full = os.path.join(mount_point, *sub.split("/")) if sub else mount_point
        if not os.path.isdir(full):
            raise RuntimeError("挂载成功但子目录不存在: " + sub)
        return full

    @staticmethod
    def _safe_rmdir(path):
        try:
            os.rmdir(path)
        except OSError:
            pass

    def _cleanup_temp_mounts(self):
        for mount_point in list(self._temp_mounts):
            subprocess.run(["umount", mount_point], capture_output=True, check=False)
            self._safe_rmdir(mount_point)
        self._temp_mounts = []

    @staticmethod
    def _scan_cross_files(target, source):
        results = []
        target_path, source_path = Path(target), Path(source)
        for file_path in sorted(target_path.rglob("*")):
            if not file_path.is_file() or ".svn" in file_path.parts or ".git" in file_path.parts:
                continue
            relative = file_path.relative_to(target_path)
            source_file = source_path / relative
            if source_file.is_file():
                results.append((str(relative), str(source_file), str(file_path)))
        return results

    @staticmethod
    def _parse_revision(output):
        match = re.search(r"(?:Committed revision|提交后的版本为?|已提交的版本|版本)\s*(\d+)", output)
        return int(match.group(1)) if match else None

    def _get_wc_last_revision(self, checkout_dir):
        rc, output = self._run_svn_bytes("info", "--show-item", "revision", checkout_dir)
        if rc == 0 and output.strip().isdigit():
            return int(output.strip())
        return None

    def _get_repo_root_http_url(self, checkout_dir):
        rc, output = self._run_svn_bytes("info", "--xml", checkout_dir, force_utf8=True)
        if rc != 0:
            return None
        root = ET.fromstring(output).findtext(".//repository/root")
        return root.rstrip("/") if root else None

    def _get_changed_paths(self, checkout_dir, revision):
        rc, output = self._run_svn_bytes("log", "--xml", "-v", "-r", str(revision), checkout_dir,
                                         force_utf8=True)
        if rc != 0:
            return []
        return [node.text.strip() for node in ET.fromstring(output).findall(".//paths/path")
                if node.text and node.get("kind") == "file"]
