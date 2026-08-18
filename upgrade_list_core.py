# -*- coding: utf-8 -*-
"""升级清单富文本解析与 Markdown 生成的无界面核心。"""

import os
import re
from bisect import bisect_right
from collections import Counter, OrderedDict, defaultdict
from html.parser import HTMLParser
from urllib.parse import quote

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
    css_text = css_text or ""
    # 原实现使用跨全文回溯正则；对很长且没有左花括号的样式文本会呈
    # 二次复杂度。先线性切分花括号，再识别与原正则相同的
    # ``非花括号文本 { 非花括号文本 }`` 序列。
    segments = []
    braces = []
    segment_start = 0
    for index, char in enumerate(css_text):
        if char not in "{}":
            continue
        segments.append(css_text[segment_start:index])
        braces.append(char)
        segment_start = index + 1
    segments.append(css_text[segment_start:])

    rules = []
    for index in range(len(braces) - 1):
        if braces[index] != "{" or braces[index + 1] != "}":
            continue
        selector_text = segments[index]
        body = segments[index + 1]
        if not selector_text or not body:
            continue
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


class _StyleBlockHTMLParser(HTMLParser):
    """线性收集文档内的 style 文本，支持 HTML 合法的结束标签空白。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_style = False
        self.current_parts = []
        self.style_blocks = []

    def _append(self, value):
        if self.in_style and value:
            self.current_parts.append(value)

    def handle_starttag(self, tag, _attrs):
        if tag.lower() == "style":
            self.in_style = True
            self.current_parts = []

    def handle_endtag(self, tag):
        if tag.lower() == "style" and self.in_style:
            self.style_blocks.append("".join(self.current_parts))
            self.in_style = False
            self.current_parts = []

    def handle_data(self, data):
        self._append(data)

    def handle_entityref(self, name):
        self._append("&%s;" % name)

    def handle_charref(self, name):
        self._append("&#%s;" % name)


def rt_extract_style_blocks(html):
    parser = _StyleBlockHTMLParser()
    parser.feed(html or "")
    parser.close()
    return parser.style_blocks


def rt_analyze_html(html, strict=False):
    css_rules = []
    for style_text in rt_extract_style_blocks(html):
        css_rules.extend(rt_parse_css_color_rules(style_text))
    parser = RedTextHTMLParser(strict=strict, css_rules=css_rules)
    parser.feed(html)
    parser.close()
    return parser.get_line_records()


def rt_parse_repository_path_from_line(line):
    """解析 ``$/仓库/相对路径(V数字)``，允许版本号后跟空白分隔的备注。"""
    candidate = (line or "").strip()
    if not candidate.startswith("$/"):
        return None

    version_start = None
    version_end = None
    cursor = 2
    while True:
        possible_start = candidate.find("(", cursor)
        if possible_start < 0:
            break
        digit_start = possible_start + 2
        if (
            digit_start < len(candidate)
            and candidate[possible_start + 1:digit_start] in {"V", "v"}
            and candidate[digit_start].isdigit()
        ):
            possible_end = digit_start + 1
            while possible_end < len(candidate) and candidate[possible_end].isdigit():
                possible_end += 1
            if (
                possible_end < len(candidate)
                and candidate[possible_end] == ")"
                and (possible_end + 1 == len(candidate) or candidate[possible_end + 1].isspace())
            ):
                version_start = possible_start
                version_end = possible_end
                break
        cursor = possible_start + 1

    if version_start is None:
        return None

    repository_path = candidate[2:version_start]
    separator = repository_path.find("/")
    if separator <= 0 or separator == len(repository_path) - 1:
        return None
    repository = repository_path[:separator]
    relative_path = repository_path[separator + 1:]
    version = candidate[version_start + 1:version_end]
    return repository, relative_path, rt_normalize_version(version)


def rt_contains_svn_url(text):
    """线性判断文本中是否存在带版本号的完整 SVN URL 或 ``$/`` 路径。"""
    if rt_parse_repository_path_from_line(text):
        return True
    for token in (text or "").split():
        version_positions = []
        cursor = 0
        while True:
            version_start = token.find("(", cursor)
            if version_start < 0:
                break
            digit_start = version_start + 2
            if (
                digit_start < len(token)
                and token[version_start + 1:digit_start] in {"V", "v"}
                and token[digit_start].isdigit()
            ):
                digit_end = digit_start + 1
                while digit_end < len(token) and token[digit_end].isdigit():
                    digit_end += 1
                if digit_end < len(token) and token[digit_end] == ")":
                    version_positions.append(version_start)
            cursor = version_start + 1
        if not version_positions:
            continue

        index = 0
        while index < len(token):
            if token.startswith("https://", index):
                authority_start = index + len("https://")
            elif token.startswith("http://", index):
                authority_start = index + len("http://")
            else:
                index += 1
                continue
            first_slash = token.find("/", authority_start)
            if (
                first_slash > authority_start
                and token.startswith("/svn/", first_slash)
                and bisect_right(version_positions, first_slash + len("/svn/")) < len(version_positions)
            ):
                return True
            index = authority_start
    return False


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
        (customer, path, version)
        for customer, path, version, _is_repository_path in rt_parse_file_references_from_line(line)
    ]


def rt_parse_file_references_from_line(line):
    references = [
        (customer, path, rt_normalize_version(version), False)
        for customer, path, version in RT_MD_SVN_URL_RE.findall(line.strip())
    ]
    repository_path = rt_parse_repository_path_from_line(line)
    if repository_path:
        customer, path, version = repository_path
        references.append((customer, path, version, True))
    return references


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
            parsed_references = rt_parse_file_references_from_line(url_line)
            if not parsed_references:
                raise ValueError("无法解析 SVN URL: " + line)
            for customer, relative_path, version, is_repository_path in parsed_references:
                if not is_repository_path and rt_is_standard_ecology_file(customer, relative_path):
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
                link_destination = quote(relative_path, safe="/-._~%")
                lines.append(
                    "  - [`%s`](%s) `版本: %s` `标识: %s`"
                    % (relative_path, link_destination, version_text, marker_text)
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

__all__ = [
    name for name in globals()
    if name.startswith(("RT_", "rt_")) or name in {"RedTextHTMLParser", "RTFileEntry", "RTQCEntry"}
]
