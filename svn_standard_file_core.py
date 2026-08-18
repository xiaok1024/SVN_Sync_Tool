# -*- coding: utf-8 -*-
"""标准文件获取的无界面业务逻辑。"""

import filecmp
import os
import posixpath
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass


@dataclass
class StandardFileItem:
    rel_path: str
    source_file: str | None
    source_label: str
    target_file: str
    target_exists: bool
    status: str
    detail: str
    local_source_file: str | None = None


def _normalize_relative_path(relative):
    value = (relative or "").replace("\\", "/")
    normalized = posixpath.normpath(value)
    if (normalized in ("", ".", "..") or normalized.startswith("../")
            or normalized.startswith("/")):
        return None
    return normalized


def parse_repository_relative_input(value, svn_root=""):
    """把 ``$/仓库/路径`` 严格映射到当前 checkout 根，拒绝其他仓库/子根。"""
    text = (value or "").strip()
    if not text.startswith("$/"):
        return None
    relative = urllib.parse.unquote(text[2:].lstrip("/"))
    root = (svn_root or "").strip()
    if root:
        root_path = urllib.parse.unquote(urllib.parse.urlsplit(root).path).strip("/")
        root_parts = [part for part in root_path.split("/") if part]
        svn_indexes = [
            index for index, part in enumerate(root_parts) if part.lower() == "svn"]
        if not svn_indexes or svn_indexes[0] + 1 >= len(root_parts):
            return None
        checkout_from_repo = "/".join(root_parts[svn_indexes[0] + 1:])
        if relative == checkout_from_repo:
            relative = ""
        elif relative.startswith(checkout_from_repo + "/"):
            relative = relative[len(checkout_from_repo) + 1:]
        else:
            return None
    return _normalize_relative_path(relative)


def parse_file_input(url_or_path, svn_root=""):
    """解析清单输入，返回 ``(ecology 相对路径, 本地源文件)``。

    只有绝对文件系统路径中的 ``ecology`` 目录段会被识别为本地源文件；
    HTTP SVN URL 即使包含 ``/ecology/`` 也不会误判为本地路径。
    """
    text = (url_or_path or "").strip()
    if not text:
        return None, None
    text = re.sub(r"^\[(?:red|black)\]\s*", "", text, flags=re.I)
    text = re.sub(r"\([Vv]\d+\)(?:\s*[-—].*)?\s*$", "", text).strip()

    if text.startswith("$/"):
        return parse_repository_relative_input(text, svn_root), None

    is_windows_absolute = bool(re.match(r"^[A-Za-z]:[\\/]", text))
    is_unc = text.startswith(("\\\\", "//")) and not re.match(r"^//[^/]+/svn/", text, re.I)
    is_local_absolute = os.path.isabs(text) or is_windows_absolute or is_unc
    if is_local_absolute and not re.match(r"https?://", text, re.I):
        ecology_match = re.search(r"(?:^|[\\/])ecology[\\/](.+)$", text, re.I)
        if ecology_match:
            relative = _normalize_relative_path(ecology_match.group(1))
            return relative, text if relative else None

    root = (svn_root or "").strip().rstrip("/")
    if re.match(r"https?://", text, re.I):
        parsed_text = urllib.parse.urlsplit(text)
        decoded_path = urllib.parse.unquote(parsed_text.path)
        parsed_root = urllib.parse.urlsplit(root) if root else None
        decoded_root_path = urllib.parse.unquote(parsed_root.path).rstrip("/") if parsed_root else ""
        same_origin = bool(parsed_root) and (
            parsed_text.scheme.lower(), parsed_text.hostname, parsed_text.port
        ) == (
            parsed_root.scheme.lower(), parsed_root.hostname, parsed_root.port
        )
        if same_origin and decoded_root_path and decoded_path.startswith(decoded_root_path + "/"):
            relative = decoded_path[len(decoded_root_path) + 1:]
        else:
            match = re.search(r"/svn/[^/]+/(.*)", decoded_path)
            if not match:
                return None, None
            relative = match.group(1)
    elif root and text.startswith(root + "/"):
        relative = text[len(root) + 1:]
    else:
        relative = text.lstrip("/\\")
    return _normalize_relative_path(relative), None


def extract_relative_path(url_or_path, svn_root=""):
    return parse_file_input(url_or_path, svn_root)[0]


def iter_standard_file_lines(lines):
    """过滤清单中的标题/黑色上下文行，产出需要处理的文件行。

    同时兼容 ``[red] URL`` 与颜色标记单独占一行的升级清单格式。
    未标色的普通文件行保持既有行为，继续参与标准文件扫描。
    """
    pending_color = None
    for raw_line in lines:
        line = (raw_line or "").strip()
        if not line or line.startswith(("#", "//")) or re.match(r"^QC\d+\b", line, re.I):
            continue
        color_only = re.fullmatch(r"\[(red|black)\]", line, re.I)
        if color_only:
            pending_color = color_only.group(1).lower()
            continue
        inline_color = re.match(r"^\[(red|black)\]\s*", line, re.I)
        color = inline_color.group(1).lower() if inline_color else pending_color
        pending_color = None
        if color == "black":
            continue
        yield line


def _safe_child(root, relative):
    root_abs = os.path.realpath(os.path.abspath(root))
    child = os.path.realpath(os.path.abspath(os.path.join(root_abs, *relative.split("/"))))
    try:
        inside = os.path.commonpath([root_abs, child]) == root_abs
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("路径超出允许目录: " + relative)
    return child


class StandardFileService:
    def __init__(self, engine):
        self.engine = engine

    def scan(self, lines, svn_root, target_dir, task_mode, standard_path, historical_path,
             allow_existing=True, log=None):
        sources = []
        if task_mode == "upgrade" and standard_path:
            sources.append((self.engine._resolve_source_path(standard_path, log), "标准文件"))
        if historical_path:
            sources.append((self.engine._resolve_source_path(historical_path, log), "历史文件"))
        results, details, parsed_count = [], [], 0
        for line in iter_standard_file_lines(lines):
            relative, local_source_file = parse_file_input(line, svn_root)
            if not relative:
                details.append("  [SKIP] 非法或无法识别的路径: %s" % line)
                continue
            parsed_count += 1
            target_file = _safe_child(target_dir, relative)
            found, label = None, ""
            for source_root, source_label in sources:
                for candidate_rel in ("ecology/" + relative, relative):
                    candidate = _safe_child(source_root, candidate_rel)
                    if os.path.isfile(candidate):
                        found, label = candidate, source_label
                        details.append("  [%s] %s" % (source_label, candidate))
                        break
                if found:
                    break
            target_exists = os.path.exists(target_file)
            same_content = False
            if found and target_exists and os.path.isfile(target_file):
                try:
                    same_content = filecmp.cmp(found, target_file, shallow=False)
                except OSError:
                    pass
            if found and same_content:
                status, detail = "内容相同", "目标文件与来源内容一致，无需覆盖"
            elif found and (allow_existing or not target_exists):
                status, detail = "待覆盖", "<- " + label
            elif found:
                status, detail = "跳过(目标已存在)", "需勾选允许覆盖"
            else:
                status, detail = "未找到来源", "来源目录中不存在"
            results.append(StandardFileItem(
                relative, found, label, target_file, target_exists, status, detail,
                local_source_file=local_source_file))
        return results, parsed_count, details

    @staticmethod
    def cover(items):
        covered, errors = [], []
        for item in items:
            if item.status != "待覆盖" or not item.source_file:
                continue
            try:
                os.makedirs(os.path.dirname(item.target_file), exist_ok=True)
                shutil.copy2(item.source_file, item.target_file)
                item.status = "已覆盖"
                item.target_exists = True
                covered.append(item)
            except Exception as exc:
                errors.append("%s: %s" % (item.rel_path, exc))
        return covered, errors

    @staticmethod
    def overwrite_from_local_sources(target_dir, items):
        """用清单中的本地文件覆盖工作副本；此步骤不会执行 SVN 提交。"""
        copied, errors = [], []
        for item in items:
            if item.status != "已覆盖" or not item.local_source_file:
                continue
            try:
                expected_target = _safe_child(target_dir, item.rel_path)
                if os.path.realpath(item.target_file) != os.path.realpath(expected_target):
                    raise ValueError("目标文件与清单相对路径不一致")
                if not os.path.isfile(item.local_source_file):
                    raise FileNotFoundError("本地文件不存在")
                os.makedirs(os.path.dirname(expected_target), exist_ok=True)
                shutil.copy2(item.local_source_file, expected_target)
                item.status = "已覆盖本地"
                item.detail = "<- 本地源文件（未提交）"
                copied.append(item)
            except Exception as exc:
                errors.append("%s: %s" % (item.rel_path, exc))
        return copied, errors

    def prepare_commit(self, target_dir, covered_items):
        """只登记本次覆盖文件，然后返回整个目标目录的待提交状态供用户确认。"""
        paths = [item.target_file for item in covered_items if item.status == "已覆盖"]
        if not paths:
            return False, "没有本次覆盖的文件可提交", ""
        target_root = os.path.realpath(os.path.abspath(target_dir))
        rc, info_out = self.engine._run_svn_bytes(
            "info", "--xml", ".", force_utf8=True, cwd=target_root)
        if rc != 0:
            return False, "目标目录不是有效的 SVN 工作副本: %s\n%s" % (target_root, info_out.strip()), ""
        for path in paths:
            if os.path.commonpath([target_root, os.path.realpath(path)]) != target_root:
                raise ValueError("拒绝提交目标目录外的文件: " + path)
        add_outputs = []
        for path in paths:
            rc, add_out = self.engine._run_svn(None, "add", "--parents", "--force", path)
            add_outputs.append(add_out)
            if rc != 0:
                return False, "登记本次覆盖文件失败: %s\n%s" % (path, add_out), ""
        rc, status_out = self.engine._run_svn_bytes("status", ".", cwd=target_root)
        if rc != 0:
            return False, "读取待提交清单失败: " + status_out, ""
        if not status_out.strip():
            return False, "目标目录没有可提交的 SVN 变更", ""
        return True, "".join(add_outputs), status_out

    def commit_working_copy(self, target_dir, message):
        """兼容模式：提交整个目标目录；未版本控制且未 add 的文件不会被提交。"""
        target_root = os.path.realpath(os.path.abspath(target_dir))
        rc, commit_out = self.engine._run_svn(None, "commit", target_root, "-m", message)
        if rc != 0:
            return False, commit_out, None, [], []
        revision = self.engine._parse_revision(commit_out)
        if not revision:
            return True, commit_out, None, [], []
        urls, relative_paths = self.engine._get_revision_urls(target_dir, revision)
        return True, commit_out, revision, urls, relative_paths

    def commit_selected_paths(self, target_dir, relative_paths, message):
        """提交经过调用方预览确认的精确路径集合。"""
        target_root = os.path.realpath(os.path.abspath(target_dir))
        safe_targets = []
        for relative in dict.fromkeys(relative_paths):
            normalized = _normalize_relative_path(relative)
            if not normalized:
                raise ValueError("拒绝提交非法相对路径: " + str(relative))
            safe_targets.append(_safe_child(target_root, normalized))
        if not safe_targets:
            return False, "没有已确认的 SVN 路径可提交", None, [], []

        targets_file = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", newline="\n", delete=False) as handle:
                targets_file = handle.name
                for path in safe_targets:
                    handle.write(path + "\n")
            rc, commit_out = self.engine._run_svn(
                None, "commit", "--targets", targets_file, "-m", message)
        finally:
            if targets_file:
                try:
                    os.remove(targets_file)
                except OSError:
                    pass
        if rc != 0:
            return False, commit_out, None, [], []
        revision = self.engine._parse_revision(commit_out)
        if not revision:
            return True, commit_out, None, [], []
        try:
            urls, committed_paths = self.engine._get_revision_urls(target_dir, revision)
        except (OSError, RuntimeError, TimeoutError):
            # 提交已明确成功时，后续 URL 查询失败不能把结果误判成提交未知。
            urls, committed_paths = [], []
        return True, commit_out, revision, urls, committed_paths

    def commit(self, target_dir, covered_items, message):
        """非交互兼容入口；GUI/CLI 应优先分别调用 prepare_commit 和 commit_working_copy。"""
        ok, output, _status = self.prepare_commit(target_dir, covered_items)
        if not ok:
            return False, output, None, [], []
        return self.commit_working_copy(target_dir, message)
