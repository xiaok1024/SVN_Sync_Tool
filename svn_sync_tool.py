# -*- coding: utf-8 -*-
"""SVN 代码拉取 + 交叉文件覆盖 + 全自动提交 + 文件路径导出"""

import os, sys, subprocess, threading, shutil, locale, tempfile, atexit
import urllib.parse, unicodedata, re
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict, defaultdict
from html.parser import HTMLParser
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, font as tkfont
import ttkbootstrap as ttk
from pathlib import Path
from svn_path_generator import SvnPathGeneratorTab
try: import queue
except: import Queue as queue

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
SVN_EXECUTABLE = shutil.which("svn")
if not SVN_EXECUTABLE:
    for _svn_path in ("/opt/homebrew/bin/svn", "/usr/local/bin/svn", "/usr/bin/svn"):
        if os.path.isfile(_svn_path) and os.access(_svn_path, os.X_OK):
            SVN_EXECUTABLE = _svn_path
            break
SVN_EXECUTABLE = SVN_EXECUTABLE or "svn"

# 自动检测系统编码：中文 Windows 用 GBK，否则 UTF-8
_SYS_ENC = locale.getpreferredencoding()
_SVN_ENC = 'gbk' if _SYS_ENC.lower() in ('cp936', 'gbk', 'gb2312', 'gb18030') else 'utf-8'

# 平台判断：用于来源目录的共享地址处理
# Windows 可直接把 \\server\share 当本地路径访问，无需挂载；
# macOS 必须先把 smb:// 挂载到本地挂载点才能用 POSIX 文件接口访问。
IS_WINDOWS = (os.name == 'nt')
IS_MACOS = (sys.platform == 'darwin')


# ═══════════════ 升级清单提取逻辑（对标 Alfred redtext 链路） ═══════════════
# 移植自 script 仓库的 clipboard_extract_red_text.py / generate_upgrade_md.py
# / generate_upgrade_ai_md.py，纯 Python 实现，跨平台、可随 GUI 一起打包。

RT_NAMED_COLORS = {"red": (255, 0, 0), "darkred": (139, 0, 0), "crimson": (220, 20, 60), "firebrick": (178, 34, 34)}
RT_EXCLUDED_LINE_PREFIXES = ("PC端需要打包", "Mobile端需要打包", "本次总共需要修改")
RT_LOOSE_SVN_URL_RE = re.compile(r"https?://[^/]+/svn/\S+?\([Vv]\d+\)")
RT_COLOR_PREFIXES = ("[red] ", "[black] ")

RT_QC_HEADER_RE = re.compile(r"^(QC\d+)\s+(.+?)\s+——\s+(.+)$")
RT_MD_SVN_URL_RE = re.compile(r"https?://[^/]+/svn/([^/]+)/(.+?)\(([Vv]\d+)\)")
RT_MARKED_LINE_RE = re.compile(r"^\[(red|black)\]\s+(.+)$", re.IGNORECASE)

RT_BINARY_SUFFIXES = {
    ".class", ".jar", ".zip", ".war", ".ear", ".rar", ".7z", ".gz", ".tar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}
RT_SQL_SUFFIXES = {".sql"}
RT_UTF8_SUFFIXES = {".java", ".js", ".jsx", ".ts", ".tsx", ".jsp", ".xml", ".html", ".htm", ".css"}
RT_GBK_SUFFIXES = {".properties", ".sql"}


def rt_parse_style_declarations(style):
    declarations = {}
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        name, value = part.split(":", 1)
        declarations[name.strip().lower()] = value.strip()
    return declarations


def rt_parse_color(value):
    if not value:
        return None
    color = value.strip().lower()
    color = re.sub(r"\s*!important\s*$", "", color).strip()
    if color in RT_NAMED_COLORS:
        return RT_NAMED_COLORS[color]
    hex_match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", color)
    if hex_match:
        value = hex_match.group(1)
        if len(value) == 3:
            value = "".join(char * 2 for char in value)
        return tuple(int(value[index: index + 2], 16) for index in (0, 2, 4))
    rgb_match = re.match(r"rgba?\((.*)\)", color)
    if rgb_match:
        values = []
        for item in re.findall(r"[\d.]+%?", rgb_match.group(1))[:3]:
            if item.endswith("%"):
                values.append(round(float(item[:-1]) * 255 / 100))
            else:
                values.append(round(float(item)))
        if len(values) == 3:
            return tuple(max(0, min(255, v)) for v in values)
    return None


def rt_is_red_color(value, strict=False):
    rgb = rt_parse_color(value)
    if not rgb:
        return False
    red, green, blue = rgb
    if strict:
        return (red, green, blue) == (255, 0, 0)
    return red >= 170 and green <= 120 and blue <= 120 and red > green * 1.4 and red > blue * 1.4


def rt_extract_color_from_style(style):
    return rt_parse_style_declarations(style).get("color")


def rt_split_selectors(selector_text):
    return [s.strip() for s in selector_text.split(",") if s.strip()]


def rt_parse_css_color_rules(css_text):
    rules = []
    for selector_text, body in re.findall(r"([^{}]+)\{([^{}]+)\}", css_text or ""):
        color = rt_extract_color_from_style(body)
        if not color:
            continue
        for selector in rt_split_selectors(selector_text):
            if " " in selector or ">" in selector or ":" in selector:
                continue
            rules.append((selector, color))
    return rules


def rt_selector_color(selector, attrs, css_rules):
    tag = selector.lower()
    element_id = attrs.get("id", "")
    classes = set(attrs.get("class", "").split())
    color = None
    for rule_selector, rule_color in css_rules:
        rule_selector = rule_selector.strip()
        if rule_selector == tag:
            color = rule_color
        elif rule_selector.startswith(".") and rule_selector[1:] in classes:
            color = rule_color
        elif rule_selector.startswith("#") and rule_selector[1:] == element_id:
            color = rule_color
        elif "." in rule_selector and not rule_selector.startswith("."):
            rule_tag, rule_class = rule_selector.split(".", 1)
            if rule_tag.lower() == tag and rule_class in classes:
                color = rule_color
    return color


def rt_normalize_line(text):
    return re.sub(r"\s+", " ", text).strip()


def rt_should_exclude_line(text):
    return text.startswith(RT_EXCLUDED_LINE_PREFIXES)


class RedTextHTMLParser(HTMLParser):
    def __init__(self, strict=False, css_rules=None):
        super().__init__(convert_charrefs=True)
        self.strict = strict
        self.css_rules = list(css_rules or [])
        self.color_stack = [None]
        self.line_records = []
        self.current_line_parts = []
        self.current_red_parts = []
        self.current_line_segments = []
        self.in_style = False
        self.style_buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "style":
            self.in_style = True
            self.style_buffer = []
        color = self.color_stack[-1]
        css_color = rt_selector_color(tag, attrs, self.css_rules)
        inline_color = rt_extract_color_from_style(attrs.get("style", ""))
        font_color = attrs.get("color") if tag.lower() == "font" else None
        color = font_color or inline_color or css_color or color
        self.color_stack.append(color)
        if tag.lower() in {"br", "tr", "p", "div", "li"}:
            self._flush_line()

    def handle_endtag(self, tag):
        if tag.lower() == "style":
            self.css_rules.extend(rt_parse_css_color_rules("".join(self.style_buffer)))
            self.in_style = False
            self.style_buffer = []
        if len(self.color_stack) > 1:
            self.color_stack.pop()
        if tag.lower() in {"p", "div", "li", "tr"}:
            self._flush_line()

    def handle_data(self, data):
        if self.in_style:
            self.style_buffer.append(data)
            return
        color = self.color_stack[-1]
        red = rt_is_red_color(color, strict=self.strict)
        self._append_line_text(data, red=red)

    def close(self):
        super().close()
        self._flush_line()

    def _append_line_text(self, text, red=False):
        if not text:
            return
        self.current_line_parts.append(text)
        self.current_line_segments.append((text, red))
        if red:
            self.current_red_parts.append(text)
        elif self.current_red_parts and not text.strip():
            self.current_red_parts.append(text)

    def _flush_line(self):
        text = rt_normalize_line("".join(self.current_line_parts))
        red_text = rt_normalize_line("".join(self.current_red_parts))
        if text or red_text:
            self.line_records.append({"text": text, "red_text": red_text, "segments": list(self.current_line_segments)})
        self.current_line_parts = []
        self.current_red_parts = []
        self.current_line_segments = []

    def get_line_records(self):
        return self.line_records


def rt_analyze_html(html, strict=False):
    css_rules = []
    for style_text in re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.I | re.S):
        css_rules.extend(rt_parse_css_color_rules(style_text))
    parser = RedTextHTMLParser(strict=strict, css_rules=css_rules)
    parser.feed(html)
    parser.close()
    return parser.get_line_records()


def rt_contains_svn_url(text):
    return bool(RT_LOOSE_SVN_URL_RE.search(text or ""))


def rt_marked_line(color, text):
    return "[%s] %s" % (color, rt_normalize_line(text))


def rt_marked_file_lines_from_record(record):
    lines = []
    segments = record.get("segments") or []
    red_text = rt_normalize_line("".join(t for t, red in segments if red))
    black_text = rt_normalize_line("".join(t for t, red in segments if not red))
    if red_text and rt_contains_svn_url(red_text) and not rt_should_exclude_line(red_text):
        lines.append(rt_marked_line("red", red_text))
    if black_text and rt_contains_svn_url(black_text) and not rt_should_exclude_line(black_text):
        lines.append(rt_marked_line("black", black_text))
    if not lines:
        text = record.get("text", "")
        if rt_contains_svn_url(text) and not rt_should_exclude_line(text):
            color = "red" if record.get("red_text") else "black"
            lines.append(rt_marked_line(color, text))
    return lines


def rt_sort_grouped_texts(texts):
    grouped = OrderedDict()
    current_qc = None
    for text in texts:
        if text.startswith("QC"):
            current_qc = text
            grouped[current_qc] = []
        elif current_qc:
            grouped[current_qc].append(text)
    out = []
    for qc, paths in grouped.items():
        out.append(qc)
        out.extend(sorted(paths))
        out.append("")
    return out


def rt_extract_qc_and_marked_texts(line_records):
    texts = []
    has_qc = False
    for record in line_records:
        text = record["text"]
        if text.startswith("QC"):
            has_qc = True
            texts.append(text)
            continue
        texts.extend(rt_marked_file_lines_from_record(record))
    if not has_qc:
        return [line for record in line_records for line in rt_marked_file_lines_from_record(record)]
    return rt_sort_grouped_texts(texts)


def rt_extract_list_from_html(html, strict=False):
    """HTML → 升级清单文本行（QC 分组 + [red]/[black] URL）。"""
    records = rt_analyze_html(html, strict=strict)
    return rt_extract_qc_and_marked_texts(records)


# ---- 清单 TXT → Markdown ----

class RTFileEntry:
    def __init__(self, path):
        self.path = path
        self.versions = []
        self.marker_colors = set()

    def marker_color(self):
        if "red" in self.marker_colors:
            return "red"
        if "black" in self.marker_colors:
            return "black"
        return "red"


class RTQCEntry:
    def __init__(self, code, title, module):
        self.code = code
        self.title = title
        self.module = module
        self.files = OrderedDict()


def rt_parse_qc_header(line):
    match = RT_QC_HEADER_RE.match(line.strip())
    if not match:
        raise ValueError("无法解析 QC 标题行: " + line)
    return match.group(1), match.group(2).strip(), match.group(3).strip()


def rt_normalize_version(version):
    if version and version[0] in {"v", "V"}:
        return "V" + version[1:]
    return version


def rt_version_number(version):
    match = re.fullmatch(r"V(\d+)", rt_normalize_version(version))
    if not match:
        raise ValueError("无法解析版本号: " + version)
    return int(match.group(1))


def rt_sort_versions(versions):
    return sorted(versions, key=rt_version_number)


def rt_parse_svn_urls_from_line(line):
    return [
        (customer, path, rt_normalize_version(version))
        for customer, path, version in RT_MD_SVN_URL_RE.findall(line.strip())
    ]


def rt_is_standard_ecology_file(customer_name, relative_path):
    normalized = relative_path.replace("\\", "/")
    return customer_name == "ecology" and (normalized.startswith("trunk/") or normalized.startswith("branches/"))


def rt_parse_line_marker(line):
    match = RT_MARKED_LINE_RE.match(line.strip())
    if not match:
        return "red", line
    return match.group(1).lower(), match.group(2).strip()


def rt_color_label(color):
    return {"red": "红色", "black": "黑色"}.get(color, color)


def rt_split_blocks(text):
    blocks = []
    current = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def rt_select_customer_name(customer_names):
    unique = list(OrderedDict.fromkeys(customer_names))
    if not unique:
        raise ValueError("解析不到客户名，请确认清单中至少包含一条合法 SVN URL")
    if len(unique) == 1:
        return unique[0]
    # 多客户名时取出现次数最多的（GUI 下不强制本地目录校验）
    return Counter(customer_names).most_common(1)[0][0]


def rt_parse_txt(text):
    """清单文本 → (QC 列表, 客户名, 原始计数)。"""
    blocks = rt_split_blocks(text)
    if not blocks:
        raise ValueError("清单内容为空，未找到任何 QC 块")
    entries = []
    customer_names = []
    raw_counter = Counter()
    for block in blocks:
        code, title, module = rt_parse_qc_header(block[0])
        entry = RTQCEntry(code, title, module)
        for line in block[1:]:
            marker_color, url_line = rt_parse_line_marker(line)
            parsed_urls = rt_parse_svn_urls_from_line(url_line)
            if not parsed_urls:
                raise ValueError("无法解析 SVN URL: " + line)
            for customer, relative_path, version in parsed_urls:
                if rt_is_standard_ecology_file(customer, relative_path):
                    continue
                customer_names.append(customer)
                raw_counter[(code, relative_path, version)] += 1
                file_entry = entry.files.setdefault(relative_path, RTFileEntry(relative_path))
                file_entry.marker_colors.add(marker_color)
                if version not in file_entry.versions:
                    file_entry.versions.append(version)
        for file_entry in entry.files.values():
            file_entry.versions = rt_sort_versions(file_entry.versions)
        entries.append(entry)
    return entries, rt_select_customer_name(customer_names), raw_counter


def rt_build_human_md(entries):
    sections = []
    for entry in entries:
        lines = ["## " + entry.code, "- 标题: " + entry.title, "- 模块: " + entry.module]
        if entry.files:
            lines.append("- 文件:")
            for relative_path, file_entry in entry.files.items():
                version_text = ", ".join(file_entry.versions)
                marker_text = rt_color_label(file_entry.marker_color())
                lines.append(
                    "  - [`%s`](%s) `版本: %s` `标识: %s`" % (relative_path, relative_path, version_text, marker_text)
                )
        else:
            lines.append("- 文件: （当前清单未列出文件）")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def rt_generated_or_minified_reason(path):
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    name = parts[-1] if parts else normalized
    lower_name = name.lower()
    patterns = (
        r"\.map$", r"(^|[.-])chunk(\.|-|$).*\.js$", r"\.chunk\.js$",
        r"\.[a-f0-9]{8,}\.(js|css|map|json|html)$", r"\.min([.-].*)?\.(js|css)$", r"_wev.*\.(js|css)$",
    )
    if {"dist", "build"}.intersection(p.lower() for p in parts):
        return "generated-or-minified-file"
    if any(re.search(pattern, lower_name) for pattern in patterns):
        return "generated-or-minified-file"
    return None


def rt_default_skip_reason(path):
    normalized = path.replace("\\", "/")
    lower_path = normalized.lower()
    suffix = os.path.splitext(lower_path)[1]
    if lower_path.startswith("cloudstore/resource/"):
        return "cloudstore-resource-file"
    if suffix in RT_BINARY_SUFFIXES:
        return "binary-file"
    if suffix in RT_SQL_SUFFIXES:
        return "sql-file"
    return rt_generated_or_minified_reason(normalized)


def rt_classify_path(path):
    suffix = os.path.splitext(path.lower())[1]
    skip_reason = rt_default_skip_reason(path)
    if skip_reason == "cloudstore-resource-file":
        return "cloudstore-resource", "skip", skip_reason, "n/a"
    if skip_reason == "binary-file":
        return "binary", "skip", "binary-file", "n/a"
    if skip_reason == "sql-file":
        return "sql", "skip", "sql-file", "gbk"
    if skip_reason == "generated-or-minified-file":
        return "generated-asset", "skip", "generated-or-minified-file", "utf-8"
    if suffix in RT_GBK_SUFFIXES:
        return "source", "migrate", "manual-diff", "gbk"
    return "source", "migrate", "manual-diff", "utf-8"


def rt_collect_duplicate_files(entries):
    occurrences = defaultdict(list)
    for entry in entries:
        for file_entry in entry.files.values():
            occurrences[file_entry.path].append((entry.code, file_entry.versions))
    return OrderedDict(
        (path, values) for path, values in sorted(occurrences.items()) if len(values) > 1
    )


def rt_collect_stats(entries):
    unique_files = OrderedDict()
    stats = OrderedDict([
        ("qc", len(entries)), ("file_entries", 0), ("unique_files", 0), ("migrate", 0),
        ("skip_binary", 0), ("skip_sql", 0), ("skip_generated_asset", 0),
        ("skip_black_context", 0), ("empty_qc", 0),
    ])
    for entry in entries:
        if not entry.files:
            stats["empty_qc"] += 1
        for file_entry in entry.files.values():
            stats["file_entries"] += 1
            unique_files.setdefault(file_entry.path, None)
            if file_entry.marker_color() == "black":
                stats["skip_black_context"] += 1
                continue
            file_type, action, reason, _enc = rt_classify_path(file_entry.path)
            if action == "migrate":
                stats["migrate"] += 1
            elif file_type == "binary":
                stats["skip_binary"] += 1
            elif file_type == "sql":
                stats["skip_sql"] += 1
            elif reason == "generated-or-minified-file":
                stats["skip_generated_asset"] += 1
    stats["unique_files"] = len(unique_files)
    stats["duplicate_files"] = len(rt_collect_duplicate_files(entries))
    return stats


def rt_duplicate_raw_inputs(raw_counter):
    duplicates = [
        (code, path, version, count)
        for (code, path, version), count in raw_counter.items()
        if count > 1
    ]
    return sorted(duplicates, key=lambda item: (item[0], item[1], rt_version_number(item[2])))


def rt_build_ai_md(entries, customer_name, raw_counter):
    stats = rt_collect_stats(entries)
    duplicate_files = rt_collect_duplicate_files(entries)
    raw_duplicates = rt_duplicate_raw_inputs(raw_counter)
    lines = [
        "# E9 Upgrade AI File List",
        "",
        "> This file is generated for AI execution. Human-readable review should use `upgrade-file-list.md`.",
        "",
        "## Metadata",
        "- customer: `%s`" % customer_name,
        "- path_base: customer SVN working copy root",
        "- version_rule: versions are unique and sorted numerically within each QC/file",
        "",
        "## Stats",
    ]
    for key, value in stats.items():
        lines.append("- %s: %s" % (key, value))
    lines.extend(["", "## Duplicate Files"])
    if duplicate_files:
        for path, occurrences in duplicate_files.items():
            lines.append("- path: `%s`" % path)
            for code, versions in occurrences:
                lines.append("  - qc: `%s` versions: `%s`" % (code, ", ".join(versions)))
    else:
        lines.append("- none")
    lines.extend(["", "## Deduplicated Raw Inputs"])
    if raw_duplicates:
        for code, path, version, count in raw_duplicates:
            lines.append("- qc: `%s` path: `%s` version: `%s` raw_count: %s" % (code, path, version, count))
    else:
        lines.append("- none")
    lines.extend(["", "## QC Entries"])
    for entry in entries:
        lines.extend(["", "### " + entry.code, "- title: " + entry.title, "- module: " + entry.module, "- files:"])
        if not entry.files:
            lines.append("  - none")
            continue
        for file_entry in entry.files.values():
            marker_color = file_entry.marker_color()
            file_type, action, reason, encoding = rt_classify_path(file_entry.path)
            upgrade_scope = "upgrade-migrate"
            if marker_color == "black":
                action = "skip"
                reason = "black-context-file"
                upgrade_scope = "context-only"
            versions = file_entry.versions
            lines.extend([
                "  - path: `%s`" % file_entry.path,
                "    versions: `%s`" % ", ".join(versions),
                "    min_version: `%s`" % versions[0],
                "    max_version: `%s`" % versions[-1],
                "    type: `%s`" % file_type,
                "    action: `%s`" % action,
                "    reason: `%s`" % reason,
                "    encoding: `%s`" % encoding,
                "    marker_color: `%s`" % marker_color,
                "    upgrade_scope: `%s`" % upgrade_scope,
            ])
    return "\n".join(lines) + "\n"


class SvnSyncTool:
    def __init__(self, root):
        self.root = root
        self.root.title("SVN 代码同步工具")
        self.root.geometry("920x820")
        self.root.minsize(760, 660)
        self._setup_styles()
        self.svn_url = tk.StringVar()
        self.svn_user = tk.StringVar()
        self.svn_pass = tk.StringVar()
        self.smb_user = tk.StringVar()  # 共享地址（SMB）账号，仅 macOS 挂载用，与 SVN 账号分开
        self.smb_pass = tk.StringVar()
        self.checkout_dir = tk.StringVar()
        self.source_dir = tk.StringVar()
        self.target_dir = tk.StringVar()
        self.mode_var = tk.StringVar(value="checkout")
        self.log_queue = queue.Queue()
        self._commit_urls = []
        self._temp_mounts = []  # 本工具在 macOS 上临时挂载的 SMB 挂载点，退出时清理
        self._build_ui()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup_temp_mounts)

    def _setup_styles(self):
        self.colors = {
            "bg": "#f6f7f9",
            "panel": "#ffffff",
            "border": "#d7dce2",
            "primary": "#2563eb",
            "primary_hover": "#1d4ed8",
            "primary_pressed": "#1e40af",
            "text": "#1f2937",
            "muted": "#6b7280",
            "tab_bg": "#e9edf3",
            "tab_selected": "#eff6ff",
            "tab_hover": "#f1f5ff",
            "log_bg": "#f8fafc",
            "log_fg": "#334155",
            "path_fg": "#166534",
            "field": "#ffffff",
            "field_disabled": "#f3f4f6",
            "selection": "#dbeafe",
        }

        family = self._choose_font_family()
        self.fonts = {
            "body": (family, 10),
            "small": (family, 9),
            "hint": (family, 8),
            "button": (family, 10, "bold"),
            "tab": (family, 10, "bold"),
            "mono": ("Menlo" if IS_MACOS else "Consolas", 10),
            "mono_small": ("Menlo" if IS_MACOS else "Consolas", 9),
        }

        self.root.configure(bg=self.colors["bg"])
        self.root.option_add("*Font", self.fonts["body"])

        self.style = ttk.Style()
        if self.style.theme.name != "flatly":
            self.style.theme_use("flatly")

        self.style.configure("TFrame", background=self.colors["panel"])
        self.style.configure("App.TFrame", background=self.colors["bg"])
        self.style.configure("Panel.TFrame", background=self.colors["panel"])
        self.style.configure("TLabel", background=self.colors["panel"], foreground=self.colors["text"], font=self.fonts["body"])
        self.style.configure("Muted.TLabel", background=self.colors["panel"], foreground=self.colors["muted"], font=self.fonts["small"])
        self.style.configure("Hint.TLabel", background=self.colors["panel"], foreground=self.colors["muted"], font=self.fonts["hint"])
        self.style.configure("Treeview", font=self.fonts["small"], rowheight=28, background=self.colors["panel"], fieldbackground=self.colors["panel"], foreground=self.colors["text"], bordercolor=self.colors["border"])
        self.style.configure("Treeview.Heading", font=self.fonts["button"])
        self.style.configure("Panel.TLabelframe", background=self.colors["panel"], borderwidth=1)
        self.style.configure("Panel.TLabelframe.Label", background=self.colors["panel"], foreground=self.colors["text"], font=self.fonts["small"])
        self.style.configure("TRadiobutton", background=self.colors["panel"], foreground=self.colors["text"], font=self.fonts["small"])

    def _choose_font_family(self):
        preferred = ["PingFang SC", "Microsoft YaHei", "Arial"]
        try:
            available = set(tkfont.families(self.root))
        except Exception:
            available = set()
        for family in preferred:
            if not available or family in available:
                return family
        return "Arial"

    def _style_text(self, widget, kind="log"):
        fg = self.colors["path_fg"] if kind == "path" else self.colors["log_fg"]
        widget.configure(
            font=self.fonts["mono_small"],
            bg=self.colors["log_bg"],
            fg=fg,
            insertbackground=self.colors["text"],
            selectbackground="#dbeafe",
            selectforeground=self.colors["text"],
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["border"],
            padx=10,
            pady=8,
        )

    def _build_ui(self):
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(12, 12, 12, 10))
        shell.pack(fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(shell, takefocus=False)
        nb.pack(fill=tk.BOTH, expand=True)
        t1 = ttk.Frame(nb, padding=18, style="Panel.TFrame")
        nb.add(t1, text="  1. SVN 拉取  ")
        self._build_tab1(t1)
        t2 = ttk.Frame(nb, padding=18, style="Panel.TFrame")
        nb.add(t2, text="  2. 交叉覆盖  ")
        self._build_tab2(t2)
        t3 = ttk.Frame(nb, padding=18, style="Panel.TFrame")
        nb.add(t3, text="  3. 全自动流程  ")
        self._build_tab3(t3)
        t4 = ttk.Frame(nb, padding=18, style="Panel.TFrame")
        nb.add(t4, text="  4. 升级清单提取  ")
        self._build_tab4(t4)
        t5 = ttk.Frame(nb, padding=12)
        nb.add(t5, text="  5. 版本号路径生成  ")
        self._build_tab5(t5)

    def _build_tab1(self, t1):
        row = 0
        ttk.Label(t1, text="SVN 仓库地址：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        ttk.Entry(t1, textvariable=self.svn_url).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10)); row+=1
        af = ttk.Frame(t1)
        af.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Label(af, text="用户名：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_user, width=18).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(af, text="密码：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_pass, width=18, show="*").pack(side=tk.LEFT, padx=(0, 0))
        ttk.Label(af, text="（留空使用缓存）", style="Hint.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        row += 1
        ttk.Label(t1, text="拉取到目录：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        frm = ttk.Frame(t1)
        frm.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 14))
        ttk.Entry(frm, textvariable=self.checkout_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(frm, text="浏览...", command=self._browse_checkout, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        self.btn_co = ttk.Button(t1, text="拉取代码", command=self._start_checkout, bootstyle="primary")
        self.btn_co.grid(row=row, column=0, columnspan=3, pady=(0, 14)); row+=1
        ttk.Label(t1, text="执行日志：", style="Muted.TLabel").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        self.log_co = scrolledtext.ScrolledText(t1, height=14)
        self._style_text(self.log_co)
        self.log_co.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        t1.columnconfigure(1, weight=1)
        t1.rowconfigure(row, weight=1)

    def _build_tab2(self, t2):
        row = 0
        ttk.Label(t2, text="SVN 拉取目录（目标，被覆盖）：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        f2 = ttk.Frame(t2)
        f2.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(f2, textvariable=self.target_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f2, text="浏览...", command=self._browse_target, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        ttk.Label(t2, text="整理好的目录（来源，取文件）：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        f1 = ttk.Frame(t2)
        f1.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(f1, textvariable=self.source_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f1, text="浏览...", command=self._browse_source, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        sf2 = ttk.Frame(t2)
        sf2.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 12))
        ttk.Label(sf2, text="SMB 账号：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(sf2, textvariable=self.smb_user, width=16).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(sf2, text="密码：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(sf2, textvariable=self.smb_pass, width=16, show="*").pack(side=tk.LEFT)
        ttk.Label(sf2, text="（仅来源填 smb:// 共享时需要；本地目录/Windows 可留空）", style="Hint.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        row+=1
        bf = ttk.Frame(t2)
        bf.grid(row=row, column=0, columnspan=3, pady=(0, 14))
        self.btn_scan = ttk.Button(bf, text="扫描预览", command=self._start_scan, bootstyle="secondary-outline")
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_quick = ttk.Button(bf, text="一键覆盖（推荐）", command=self._start_quick_overwrite, bootstyle="primary")
        self.btn_quick.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_ow = ttk.Button(bf, text="覆盖选中", command=self._start_overwrite, state=tk.DISABLED, bootstyle="secondary-outline")
        self.btn_ow.pack(side=tk.LEFT)
        ttk.Button(bf, text="清空结果", command=self._clear_results, bootstyle="secondary-outline").pack(side=tk.LEFT, padx=(8, 0))
        row+=1
        ttk.Label(t2, text="文件列表（点击切换勾选）：", style="Muted.TLabel").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        lf = ttk.Frame(t2)
        lf.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        self.tree = ttk.Treeview(lf, columns=("src", "tgt"), show="tree headings", height=8)
        self.tree.heading("#0", text="文件名")
        self.tree.heading("src", text="来源路径")
        self.tree.heading("tgt", text="目标路径")
        self.tree.column("#0", width=260, minwidth=180)
        self.tree.column("src", width=240, minwidth=160)
        self.tree.column("tgt", width=240, minwidth=160)
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lbl_st = ttk.Label(t2, text="就绪", style="Muted.TLabel")
        self.lbl_st.grid(row=row, column=0, sticky=tk.W, pady=(8, 0))
        self.lbl_cnt = ttk.Label(t2, text="共 0 个文件", style="Muted.TLabel")
        self.lbl_cnt.grid(row=row, column=2, sticky=tk.E, pady=(8, 0))
        t2.columnconfigure(1, weight=1)
        t2.rowconfigure(row, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_click)
        self._xf = []
        self._ck = set()

    def _build_tab3(self, t3):
        row = 0
        af = ttk.Frame(t3)
        af.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 12))
        ttk.Label(af, text="用户名：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_user, width=16).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(af, text="密码：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_pass, width=16, show="*").pack(side=tk.LEFT, padx=(0, 0))
        ttk.Label(af, text="（留空使用缓存）", style="Hint.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        row += 1
        ttk.Label(t3, text="SVN 仓库地址：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        ttk.Entry(t3, textvariable=self.svn_url).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10)); row+=1
        ttk.Label(t3, text="SVN 拉取目录：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        f_a = ttk.Frame(t3)
        f_a.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(f_a, textvariable=self.checkout_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_a, text="浏览...", command=self._browse_checkout, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        ttk.Label(t3, text="整理好的目录（来源取文件）：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        f_b = ttk.Frame(t3)
        f_b.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 12))
        ttk.Entry(f_b, textvariable=self.source_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_b, text="浏览...", command=self._browse_source, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        sf3 = ttk.Frame(t3)
        sf3.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        ttk.Label(sf3, text="SMB 账号：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(sf3, textvariable=self.smb_user, width=16).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(sf3, text="密码：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Entry(sf3, textvariable=self.smb_pass, width=16, show="*").pack(side=tk.LEFT)
        ttk.Label(sf3, text="（仅来源填 smb:// 共享时需要；本地目录/Windows 可留空）", style="Hint.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        row+=1
        mode_f = ttk.Frame(t3)
        mode_f.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        ttk.Label(mode_f, text="拉取模式：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_f, text="checkout（首次拉取）", variable=self.mode_var, value="checkout").pack(side=tk.LEFT, padx=(6, 12))
        ttk.Radiobutton(mode_f, text="update（已有则更新）", variable=self.mode_var, value="update").pack(side=tk.LEFT, padx=(0, 6))
        row += 1
        ttk.Label(t3, text="SVN 提交信息：").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        self.auto_msg = scrolledtext.ScrolledText(t3, height=3, wrap=tk.WORD)
        self.auto_msg.configure(font=self.fonts["body"], bg=self.colors["field"], fg=self.colors["text"], insertbackground=self.colors["text"], relief=tk.FLAT, borderwidth=0, highlightthickness=1, highlightbackground=self.colors["border"], highlightcolor=self.colors["primary"], padx=8, pady=6)
        self.auto_msg.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 12))
        self.auto_msg.insert(tk.END, "自动同步代码")
        row += 1
        self.btn_auto = ttk.Button(t3, text="一键执行：拉取 -> 覆盖 -> 提交", command=self._start_auto_pipeline, bootstyle="primary")
        self.btn_auto.grid(row=row, column=0, columnspan=3, pady=(0, 14), ipady=4)
        row += 1
        ttk.Label(t3, text="执行日志：", style="Muted.TLabel").grid(row=row, column=0, sticky=tk.W, pady=(0, 6)); row+=1
        self.log_auto = scrolledtext.ScrolledText(t3, height=10)
        self._style_text(self.log_auto)
        self.log_auto.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        row += 1

        # 文件路径导出区域
        self.path_frame = ttk.Labelframe(t3, text=" 提交文件路径 ", style="Panel.TLabelframe")
        self.path_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(10, 2))
        pf_row = 0
        btn_copy_frame = ttk.Frame(self.path_frame)
        btn_copy_frame.grid(row=pf_row, column=0, sticky=tk.W, pady=(2, 6))
        self.btn_copy_paths = ttk.Button(btn_copy_frame, text="复制文件路径", command=self._copy_commit_paths, state=tk.DISABLED, bootstyle="secondary-outline")
        self.btn_copy_paths.pack(side=tk.LEFT, padx=(0, 10))
        self.lbl_paths_count = ttk.Label(btn_copy_frame, text="（暂无）", style="Muted.TLabel")
        self.lbl_paths_count.pack(side=tk.LEFT)
        pf_row += 1
        self.txt_paths = scrolledtext.ScrolledText(self.path_frame, height=4, state=tk.DISABLED)
        self._style_text(self.txt_paths, kind="path")
        self.txt_paths.grid(row=pf_row, column=0, sticky=tk.EW, pady=(2, 2))
        self.path_frame.columnconfigure(0, weight=1)
        row += 1

        t3.columnconfigure(1, weight=1)
        t3.rowconfigure(row - 2, weight=1)

    def _build_tab4(self, t4):
        row = 0
        ttk.Label(t4, text="从复制的带颜色升级清单提取，生成清单与升级 Markdown", style="Muted.TLabel").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10)); row += 1
        bf = ttk.Frame(t4)
        bf.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        ttk.Button(bf, text="从剪贴板提取", command=self._rt_extract, bootstyle="secondary-outline").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bf, text="清空", command=self._rt_clear, bootstyle="secondary-outline").pack(side=tk.LEFT)
        row += 1
        ttk.Label(t4, text="提取清单（可编辑，按 QC 分组，[red]/[black] + SVN URL）：", style="Muted.TLabel").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 6)); row += 1
        self.rt_list = scrolledtext.ScrolledText(t4, height=12, wrap=tk.NONE)
        self._style_text(self.rt_list)
        self.rt_list.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row += 1
        gen_f = ttk.Frame(t4)
        gen_f.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(10, 10))
        ttk.Button(gen_f, text="复制清单", command=self._rt_copy_list, bootstyle="secondary-outline").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(gen_f, text="生成升级 Markdown", command=self._rt_gen_human, bootstyle="secondary-outline").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(gen_f, text="生成 AI Markdown", command=self._rt_gen_ai, bootstyle="primary").pack(side=tk.LEFT)
        row += 1
        res_f = ttk.Frame(t4)
        res_f.grid(row=row, column=0, columnspan=3, sticky=tk.EW)
        ttk.Label(res_f, text="生成结果：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(res_f, text="另存为...", command=self._rt_save_result, bootstyle="secondary-outline").pack(side=tk.RIGHT)
        ttk.Button(res_f, text="复制结果", command=self._rt_copy_result, bootstyle="secondary-outline").pack(side=tk.RIGHT, padx=(0, 8))
        row += 1
        self.rt_result = scrolledtext.ScrolledText(t4, height=10, wrap=tk.NONE)
        self._style_text(self.rt_result, kind="path")
        self.rt_result.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row += 1
        self.rt_status = ttk.Label(t4, text="就绪", style="Muted.TLabel")
        self.rt_status.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(8, 0)); row += 1
        self._rt_result_default_name = "upgrade-file-list.md"
        t4.columnconfigure(1, weight=1)
        t4.rowconfigure(3, weight=3)
        t4.rowconfigure(7, weight=2)

    # ---- tab4 剪贴板读取（分平台）----
    def _read_clipboard_content(self):
        """读取剪贴板内容，返回 (text, kind)。kind 为 'html' 或 'text'（纯文本兜底）。"""
        if IS_WINDOWS:
            html = self._read_clipboard_html_windows()
            if html and html.strip():
                return html, "html"
        elif IS_MACOS:
            html = self._read_clipboard_html_macos()
            if html and html.strip():
                return html, "html"
        # 兜底：纯文本（无颜色信息，红/黑会全部判为黑）
        try:
            text = self.root.clipboard_get()
        except Exception:
            text = ""
        return text, "text"

    def _read_clipboard_html_macos(self):
        script = (
            'ObjC.import("AppKit");'
            '(function(){var pb=$.NSPasteboard.generalPasteboard;'
            'var types=["public.html","Apple HTML pasteboard type","HTML Format"];'
            'for(var i=0;i<types.length;i++){var v=pb.stringForType(types[i]);'
            'if(v){return ObjC.unwrap(v);}}return "";})()'
        )
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                               capture_output=True, text=True, errors="ignore")
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            pass
        try:
            r = subprocess.run(["pbpaste", "-Prefer", "html"], capture_output=True, text=True, errors="ignore")
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
        return ""

    def _read_clipboard_html_windows(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # 关键：显式声明返回值/参数类型，否则 64 位 Windows 上句柄/指针会被截断为 32 位
            user32.RegisterClipboardFormatW.restype = ctypes.c_uint
            user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.GetClipboardData.restype = ctypes.c_void_p
            user32.GetClipboardData.argtypes = [ctypes.c_uint]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalSize.restype = ctypes.c_size_t
            kernel32.GlobalSize.argtypes = [ctypes.c_void_p]

            cf_html = user32.RegisterClipboardFormatW("HTML Format")
            if not cf_html:
                return ""
            if not user32.OpenClipboard(None):
                return ""
            try:
                handle = user32.GetClipboardData(cf_html)
                if not handle:
                    return ""  # 剪贴板里没有 HTML 格式（多为纯文本来源）
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    size = kernel32.GlobalSize(handle)
                    data = ctypes.string_at(ptr, size)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
            # CF_HTML 头部为 ASCII，StartHTML 给出 HTML 片段的字节偏移
            m = re.search(rb"StartHTML:(\d+)", data)
            if m:
                start = int(m.group(1))
                if 0 <= start < len(data):
                    return data[start:].decode("utf-8", errors="replace")
            idx = data.find(b"<")
            return data[idx:].decode("utf-8", errors="replace") if idx >= 0 else data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    # ---- tab4 动作 ----
    def _rt_set_list(self, text):
        self.rt_list.delete(1.0, tk.END)
        self.rt_list.insert(tk.END, text)

    def _rt_set_result(self, text):
        self.rt_result.delete(1.0, tk.END)
        self.rt_result.insert(tk.END, text)

    def _rt_extract(self):
        self.rt_status.config(text="正在读取剪贴板...")

        def run():
            try:
                content_text, kind = self._read_clipboard_content()
                if not content_text or not content_text.strip():
                    self.root.after(0, lambda: self.rt_status.config(text="剪贴板没有内容，请先从网页复制带颜色的升级清单"))
                    return
                lines = rt_extract_list_from_html(content_text)
                file_lines = [l for l in lines if l.startswith(RT_COLOR_PREFIXES)]
                if not file_lines:
                    if kind == "text":
                        msg = "剪贴板只有纯文本（拿不到颜色）：请从浏览器/富文本复制带样式的升级清单；若已是富文本仍失败，可截图反馈"
                    else:
                        msg = "未识别到 SVN 文件行：请确认清单里包含形如 https://.../svn/客户/路径(V版本) 的 URL"
                    self.root.after(0, lambda: self.rt_status.config(text=msg))
                    return
                content = "\n".join(lines)
                qc_count = sum(1 for l in lines if l.startswith("QC"))
                degraded = "（纯文本，红/黑均按黑处理）" if kind == "text" else ""
                self.root.after(0, lambda: (self._rt_set_list(content),
                                            self.rt_status.config(text="提取完成：%d 个 QC，%d 个文件行%s" % (qc_count, len(file_lines), degraded))))
            except Exception as e:
                self.root.after(0, lambda: self.rt_status.config(text="提取失败: " + str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _rt_clear(self):
        self._rt_set_list("")
        self._rt_set_result("")
        self.rt_status.config(text="就绪")

    def _rt_copy_list(self):
        text = self.rt_list.get(1.0, tk.END).strip()
        if not text:
            self.rt_status.config(text="清单为空，无可复制内容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.rt_status.config(text="清单已复制到剪贴板")

    def _rt_gen_human(self):
        text = self.rt_list.get(1.0, tk.END).strip()
        if not text:
            self.rt_status.config(text="请先提取或填写清单")
            return
        try:
            entries, customer, _raw = rt_parse_txt(text)
            md = rt_build_human_md(entries)
        except Exception as e:
            self.rt_status.config(text="生成升级 Markdown 失败: " + str(e))
            return
        self._rt_set_result(md)
        self._rt_result_default_name = "upgrade-file-list.md"
        self.rt_status.config(text="已生成升级 Markdown（客户: %s，%d 个 QC）" % (customer, len(entries)))

    def _rt_gen_ai(self):
        text = self.rt_list.get(1.0, tk.END).strip()
        if not text:
            self.rt_status.config(text="请先提取或填写清单")
            return
        try:
            entries, customer, raw = rt_parse_txt(text)
            md = rt_build_ai_md(entries, customer, raw)
        except Exception as e:
            self.rt_status.config(text="生成 AI Markdown 失败: " + str(e))
            return
        self._rt_set_result(md)
        self._rt_result_default_name = "upgrade-file-list-ai.md"
        self.rt_status.config(text="已生成 AI Markdown（客户: %s，%d 个 QC）" % (customer, len(entries)))

    def _rt_copy_result(self):
        text = self.rt_result.get(1.0, tk.END).strip()
        if not text:
            self.rt_status.config(text="结果为空，无可复制内容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.rt_status.config(text="结果已复制到剪贴板")

    def _rt_save_result(self):
        text = self.rt_result.get(1.0, tk.END).strip()
        if not text:
            self.rt_status.config(text="结果为空，无可保存内容")
            return
        init_dir = self.checkout_dir.get().strip() or os.path.expanduser("~/Desktop")
        path = filedialog.asksaveasfilename(
            title="保存 Markdown",
            initialdir=init_dir if os.path.isdir(init_dir) else os.path.expanduser("~"),
            initialfile=getattr(self, "_rt_result_default_name", "upgrade-file-list.md"),
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + ("\n" if not text.endswith("\n") else ""))
            self.rt_status.config(text="已保存: " + path)
        except OSError as e:
            self.rt_status.config(text="保存失败: " + str(e))

    def _browse_checkout(self):
        d = filedialog.askdirectory(title="选择 SVN 拉取目标目录")
        if d: self.checkout_dir.set(d)
    def _browse_source(self):
        d = filedialog.askdirectory(title="选择整理好的目录（来源）")
        if d: self.source_dir.set(d)
    def _browse_target(self):
        d = filedialog.askdirectory(title="选择 SVN 拉取目录（目标）")
        if d: self.target_dir.set(d)

    def _log(self, w, m):
        self.log_queue.put((w, m))

    def _poll_log_queue(self):
        try:
            while True:
                w, m = self.log_queue.get_nowait()
                w.insert(tk.END, m); w.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _build_svn_cmd(self, *args):
        cmd = [SVN_EXECUTABLE, "--non-interactive",
               "--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,not-yet-valid,other"]
        u = self.svn_user.get().strip()
        p = self.svn_pass.get().strip()
        if u:
            cmd.extend(["--username", u])
            if p:
                cmd.extend(["--password", p])
            else:
                cmd.append("--no-auth-cache")
        cmd.extend(args)
        return cmd

    def _svn_env(self):
        env = os.environ.copy()
        if os.name != 'nt':
            env["LANG"] = "zh_CN.UTF-8"
            env["LC_ALL"] = "zh_CN.UTF-8"
            env["LC_CTYPE"] = "zh_CN.UTF-8"
            extra_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env["PATH"] = extra_path + (":" + env["PATH"] if env.get("PATH") else "")
        return env

    def _run_svn(self, log_widget, *args):
        """运行 svn 命令，用系统编码解码输出"""
        cmd = self._build_svn_cmd(*args)
        self._log(log_widget, ">> " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding=_SVN_ENC, errors="replace",
            env=self._svn_env(),
            creationflags=CREATE_NO_WINDOW)
        out_lines = []
        for line in proc.stdout:
            self._log(log_widget, line)
            out_lines.append(line)
        proc.wait()
        return proc.returncode, "".join(out_lines)

    def _run_svn_bytes(self, *args, force_utf8=False):
        """运行 svn 命令，返回原始字节解码后的文本。
        force_utf8=True 用于 --xml 输出（svn 的 XML 始终是 UTF-8，与系统/界面 locale 无关）。"""
        cmd = self._build_svn_cmd(*args)
        proc = subprocess.Popen(cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._svn_env(),
            creationflags=CREATE_NO_WINDOW)
        out, err = proc.communicate(timeout=30)
        if force_utf8:
            text = out.decode('utf-8', errors='replace')
        else:
            # 优先系统编码（中文 Windows 为 GBK），失败则 UTF-8
            try:
                text = out.decode(_SVN_ENC)
            except (UnicodeDecodeError, LookupError):
                text = out.decode('utf-8', errors='replace')
        return proc.returncode, text

    def _start_checkout(self):
        url = self.svn_url.get().strip()
        if not url: messagebox.showwarning("提示", "请先输入 SVN 仓库地址"); return
        dst = self.checkout_dir.get().strip()
        if not dst: messagebox.showwarning("提示", "请选择拉取目录"); return
        self.btn_co.config(state=tk.DISABLED, text="正在拉取...")
        self.log_co.delete(1.0, tk.END)

        def run():
            try:
                rc, _ = self._run_svn(self.log_co, "checkout", url, dst)
                if rc == 0:
                    self._log(self.log_co, "\n--- 拉取完成 ---\n")
                    self.target_dir.set(dst)
                else:
                    self._log(self.log_co, "\n--- 拉取失败，返回码: " + str(rc) + " ---\n")
            except Exception as e:
                self._log(self.log_co, "\n--- 错误: " + str(e) + " ---\n")
            finally:
                self.root.after(0, lambda: self.btn_co.config(state=tk.NORMAL, text="拉取代码"))
        threading.Thread(target=run, daemon=True).start()

    # ═══════════════ 来源目录共享地址处理（分平台） ═══════════════
    def _clean_share_text(self, path):
        """剥除常见的提示语包装（如「标准文件请到…下面提取」），返回纯地址。
        让用户可以直接粘贴原文，无需手动删前后缀。"""
        p = (path or "").strip()
        # 去掉前缀提示语
        for pre in ("标准文件请到", "标准文件在", "请到", "文件请到"):
            if p.startswith(pre):
                p = p[len(pre):].strip()
                break
        # 去掉后缀提示语
        for suf in ("下面提取", "里提取", "中提取", "提取", "下载"):
            if p.endswith(suf):
                p = p[:-len(suf)].strip()
                break
        return p

    def _is_share_address(self, path):
        """判断来源是否为网络共享地址（smb:// 或 \\\\server\\share 或 //server/share）。
        会先剥除提示语包装再判断。"""
        p = self._clean_share_text(path)
        low = p.lower()
        return (low.startswith("smb://") or low.startswith("smb:")
                or p.startswith("\\\\") or p.startswith("//"))

    def _share_to_smb_url(self, path):
        """把任意写法的共享地址归一化为 smb://server/share/sub。"""
        p = path.strip()
        low = p.lower()
        if low.startswith("smb://"):
            return p
        if low.startswith("smb:"):
            return "smb://" + p[4:].lstrip("/")
        # \\server\share 或 //server/share
        p = p.replace("\\", "/").lstrip("/")
        return "smb://" + p

    def _share_to_unc(self, path):
        """把任意写法的共享地址归一化为 Windows UNC：\\\\server\\share。"""
        p = path.strip()
        low = p.lower()
        if low.startswith("smb://"):
            p = p[6:]
        elif low.startswith("smb:"):
            p = p[4:].lstrip("/")
        p = p.replace("/", "\\")
        if not p.startswith("\\\\"):
            p = "\\\\" + p.lstrip("\\")
        return p

    def _precheck_source(self, src):
        """主线程快速校验来源；共享地址的可达性延后到 worker 线程解析时再判断。
        返回错误消息字符串；无错误返回 None。"""
        if not src:
            return "请先选择/填写来源目录"
        if not self._is_share_address(src) and not os.path.isdir(src):
            return "来源目录不存在: " + src
        return None

    def _resolve_source_path(self, addr, log=None):
        """把来源地址解析为可直接用于文件遍历/复制的本地路径。
        - 普通本地路径：原样返回。
        - Windows + 共享地址：转成 UNC，系统可直接访问，无需挂载。
        - macOS + 共享地址：复用已有挂载或临时挂载，返回真实路径。
        失败时抛出异常。"""
        raw = (addr or "").strip()
        if not self._is_share_address(raw):
            return raw  # 普通本地路径，原样返回
        addr = self._clean_share_text(raw)  # 共享地址：剥除提示语包装
        if IS_WINDOWS:
            return self._share_to_unc(addr)
        if IS_MACOS:
            return self._mount_smb_macos(addr, log)
        # 其它平台：按已挂载的本地路径处理
        return addr

    def _find_existing_smb_mount(self, server, rel_path):
        """在已有 smbfs 挂载中查找能覆盖 server + rel_path 的挂载，返回对应真实本地路径。
        支持挂载点位于共享根，也支持挂载点已是任意深层子目录（macOS 允许挂载深层路径）。
        rel_path 形如 'ECOLOGY_customer/H/H河南思维/QC4911408/ecology'（//server/ 之后的部分）。
        """
        def norm(parts):
            # 统一 Unicode 规范化，规避 NFC/NFD 中文路径不匹配
            return [unicodedata.normalize("NFC", p) for p in parts if p]
        target = norm(rel_path.split("/"))
        try:
            out = subprocess.run(["mount"], capture_output=True, text=True).stdout
        except Exception:
            return None
        for line in (out or "").splitlines():
            if "smbfs" not in line or " on " not in line:
                continue
            source, rest = line.split(" on ", 1)
            mount_path = rest.split(" (", 1)[0].strip()
            source = source.strip()
            if not source.startswith("//"):
                continue
            body = source[2:]            # user@server/share/sub...
            slash = body.find("/")
            if slash < 0:
                continue
            m_server = body[:slash].split("@")[-1]
            if m_server.lower() != server.lower():
                continue
            # 挂载源路径可能被百分号编码（如中文 %E6%B2%B3...），先解码再比对
            m_parts = norm(urllib.parse.unquote(body[slash + 1:]).split("/"))
            if not m_parts:
                continue
            # 挂载路径必须是目标路径的前缀，剩余部分追加到挂载点
            if target[:len(m_parts)] != m_parts:
                continue
            remaining = target[len(m_parts):]
            return os.path.join(mount_path, *remaining) if remaining else mount_path
        return None

    def _mount_smb_macos(self, addr, log=None):
        """macOS：把共享地址挂载到本地并返回真实路径。优先复用已有挂载。"""
        smb_url = self._share_to_smb_url(addr)
        raw = smb_url[6:]  # 去掉 smb://，得到 server/share/sub...
        if "/" not in raw:
            raise ValueError("SMB 地址需包含 server/share：" + addr)
        server, rel = raw.split("/", 1)   # server, "share/sub..."
        server = server.split("@")[-1]
        rel_parts = rel.split("/", 1)
        share = rel_parts[0]
        sub = rel_parts[1] if len(rel_parts) > 1 else ""

        # 1) 优先复用已有挂载（Finder 在 /Volumes 的连接、本工具之前的临时挂载）
        #    挂载点可能是共享根，也可能已是深层子目录（如直接挂到 .../ecology）
        existing = self._find_existing_smb_mount(server, rel)
        if existing:
            if os.path.isdir(existing):
                if log: self._log(log, "复用已挂载共享: " + existing + "\n")
                return existing
            raise RuntimeError("已挂载 %s，但目标目录不存在: %s" % (server, rel))

        # 2) 临时挂载
        mount_point = tempfile.mkdtemp(prefix="svn_sync_smb_")
        smb_u = self.smb_user.get().strip()
        smb_p = self.smb_pass.get().strip()
        if smb_u:
            # 用填写的 SMB 账号密码挂载：//user:pass@server/share（对账号密码做 URL 转义）
            auth = urllib.parse.quote(smb_u, safe="")
            if smb_p:
                auth += ":" + urllib.parse.quote(smb_p, safe="")
            mount_source = "//%s@%s/%s" % (auth, server, share)
        else:
            # 未填账号：免密挂载，依赖钥匙串/Guest（-N 不弹密码框）
            mount_source = "//%s/%s" % (server, share)
        # 注意：日志只打印不含密码的地址，避免泄露凭据
        if log: self._log(log, "正在挂载 SMB: //%s/%s -> %s\n" % (server, share, mount_point))
        try:
            cmd = ["mount_smbfs", mount_source, mount_point] if smb_u \
                else ["mount_smbfs", "-N", mount_source, mount_point]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self._safe_rmdir(mount_point)
            raise RuntimeError("挂载超时（>60s），请检查网络与共享地址: //%s/%s" % (server, share))
        if res.returncode != 0:
            self._safe_rmdir(mount_point)
            hint = "请检查 SMB 账号密码是否正确" if smb_u else "请填写 SMB 账号密码，或先在访达中用 Cmd+K 连接一次"
            raise RuntimeError("挂载失败（%s）：%s\n%s"
                               % (smb_url, hint, (res.stderr or "").strip()))
        self._temp_mounts.append(mount_point)
        full = os.path.join(mount_point, *sub.split("/")) if sub else mount_point
        if not os.path.isdir(full):
            raise RuntimeError("挂载成功但子目录不存在: " + sub)
        return full

    def _safe_rmdir(self, p):
        try:
            os.rmdir(p)
        except OSError:
            pass

    def _cleanup_temp_mounts(self):
        """卸载本工具创建的临时挂载点（不会动 Finder/用户手动挂载的共享）。"""
        for mp in list(self._temp_mounts):
            subprocess.run(["umount", mp], capture_output=True, check=False)
            self._safe_rmdir(mp)
        self._temp_mounts = []

    def _on_close(self):
        self._cleanup_temp_mounts()
        self.root.destroy()

    def _scan_cross_files(self, tgt, src):
        results = []
        tp = Path(tgt)
        sp = Path(src)
        for f in sorted(tp.rglob("*")):
            if not f.is_file(): continue
            if ".svn" in f.parts or ".git" in f.parts: continue
            rel = f.relative_to(tp)
            src_file = sp / rel
            if src_file.exists() and src_file.is_file():
                results.append((str(rel), str(src_file), str(f)))
        return results

    def _start_scan(self):
        src = self.source_dir.get().strip()
        tgt = self.target_dir.get().strip()
        if not src or not tgt: messagebox.showwarning("提示", "请先选择来源目录和目标目录"); return
        err = self._precheck_source(src)
        if err: messagebox.showerror("错误", err); return
        if not os.path.isdir(tgt): messagebox.showerror("错误", "目标目录不存在: " + tgt); return
        self._clear_results()
        self.btn_scan.config(state=tk.DISABLED, text="扫描中...")
        self.lbl_st.config(text="正在扫描...")

        def run():
            try:
                rsrc = self._resolve_source_path(src)
            except Exception as e:
                self.root.after(0, lambda: (self.lbl_st.config(text="挂载失败: " + str(e)),
                                            self.btn_scan.config(state=tk.NORMAL, text="扫描预览")))
                return
            if not os.path.isdir(rsrc):
                self.root.after(0, lambda: (self.lbl_st.config(text="来源目录不存在: " + rsrc),
                                            self.btn_scan.config(state=tk.NORMAL, text="扫描预览")))
                return
            res = self._scan_cross_files(tgt, rsrc)
            self.root.after(0, lambda: self._show_scan(res))
        threading.Thread(target=run, daemon=True).start()

    def _show_scan(self, res):
        self._xf = res; self._ck.clear()
        self.tree.delete(*self.tree.get_children())
        if not res:
            self.lbl_st.config(text="未发现交叉文件"); self.lbl_cnt.config(text="共 0 个文件")
            self.btn_ow.config(state=tk.DISABLED); self.btn_scan.config(state=tk.NORMAL, text="扫描预览")
            return
        for i, (r, sa, ta) in enumerate(res):
            iid = "i" + str(i)
            self.tree.insert("", tk.END, iid=iid, text=r, values=(sa, ta))
            self._ck.add(iid)
        self.lbl_st.config(text="扫描完成 - 点击条目切换勾选")
        self.lbl_cnt.config(text="共 " + str(len(res)) + " 个文件 (全部默认勾选)")
        self.btn_ow.config(state=tk.NORMAL)
        self.btn_scan.config(state=tk.NORMAL, text="扫描预览")

    def _on_click(self, evt):
        for iid in self.tree.selection():
            if iid in self._ck: self._ck.discard(iid)
            else: self._ck.add(iid)
        self.tree.selection_remove(*self.tree.selection())
        self.lbl_cnt.config(text="已选 " + str(len(self._ck)) + " / " + str(len(self._xf)) + " 个")

    def _start_quick_overwrite(self):
        src = self.source_dir.get().strip()
        tgt = self.target_dir.get().strip()
        if not src or not tgt: messagebox.showwarning("提示", "请先选择来源目录和目标目录"); return
        err = self._precheck_source(src)
        if err: messagebox.showerror("错误", err); return
        if not os.path.isdir(tgt): messagebox.showerror("错误", "目标目录不存在: " + tgt); return
        self._clear_results()
        self.btn_quick.config(state=tk.DISABLED, text="正在覆盖...")
        self.lbl_st.config(text="正在扫描并覆盖...")

        def run():
            try:
                rsrc = self._resolve_source_path(src)
                if not os.path.isdir(rsrc):
                    self.root.after(0, lambda: self._quick_done(0, 0, "来源目录不存在: " + rsrc))
                    return
                res = self._scan_cross_files(tgt, rsrc)
                if not res:
                    self.root.after(0, lambda: self._quick_done(0, 0, "未找到匹配的交叉文件"))
                    return
                ok = 0; fail = 0
                for rel, sa, ta in res:
                    try:
                        Path(ta).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sa, ta); ok += 1
                    except: fail += 1
                self.root.after(0, lambda: self._quick_done(ok, fail, ""))
            except Exception as e:
                self.root.after(0, lambda: self._quick_done(0, 0, "错误: " + str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _quick_done(self, ok, fail, err):
        if err:
            self.lbl_st.config(text=err); self.lbl_cnt.config(text="")
        else:
            msg = "覆盖完成! 成功 " + str(ok) + " 个"
            if fail: msg += ", 失败 " + str(fail) + " 个"
            self.lbl_st.config(text=msg); self.lbl_cnt.config(text="")
        self.btn_quick.config(state=tk.NORMAL, text="一键覆盖（推荐）")

    def _start_overwrite(self):
        if not self._ck: messagebox.showinfo("提示", "请勾选要覆盖的文件"); return
        if not messagebox.askyesno("确认", "确定覆盖 " + str(len(self._ck)) + " 个文件？\n此操作不可撤销！"): return
        self.btn_ow.config(state=tk.DISABLED, text="覆盖中...")
        self.lbl_st.config(text="正在覆盖...")

        def run():
            ok = 0; fail = 0
            for i, (r, sa, ta) in enumerate(self._xf):
                if ("i" + str(i)) not in self._ck: continue
                try:
                    Path(ta).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sa, ta); ok += 1
                except: fail += 1
            self.root.after(0, lambda: self._done(ok, fail))
        threading.Thread(target=run, daemon=True).start()

    def _done(self, ok, fail):
        msg = "覆盖完成! 成功 " + str(ok) + " 个"
        if fail: msg += ", 失败 " + str(fail) + " 个"
        self.lbl_st.config(text=msg); self.lbl_cnt.config(text="")
        self.btn_ow.config(state=tk.NORMAL, text="覆盖选中")
        messagebox.showinfo("完成", msg)

    def _clear_results(self):
        self._xf = []; self._ck.clear()
        self.tree.delete(*self.tree.get_children())
        self.lbl_st.config(text="就绪"); self.lbl_cnt.config(text="共 0 个文件")
        self.btn_ow.config(state=tk.DISABLED)

    # ═══════════════ 全自动流程 + 文件路径导出 ═══════════════
    def _start_auto_pipeline(self):
        url = self.svn_url.get().strip()
        dst = self.checkout_dir.get().strip()
        src = self.source_dir.get().strip()
        mode = self.mode_var.get()

        if not url: messagebox.showwarning("提示", "请先输入 SVN 仓库地址"); return
        if not dst: messagebox.showwarning("提示", "请选择 SVN 拉取目录"); return
        if not src: messagebox.showwarning("提示", "请选择整理好的目录"); return
        err = self._precheck_source(src)
        if err: messagebox.showerror("错误", err); return

        msg = self.auto_msg.get(1.0, tk.END).strip()
        if not msg: messagebox.showwarning("提示", "请输入提交信息"); return

        self.btn_auto.config(state=tk.DISABLED, text="正在执行...")
        self.log_auto.delete(1.0, tk.END)
        self._clear_paths_display()

        def run():
            log = self.log_auto
            overall_ok = True
            rev = None
            try:
                # Step 1
                self._log(log, "【步骤 1/3】SVN 拉取\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                dst_exists = os.path.isdir(dst) and os.path.isdir(os.path.join(dst, ".svn"))
                if dst_exists and mode == "update":
                    self._log(log, "目录已存在，执行 svn update...\n")
                    rc, _ = self._run_svn(log, "update", dst)
                else:
                    if dst_exists:
                        self._log(log, "目录已存在但选择 checkout 模式，先删除再拉取...\n")
                        shutil.rmtree(dst, ignore_errors=True)
                    rc, _ = self._run_svn(log, "checkout", url, dst)
                if rc != 0:
                    self._log(log, "\n--- 步骤 1 失败，终止流程 ---\n")
                    self.root.after(0, lambda: self._auto_done(False))
                    return
                self.target_dir.set(dst)
                self._log(log, "\n--- 步骤 1 完成 ---\n\n")

                # Step 2
                self._log(log, "【步骤 2/3】交叉文件覆盖\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                try:
                    rsrc = self._resolve_source_path(src, log)
                except Exception as e:
                    self._log(log, "来源共享挂载失败: " + str(e) + "\n--- 步骤 2 失败，终止流程 ---\n")
                    self.root.after(0, lambda: self._auto_done(False))
                    return
                if not os.path.isdir(rsrc):
                    self._log(log, "来源目录不存在: " + rsrc + "\n--- 步骤 2 失败，终止流程 ---\n")
                    self.root.after(0, lambda: self._auto_done(False))
                    return
                res = self._scan_cross_files(dst, rsrc)
                if not res:
                    self._log(log, "未找到交叉文件，跳过覆盖\n\n")
                else:
                    ok = 0; fail = 0
                    for rel, sa, ta in res:
                        try:
                            Path(ta).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sa, ta)
                            self._log(log, "  [覆盖] " + rel + "\n")
                            ok += 1
                        except Exception as e:
                            self._log(log, "  [失败] " + rel + " - " + str(e) + "\n")
                            fail += 1
                    self._log(log, "覆盖结果: 成功 " + str(ok) + " 个")
                    if fail: self._log(log, ", 失败 " + str(fail) + " 个")
                    self._log(log, "\n\n--- 步骤 2 完成 ---\n\n")

                # Step 3
                self._log(log, "【步骤 3/3】SVN 提交\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                self._log(log, "检查变更状态...\n")
                _, status_out = self._run_svn(log, "status", dst)
                changed = [l for l in status_out.split("\n") if l.strip() and ".svn" not in l]
                if not changed:
                    self._log(log, "无变更需要提交\n")
                    # 即使本次无新增提交，也导出工作副本当前版本的文件路径，方便随时复制
                    last_rev = self._get_wc_last_revision(dst)
                    if last_rev:
                        self._log(log, "（无新增提交，导出当前版本 %s 的文件路径）\n" % last_rev)
                        self.root.after(0, lambda r=last_rev: self._load_and_show_commit_paths(r))
                else:
                    self._log(log, "共 " + str(len(changed)) + " 个文件有变更，正在提交...\n")
                    rc, commit_out = self._run_svn(log, "commit", dst, "-m", msg)
                    if rc == 0:
                        self._log(log, "\n--- 提交成功！---\n")
                        rev = self._parse_revision(commit_out)
                        if rev:
                            self._log(log, "版本号: " + str(rev) + "\n")
                            self.root.after(0, lambda r=rev: self._load_and_show_commit_paths(r))
                        overall_ok = True
                    else:
                        self._log(log, "\n--- 提交失败，返回码: " + str(rc) + " ---\n")
                        overall_ok = False

                self._log(log, "\n" + "=" * 45 + "\n")
                self._log(log, "全自动流程结束\n")

            except Exception as e:
                self._log(log, "\n--- 流程异常: " + str(e) + " ---\n")
                overall_ok = False
            finally:
                self.root.after(0, lambda: self._auto_done(overall_ok))

        threading.Thread(target=run, daemon=True).start()

    def _parse_revision(self, commit_output):
        import re
        # 兼容英文「Committed revision N」与中文「提交后的版本为 N」等本地化输出
        m = re.search(r'(?:Committed revision|提交后的版本为?|版本)\s*(\d+)', commit_output)
        if m:
            return int(m.group(1))
        return None

    def _load_and_show_commit_paths(self, rev):
        dst = self.checkout_dir.get().strip()
        if not dst or not os.path.isdir(dst):
            return

        def run():
            try:
                base_url = self._get_repo_root_http_url(dst)
                if not base_url:
                    return
                changed_paths = self._get_changed_paths(dst, rev)
                if not changed_paths:
                    return
                urls = []
                for p in changed_paths:
                    decoded_path = p
                    # URL decode (unquote_to_bytes -> UTF-8, compatible Python 3.6)
                    from urllib.parse import unquote_to_bytes
                    raw = unquote_to_bytes(p)
                    decoded_path = raw.decode("utf-8")
                    full_url = base_url.rstrip("/") + decoded_path + "(V" + str(rev) + ")"
                    urls.append(full_url)
                self.root.after(0, lambda: self._display_commit_paths(urls))
            except Exception as e:
                self._log(self.log_auto, "\n[路径导出] 获取提交文件路径失败: " + str(e) + "\n")

        threading.Thread(target=run, daemon=True).start()

    def _get_wc_last_revision(self, checkout_dir):
        """获取工作副本最后变更版本号（用 `svn info --xml` 的 commit/@revision）。"""
        try:
            rc, out = self._run_svn_bytes("info", "--xml", checkout_dir, force_utf8=True)
            if rc != 0:
                return None
            entry = ET.fromstring(out).find(".//entry")
            commit = entry.find("commit") if entry is not None else None
            rev = commit.get("revision") if commit is not None else (entry.get("revision") if entry is not None else None)
            return int(rev) if rev else None
        except Exception:
            return None

    def _get_repo_root_http_url(self, checkout_dir):
        """获取仓库根 URL。用 `svn info --xml` 解析，避免依赖被本地化的文本字段。"""
        try:
            rc, out = self._run_svn_bytes("info", "--xml", checkout_dir, force_utf8=True)
            if rc != 0:
                return None
            node = ET.fromstring(out).find(".//repository/root")
            if node is None or not node.text:
                return None
            root = node.text.strip()
            # svn:// 无法在浏览器直接访问，转 https；http/https 原样保留
            if root.startswith("svn://"):
                root = "https://" + root[6:]
            # XML 中的 URL 可能对中文做了百分号编码，解码为可读形式
            from urllib.parse import unquote_to_bytes
            return unquote_to_bytes(root).decode("utf-8")
        except Exception:
            return None

    def _get_changed_paths(self, checkout_dir, rev):
        """获取某次提交变更的文件路径。用 `svn log --xml -v` 解析，locale 无关。"""
        try:
            rc, out = self._run_svn_bytes("log", "--xml", "-v", "-r", str(rev), checkout_dir, force_utf8=True)
            if rc != 0:
                return []
            # <logentry><paths><path action="M">/xxx</path>...</paths></logentry>
            return [p.text.strip() for p in ET.fromstring(out).findall(".//logentry/paths/path") if p.text]
        except Exception:
            return []

    def _display_commit_paths(self, urls):
        self._commit_urls = urls
        self.txt_paths.config(state=tk.NORMAL)
        self.txt_paths.delete(1.0, tk.END)
        self.txt_paths.insert(tk.END, "\n".join(urls))
        self.txt_paths.config(state=tk.DISABLED)
        self.lbl_paths_count.config(text="共 " + str(len(urls)) + " 个文件")
        self.btn_copy_paths.config(state=tk.NORMAL)

    def _copy_commit_paths(self):
        if not self._commit_urls:
            return
        text = "\n".join(self._commit_urls)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.btn_copy_paths.config(text="已复制")
        self.root.after(2000, lambda: self.btn_copy_paths.config(text="复制文件路径"))

    def _clear_paths_display(self):
        self._commit_urls = []
        self.txt_paths.config(state=tk.NORMAL)
        self.txt_paths.delete(1.0, tk.END)
        self.txt_paths.config(state=tk.DISABLED)
        self.lbl_paths_count.config(text="（暂无）")
        self.btn_copy_paths.config(state=tk.DISABLED, text="复制文件路径")

    def _auto_done(self, ok):
        self.btn_auto.config(state=tk.NORMAL, text="一键执行：拉取 -> 覆盖 -> 提交")
        if ok and self._commit_urls:
            messagebox.showinfo("完成", "全自动流程执行完成！\n提交文件路径已导出。")
        elif ok:
            messagebox.showinfo("完成", "全自动流程执行完成！")

    def _build_tab5(self, t5):
        from svn_path_generator import SvnPathGeneratorTab
        self._tab5 = SvnPathGeneratorTab(t5)
        self._tab5.build()

    def sync_checkout_to_target(self, *args):
        self.target_dir.set(self.checkout_dir.get())


if __name__ == "__main__":
    root = ttk.Window(themename="flatly")
    app = SvnSyncTool(root)
    if hasattr(app.checkout_dir, "trace_add"):
        app.checkout_dir.trace_add("write", app.sync_checkout_to_target)
    else:
        app.checkout_dir.trace("w", app.sync_checkout_to_target)
    root.mainloop()
