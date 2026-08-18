# -*- coding: utf-8 -*-
"""升级清单 Web 入口的无状态业务适配层。

本模块只负责输入限制、错误映射和响应组装。红黑颜色识别、QC 分组、
版本去重和 Markdown 生成始终复用 :mod:`upgrade_list_core`。
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser

import upgrade_list_core as core


MAX_HTML_BYTES = 1024 * 1024
MAX_LIST_BYTES = 1024 * 1024
MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
MAX_HTML_TAGS = 20_000
MAX_STYLE_BYTES = 256 * 1024
MAX_CSS_RULES = 2_000
MAX_SELECTOR_CHECKS = 2_000_000
MAX_SELECTOR_BYTES = 4 * 1024
MAX_TOTAL_SELECTOR_BYTES = 64 * 1024
MAX_SELECTOR_SCAN_BYTES = 64 * 1024 * 1024
MAX_LIST_LINES = 5_000
MAX_LINE_BYTES = 8 * 1024
MAX_DOWNLOAD_FILENAME_BYTES = 240
INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')


@dataclass(frozen=True)
class UpgradeWebError(Exception):
    """可安全返回给浏览器的业务错误。"""

    code: str
    message: str
    status_code: int = 422

    def __str__(self):
        return self.message


def _utf8_size(value):
    return len(value.encode("utf-8"))


def _validate_text(value, field_name, max_bytes):
    if not isinstance(value, str):
        raise UpgradeWebError("invalid_field", "%s 必须是文本" % field_name)
    if not value.strip():
        raise UpgradeWebError("empty_input", "%s不能为空" % field_name)
    if "\x00" in value:
        raise UpgradeWebError("invalid_character", "%s包含不支持的空字符" % field_name)
    if _utf8_size(value) > max_bytes:
        raise UpgradeWebError(
            "input_too_large",
            "%s超过大小限制" % field_name,
            status_code=413,
        )


class _StyleShapeParser(HTMLParser):
    """以线性扫描累计 style 内容，并拒绝未闭合的 style 标签。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_style = False
        self.style_blocks = []
        self.style_parts = []
        self.style_bytes = 0

    def _append_style(self, value):
        if not self.in_style or not value:
            return
        self.style_bytes += _utf8_size(value)
        if self.style_bytes > MAX_STYLE_BYTES:
            raise UpgradeWebError("html_too_complex", "HTML 样式内容超过限制", status_code=413)
        self.style_parts.append(value)

    def handle_starttag(self, tag, _attrs):
        if tag.lower() == "style":
            self.in_style = True
            self.style_parts = []

    def handle_endtag(self, tag):
        if tag.lower() == "style" and self.in_style:
            self.style_blocks.append("".join(self.style_parts))
            self.in_style = False
            self.style_parts = []

    def handle_data(self, data):
        self._append_style(data)

    def handle_entityref(self, name):
        self._append_style("&%s;" % name)

    def handle_charref(self, name):
        self._append_style("&#%s;" % name)


def _validate_html_shape(html):
    # 每个真实标签至少包含一个 "<"；先用 C 层线性计数挡住大量未闭合标记，
    # 避免对不可信 HTML 使用跨全文回溯正则。
    tag_count = html.count("<")
    if tag_count > MAX_HTML_TAGS:
        raise UpgradeWebError("html_too_complex", "HTML 标签数量超过限制", status_code=413)
    parser = _StyleShapeParser()
    parser.feed(html)
    parser.close()
    if parser.in_style:
        raise UpgradeWebError("html_too_complex", "HTML 样式标签未闭合", status_code=413)
    style_blocks = parser.style_blocks
    css_rules = [
        rule
        for style_text in style_blocks
        for rule in core.rt_parse_css_color_rules(style_text)
    ]
    css_rule_count = len(css_rules)
    if css_rule_count > MAX_CSS_RULES:
        raise UpgradeWebError("html_too_complex", "HTML 样式规则数量超过限制", status_code=413)
    selector_sizes = [_utf8_size(selector) for selector, _color in css_rules]
    if any(size > MAX_SELECTOR_BYTES for size in selector_sizes):
        raise UpgradeWebError("html_too_complex", "HTML 存在过长的样式选择器", status_code=413)
    total_selector_bytes = sum(selector_sizes)
    if total_selector_bytes > MAX_TOTAL_SELECTOR_BYTES:
        raise UpgradeWebError("html_too_complex", "HTML 样式选择器总长度超过限制", status_code=413)
    # 核心会为每个标签检查匹配的颜色规则，并在解析 <style> 时追加一次规则。
    # 限制组合工作量，避免标签数和规则数各自合规但乘积过大。
    selector_checks = tag_count * max(1, css_rule_count * 2)
    if selector_checks > MAX_SELECTOR_CHECKS:
        raise UpgradeWebError("html_too_complex", "HTML 样式匹配复杂度超过限制", status_code=413)
    selector_scan_bytes = tag_count * max(1, total_selector_bytes * 2)
    if selector_scan_bytes > MAX_SELECTOR_SCAN_BYTES:
        raise UpgradeWebError("html_too_complex", "HTML 样式扫描量超过限制", status_code=413)


def _validate_list_shape(list_text):
    lines = list_text.splitlines()
    if len(lines) > MAX_LIST_LINES:
        raise UpgradeWebError("list_too_large", "清单行数超过限制", status_code=413)
    if any(_utf8_size(line) > MAX_LINE_BYTES for line in lines):
        raise UpgradeWebError("list_too_large", "清单中存在过长的单行", status_code=413)


def _warning(code, message):
    return {"code": code, "message": message}


def _safe_customer_filename_segment(customer):
    normalized = unicodedata.normalize("NFC", customer or "")
    characters = []
    for character in normalized:
        if character in INVALID_FILENAME_CHARS or unicodedata.category(character) in {"Cc", "Cf"}:
            characters.append("_")
        else:
            characters.append(character)
    segment = re.sub(r"\s+", " ", "".join(characters))
    segment = re.sub(r"_+", "_", segment).strip(" ._")
    return segment or "customer"


def _truncate_utf8(value, max_bytes):
    if _utf8_size(value) <= max_bytes:
        return value
    result = []
    size = 0
    for character in value:
        character_size = _utf8_size(character)
        if size + character_size > max_bytes:
            break
        result.append(character)
        size += character_size
    return "".join(result).rstrip(" ._")


def _download_filename(customer, output_format):
    suffix = "-upgrade-file-list%s.md" % ("-ai" if output_format == "ai-md" else "")
    max_customer_bytes = MAX_DOWNLOAD_FILENAME_BYTES - _utf8_size(suffix)
    customer_segment = _truncate_utf8(_safe_customer_filename_segment(customer), max_customer_bytes)
    return "%s%s" % (customer_segment or "customer", suffix)


def _safe_parse_error(exc):
    message = str(exc)
    mappings = (
        ("清单内容为空", "empty_list", "清单内容为空"),
        ("无法解析 QC 标题行", "invalid_qc_header", "QC 标题格式不正确，请按“QC编号 标题 —— 模块”校对"),
        ("无法解析 SVN URL", "invalid_svn_url", "清单中存在无法识别的 SVN 文件路径"),
        ("解析不到客户名", "missing_customer", "未能从 SVN 文件路径中识别客户名称"),
        ("无法解析版本号", "invalid_version", "清单中存在无法识别的版本号"),
    )
    for prefix, code, safe_message in mappings:
        if prefix in message:
            return UpgradeWebError(code, safe_message)
    return UpgradeWebError("invalid_list", "清单格式不正确，请校对后重试")


def _list_customers(list_text):
    customers = OrderedDict()
    for block in core.rt_split_blocks(list_text):
        for raw_line in block[1:]:
            _marker, line = core.rt_parse_line_marker(raw_line)
            for customer, relative_path, _version, is_repository_path in core.rt_parse_file_references_from_line(line):
                if not is_repository_path and core.rt_is_standard_ecology_file(customer, relative_path):
                    continue
                customers.setdefault(customer, None)
    return list(customers)


def extract_upgrade_list(html):
    """从富文本 HTML 提取可编辑清单及摘要。"""
    _validate_text(html, "富文本内容", MAX_HTML_BYTES)
    _validate_html_shape(html)

    lines = core.rt_extract_list_from_html(html)
    file_lines = [line for line in lines if line.startswith(core.RT_COLOR_PREFIXES)]
    if not file_lines:
        raise UpgradeWebError(
            "no_svn_file",
            "未识别到带版本号的 SVN 文件路径（完整 URL 或 $/ 路径），请确认粘贴的是完整升级清单",
        )

    qc_count = sum(1 for line in lines if line.startswith("QC"))
    red_count = sum(1 for line in file_lines if line.startswith("[red] "))
    black_count = sum(1 for line in file_lines if line.startswith("[black] "))
    warnings = []
    if not qc_count:
        warnings.append(_warning("no_qc_header", "已识别文件，但没有找到 QC 标题；请在清单编辑区补充"))
    if not red_count:
        warnings.append(_warning("no_red_marker", "没有识别到红色文件，请确认复制内容保留了富文本颜色"))

    list_text = "\n".join(lines).rstrip() + "\n"
    return {
        "ok": True,
        "list_text": list_text,
        "summary": {
            "qc_count": qc_count,
            "file_line_count": len(file_lines),
            "red_count": red_count,
            "black_count": black_count,
        },
        "warnings": warnings,
    }


def generate_upgrade_markdown(list_text, output_format):
    """由用户校对后的清单生成人读或 AI Markdown。"""
    _validate_text(list_text, "升级清单", MAX_LIST_BYTES)
    _validate_list_shape(list_text)
    if output_format not in {"md", "ai-md"}:
        raise UpgradeWebError("invalid_format", "输出格式只支持 md 或 ai-md")

    try:
        entries, customer, raw_counter = core.rt_parse_txt(list_text)
    except ValueError as exc:
        raise _safe_parse_error(exc) from None

    stats = dict(core.rt_collect_stats(entries))
    if output_format == "md":
        content = core.rt_build_human_md(entries)
    else:
        content = core.rt_build_ai_md(entries, customer, raw_counter)
    filename = _download_filename(customer, output_format)
    if _utf8_size(content) > MAX_MARKDOWN_BYTES:
        raise UpgradeWebError("result_too_large", "生成结果超过大小限制", status_code=413)

    warnings = []
    customers = _list_customers(list_text)
    if len(customers) > 1:
        warnings.append(_warning(
            "multiple_customers",
            "清单包含多个客户名称，当前按出现频率选择：%s" % customer,
        ))

    return {
        "ok": True,
        "format": output_format,
        "filename": filename,
        "content": content,
        "customer": customer,
        "stats": stats,
        "warnings": warnings,
    }


__all__ = [
    "MAX_HTML_BYTES",
    "MAX_LIST_BYTES",
    "MAX_MARKDOWN_BYTES",
    "MAX_CSS_RULES",
    "MAX_SELECTOR_CHECKS",
    "MAX_SELECTOR_BYTES",
    "MAX_TOTAL_SELECTOR_BYTES",
    "MAX_SELECTOR_SCAN_BYTES",
    "MAX_DOWNLOAD_FILENAME_BYTES",
    "UpgradeWebError",
    "extract_upgrade_list",
    "generate_upgrade_markdown",
]
