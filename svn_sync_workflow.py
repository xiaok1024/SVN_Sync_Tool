# -*- coding: utf-8 -*-
"""GUI 可复用的无界面工作流编排。

本模块只组合 :class:`svn_sync_core.SyncEngine` 的能力，不处理任何界面确认。
调用方必须在 checkout 覆盖、文件复制和 SVN 提交前完成相应的用户确认。
"""

import os
import shutil
from dataclasses import dataclass, field


@dataclass
class AutoPipelineResult:
    ok: bool
    message: str
    revision: int | None = None
    urls: list[str] = field(default_factory=list)
    relative_paths: list[str] = field(default_factory=list)
    no_changes: bool = False


def _emit(engine, log, message):
    engine._log(log, message)


def validate_checkout_replacement(path):
    """拒绝删除文件系统根目录或用户主目录等过宽目标。"""
    target = os.path.realpath(os.path.abspath(path or ""))
    anchor = os.path.realpath(os.path.abspath(os.path.splitdrive(target)[0] + os.sep))
    home = os.path.realpath(os.path.expanduser("~"))
    if not path or target in {anchor, home}:
        raise ValueError("拒绝删除过宽的 checkout 目标目录: " + (path or "<空>"))
    if not os.path.isdir(os.path.join(target, ".svn")):
        raise ValueError("拒绝删除非 SVN 工作副本目录: " + target)
    return target


def resolve_and_scan_cross_files(engine, target, source, log=None):
    """检查来源与目标并返回交叉文件条目。"""
    source_error = engine._precheck_source(source)
    if source_error:
        raise RuntimeError(source_error)
    if not os.path.isdir(target):
        raise RuntimeError("目标目录不存在: " + target)
    resolved = engine._resolve_source_path(source, log)
    if not os.path.isdir(resolved):
        raise RuntimeError("来源目录不存在: " + resolved)
    return engine._scan_cross_files(target, resolved)


def run_checkout(engine, url, destination, log=None):
    """执行一次 SVN checkout，返回 ``(成功, 输出)``。"""
    rc, output = engine._run_svn(log, "checkout", url, destination)
    return rc == 0, output


def run_auto_pipeline(engine, url, destination, source, mode, message, log=None, progress=None):
    """执行已由调用方确认的拉取、覆盖和提交工作流。"""
    notify = progress or (lambda _step, _state, _text: None)
    checkout_exists = os.path.isdir(destination) and os.path.isdir(
        os.path.join(destination, ".svn"))

    notify(1, "running", "正在拉取或更新工作副本")
    _emit(engine, log, "【步骤 1/3】SVN 拉取\n")
    _emit(engine, log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    if checkout_exists and mode == "update":
        _emit(engine, log, "目录已存在，执行 svn update...\n")
        rc, _ = engine._run_svn(log, "update", destination)
    else:
        if checkout_exists:
            target = validate_checkout_replacement(destination)
            _emit(engine, log, "checkout 模式：删除已确认的旧工作副本后重新拉取...\n")
            shutil.rmtree(target)
            if os.path.exists(target):
                raise RuntimeError("旧工作副本删除失败: " + target)
        rc, _ = engine._run_svn(log, "checkout", url, destination)
    if rc != 0:
        notify(1, "error", "SVN 拉取失败")
        _emit(engine, log, "\n--- 步骤 1 失败，终止流程 ---\n")
        return AutoPipelineResult(False, "SVN 拉取失败")
    notify(1, "done", "工作副本已就绪")
    _emit(engine, log, "\n--- 步骤 1 完成 ---\n\n")

    notify(2, "running", "正在扫描并覆盖交叉文件")
    _emit(engine, log, "【步骤 2/3】交叉文件覆盖\n")
    _emit(engine, log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    resolved = engine._resolve_source_path(source, log)
    if not os.path.isdir(resolved):
        notify(2, "error", "来源目录不可用")
        _emit(engine, log, "来源目录不存在: %s\n--- 步骤 2 失败，终止流程 ---\n" % resolved)
        return AutoPipelineResult(False, "来源目录不存在: " + resolved)
    entries = engine._scan_cross_files(destination, resolved)
    if not entries:
        _emit(engine, log, "未找到交叉文件，跳过覆盖\n")
    else:
        def report(relative, success, error):
            text = "  [覆盖] " + relative if success else "  [失败] %s - %s" % (relative, error)
            _emit(engine, log, text + "\n")

        copied, errors = engine._copy_cross_files(entries, on_result=report)
        _emit(engine, log, "覆盖结果: 成功 %d 个%s\n" % (
            len(copied), ", 失败 %d 个" % len(errors) if errors else ""))
        if errors:
            notify(2, "error", "部分文件覆盖失败")
            _emit(engine, log, "检测到覆盖失败，为避免提交不完整变更，终止流程。\n")
            return AutoPipelineResult(False, "部分文件覆盖失败，未执行 SVN 提交")
    notify(2, "done", "交叉文件覆盖完成")
    _emit(engine, log, "\n--- 步骤 2 完成 ---\n\n")

    notify(3, "running", "正在检查并提交 SVN 变更")
    _emit(engine, log, "【步骤 3/3】SVN 提交\n")
    _emit(engine, log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    if not engine._unlock_svn_locks_before_commit(log, destination):
        notify(3, "error", "提交前解锁失败")
        _emit(engine, log, "\n--- 提交前解锁失败，终止流程 ---\n")
        return AutoPipelineResult(False, "提交前解锁失败")
    _emit(engine, log, "检查变更状态...\n")
    rc, status_output = engine._run_svn(log, "status", destination)
    if rc != 0:
        notify(3, "error", "读取 SVN 状态失败")
        return AutoPipelineResult(False, "读取 SVN 状态失败")
    changed = [line for line in status_output.splitlines()
               if line.strip() and ".svn" not in line]
    if not changed:
        _emit(engine, log, "无变更需要提交\n")
        revision = engine._get_wc_last_revision(destination)
        urls, relative_paths = (engine._get_revision_urls(destination, revision)
                                if revision else ([], []))
        if revision:
            _emit(engine, log, "（无新增提交，导出当前版本 %d 的文件路径）\n" % revision)
        notify(3, "done", "没有新变更，无需提交")
        return AutoPipelineResult(
            True, "没有新变更，无需提交", revision, urls, relative_paths,
            no_changes=True)

    _emit(engine, log, "共 %d 个文件有变更，正在提交...\n" % len(changed))
    rc, commit_output = engine._run_svn(log, "commit", destination, "-m", message)
    if rc != 0:
        notify(3, "error", "SVN 提交失败")
        _emit(engine, log, "\n--- 提交失败，返回码: %d ---\n" % rc)
        return AutoPipelineResult(False, "SVN 提交失败")
    revision = engine._parse_revision(commit_output)
    urls, relative_paths = (engine._get_revision_urls(destination, revision)
                            if revision else ([], []))
    _emit(engine, log, "\n--- 提交成功！---\n")
    if revision:
        _emit(engine, log, "版本号: %d\n" % revision)
    notify(3, "done", "SVN 提交完成")
    return AutoPipelineResult(True, "全自动流程执行完成", revision, urls, relative_paths)
