# -*- coding: utf-8 -*-

import unittest

import svn_sync_tool as tool
import upgrade_list_core as core


class UpgradeListParserTest(unittest.TestCase):
    def test_svn_url_detection_is_linear_for_repeated_unversioned_prefixes(self):
        malformed = "https://a/svn/customer/" * 5_000
        self.assertFalse(core.rt_contains_svn_url(malformed))

    def test_svn_url_detection_keeps_supported_url_behavior(self):
        self.assertTrue(core.rt_contains_svn_url(
            "前缀 https://svn.example.com/svn/customer/ecology/src/A.java(V12) 后缀"
        ))
        self.assertTrue(core.rt_contains_svn_url(
            "http://svn.example.com/svn/customer/ecology/src/A.java(v12),"
        ))
        self.assertFalse(core.rt_contains_svn_url(
            "https://svn.example.com/svn/customer/ecology/src/A.java"
        ))
        self.assertFalse(core.rt_contains_svn_url(
            "https://svn.example.com/other/customer/ecology/src/A.java(V12)"
        ))

    def test_repository_relative_path_detection_supports_chinese_and_spaces(self):
        path = "$/Y示例客户/sql/for Oracle/升级脚本.sql(v12) - 不交叉"
        self.assertTrue(core.rt_contains_svn_url(path))
        self.assertEqual(
            core.rt_parse_repository_path_from_line(path),
            ("Y示例客户", "sql/for Oracle/升级脚本.sql", "V12"),
        )
        self.assertFalse(core.rt_contains_svn_url(
            "$/Y示例客户/sql/for Oracle/升级脚本.sql"
        ))
        self.assertFalse(core.rt_contains_svn_url("$/OnlyRepository(V12)"))

    def test_css_rule_parser_handles_long_non_rule_text_without_backtracking(self):
        self.assertEqual(core.rt_parse_css_color_rules("x" * 100_000), [])

    def test_css_rule_parser_keeps_supported_selector_behavior(self):
        rules = core.rt_parse_css_color_rules(
            "div, .upgrade { color: rgb(255, 0, 0); }"
            "a:hover { color: black; }"
            "#context { color: #222; }"
        )
        self.assertEqual(rules, [
            ("div", "rgb(255, 0, 0)"),
            (".upgrade", "rgb(255, 0, 0)"),
            ("#context", "#222"),
        ])

    def test_style_prescan_is_linear_and_accepts_html_end_tag_whitespace(self):
        html = (
            '<div>QC124 后置样式 —— 门户</div>'
            '<div class="upgrade">https://svn.example.com/svn/customer/ecology/src/C.java(V14)</div>'
            '<style>.upgrade { color: red; }</style >'
        )
        lines = core.rt_extract_list_from_html(html)
        self.assertIn(
            "[red] https://svn.example.com/svn/customer/ecology/src/C.java(V14)",
            lines,
        )
        self.assertEqual(core.rt_analyze_html("<style>x</style >" * 5_000), [])

    def test_extracts_color_markers_and_builds_ai_actions(self):
        html = (
            '<style>.upgrade { color: rgb(255, 0, 0); }</style>'
            '<div>QC123 修复问题 —— 门户</div>'
            '<div class="upgrade">https://svn.example.com/svn/customer/ecology/src/A.java(V12)</div>'
            '<div style="color: black">https://svn.example.com/svn/customer/ecology/src/B.java(V13)</div>'
        )
        lines = tool.rt_extract_list_from_html(html)
        self.assertIn("[red] https://svn.example.com/svn/customer/ecology/src/A.java(V12)", lines)
        self.assertIn("[black] https://svn.example.com/svn/customer/ecology/src/B.java(V13)", lines)

        entries, customer, raw_counter = tool.rt_parse_txt("\n".join(lines))
        markdown = tool.rt_build_ai_md(entries, customer, raw_counter)
        self.assertEqual(customer, "customer")
        self.assertIn("path: `ecology/src/A.java`", markdown)
        self.assertIn("action: `migrate`", markdown)
        self.assertIn("path: `ecology/src/B.java`", markdown)
        self.assertIn("reason: `black-context-file`", markdown)
        self.assertIn("upgrade_scope: `context-only`", markdown)

    def test_extracts_and_generates_repository_relative_paths_without_filtering(self):
        html = (
            '<div>QC123456 支持仓库相对路径 —— 集成模块</div>'
            '<div style="color: red">'
            '$/Y示例客户/src/weaver/interfaces/hrm/DemoHrmSync.java(V2)'
            '</div>'
            '<div style="color: black">'
            '$/Y示例客户/sql/for Oracle/upgrade_demo.sql(V1)'
            '</div>'
        )
        lines = core.rt_extract_list_from_html(html)
        self.assertIn(
            "[red] $/Y示例客户/src/weaver/interfaces/hrm/DemoHrmSync.java(V2)",
            lines,
        )
        self.assertIn(
            "[black] $/Y示例客户/sql/for Oracle/upgrade_demo.sql(V1)",
            lines,
        )

        entries, customer, raw_counter = core.rt_parse_txt("\n".join(lines))
        self.assertEqual(customer, "Y示例客户")
        self.assertIn("src/weaver/interfaces/hrm/DemoHrmSync.java", entries[0].files)
        self.assertIn("sql/for Oracle/upgrade_demo.sql", entries[0].files)
        human_markdown = core.rt_build_human_md(entries)
        self.assertIn(
            "(sql/for%20Oracle/upgrade_demo.sql)",
            human_markdown,
        )
        ai_markdown = core.rt_build_ai_md(entries, customer, raw_counter)
        self.assertIn("path: `sql/for Oracle/upgrade_demo.sql`", ai_markdown)

    def test_repository_relative_path_with_trailing_note_is_preserved_and_parsed(self):
        html = (
            '<div>QC123 保留路径尾注 —— 门户</div>'
            '<div style="color: red">'
            '$/Y客户/src/weaver/Test.java(V3) - 不交叉'
            '</div>'
        )
        lines = core.rt_extract_list_from_html(html)
        self.assertIn("[red] $/Y客户/src/weaver/Test.java(V3) - 不交叉", lines)
        entries, customer, _raw_counter = core.rt_parse_txt("\n".join(lines))
        self.assertEqual(customer, "Y客户")
        self.assertIn("src/weaver/Test.java", entries[0].files)

    def test_human_markdown_does_not_double_encode_existing_url_path(self):
        list_text = (
            "QC123 保留已有路径编码 —— 门户\n"
            "[red] https://svn.example.com/svn/Y客户/sql/for%20Oracle/"
            "%E5%8D%87%E7%BA%A7.sql(V1)\n"
        )
        entries, _customer, _raw_counter = core.rt_parse_txt(list_text)
        markdown = core.rt_build_human_md(entries)
        self.assertIn(
            "(sql/for%20Oracle/%E5%8D%87%E7%BA%A7.sql)",
            markdown,
        )
        self.assertNotIn("%2520", markdown)

    def test_repository_relative_standard_path_is_not_filtered(self):
        list_text = (
            "QC123 保留仓库相对标准文件 —— 门户\n"
            "[red] $/ecology/trunk/src/Standard.java(V9)\n"
        )
        entries, customer, _raw_counter = core.rt_parse_txt(list_text)
        self.assertEqual(customer, "ecology")
        self.assertIn("trunk/src/Standard.java", entries[0].files)


if __name__ == "__main__":
    unittest.main()
