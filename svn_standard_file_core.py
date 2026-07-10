# -*- coding: utf-8 -*-
"""标准文件获取的无界面业务逻辑。"""

import filecmp
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass
class StandardFileItem:
    rel_path: str
    source_file: str | None
    source_label: str
    target_file: str
    target_exists: bool
    status: str
    detail: str


def extract_relative_path(url_or_path, svn_root=""):
    text = (url_or_path or "").strip()
    if not text:
        return None
    text = re.sub(r"^\[(?:red|black)\]\s*", "", text, flags=re.I)
    text = re.sub(r"\([Vv]\d+\)\s*$", "", text).strip()
    root = (svn_root or "").strip().rstrip("/")
    if root and text.startswith(root + "/"):
        relative = text[len(root) + 1:]
    elif re.match(r"https?://", text, re.I):
        match = re.search(r"/svn/[^/]+/(.*)", text)
        if not match:
            return None
        relative = match.group(1)
    else:
        relative = text.lstrip("/\\")
    relative = relative.replace("\\", "/")
    normalized = posixpath.normpath(relative)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return normalized


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
        for line in lines:
            if not line or line.startswith(("#", "//")) or re.match(r"^\s*\[black\]", line, re.I):
                continue
            relative = extract_relative_path(line, svn_root)
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
            results.append(StandardFileItem(relative, found, label, target_file, target_exists, status, detail))
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
        rc, status_out = self.engine._run_svn_bytes("status", target_root)
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
        repo_root = self.engine._get_repo_root_http_url(target_dir)
        changed = self.engine._get_changed_paths(target_dir, revision)
        decoded_paths = [unquote(path, encoding="utf-8", errors="replace") for path in changed]
        urls = [(repo_root.rstrip("/") + (path if path.startswith("/") else "/" + path) + "(V%d)" % revision)
                for path in decoded_paths] if repo_root else []
        return True, commit_out, revision, urls, [path.lstrip("/") for path in decoded_paths]

    def commit(self, target_dir, covered_items, message):
        """非交互兼容入口；GUI/CLI 应优先分别调用 prepare_commit 和 commit_working_copy。"""
        ok, output, _status = self.prepare_commit(target_dir, covered_items)
        if not ok:
            return False, output, None, [], []
        return self.commit_working_copy(target_dir, message)
