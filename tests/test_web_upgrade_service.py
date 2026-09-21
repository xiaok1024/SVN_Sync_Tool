# -*- coding: utf-8 -*-

import unittest
from unittest import mock

import web_upgrade_service as service


SAMPLE_HTML = (
    '<style>.upgrade { color: rgb(255, 0, 0); }</style>'
    '<div>QC123 修复登录问题 —— 门户</div>'
    '<div class="upgrade">https://svn.example.com/svn/customer/ecology/src/A.java(V12)</div>'
    '<div style="color: black">https://svn.example.com/svn/customer/ecology/src/B.java(V13)</div>'
)


class UpgradeWebServiceTest(unittest.TestCase):
    def test_extract_returns_editable_list_and_color_summary(self):
        result = service.extract_upgrade_list(SAMPLE_HTML)

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], {
            "qc_count": 1,
            "file_line_count": 2,
            "red_count": 1,
            "black_count": 1,
        })
        self.assertIn("QC123 修复登录问题 —— 门户", result["list_text"])
        self.assertIn("[red] https://svn.example.com/svn/customer/ecology/src/A.java(V12)",
                      result["list_text"])
        self.assertTrue(result["list_text"].endswith("\n"))
        self.assertEqual(result["warnings"], [])

    def test_extract_warns_when_qc_or_red_marker_is_missing(self):
        html = (
            '<div style="color: black">'
            'https://svn.example.com/svn/customer/ecology/src/B.java(V13)'
            '</div>'
        )
        result = service.extract_upgrade_list(html)
        warning_codes = {item["code"] for item in result["warnings"]}
        self.assertEqual(warning_codes, {"no_qc_header", "no_red_marker"})

    def test_repository_relative_paths_are_extracted_and_can_generate_markdown(self):
        html = (
            '<div>QC123456 仓库相对路径 —— 集成模块</div>'
            '<div style="color: red">'
            '$/Y示例客户/src/weaver/interfaces/hrm/DemoHrmSync.java(V2) - 不交叉'
            '</div>'
            '<div style="color: black">'
            '$/Y示例客户/WEB-INF/prop/demo-hrm.properties(V1)'
            '</div>'
        )
        extracted = service.extract_upgrade_list(html)
        self.assertEqual(extracted["summary"], {
            "qc_count": 1,
            "file_line_count": 2,
            "red_count": 1,
            "black_count": 1,
        })
        self.assertIn("[red] $/Y示例客户/src/weaver/interfaces/hrm/DemoHrmSync.java(V2) - 不交叉",
                      extracted["list_text"])

        generated = service.generate_upgrade_markdown(extracted["list_text"], "ai-md")
        self.assertEqual(generated["customer"], "Y示例客户")
        self.assertEqual(
            generated["filename"],
            "Y示例客户-upgrade-file-list-ai.md",
        )
        self.assertIn("path: `src/weaver/interfaces/hrm/DemoHrmSync.java`", generated["content"])
        self.assertIn("path: `WEB-INF/prop/demo-hrm.properties`", generated["content"])

    def test_extract_rejects_html_without_versioned_svn_file(self):
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.extract_upgrade_list("<div>QC123 没有文件 —— 门户</div>")
        self.assertEqual(raised.exception.code, "no_svn_file")

    def test_generate_uses_edited_list_for_human_and_ai_markdown(self):
        list_text = service.extract_upgrade_list(SAMPLE_HTML)["list_text"]
        edited = list_text.replace("修复登录问题", "用户校对后的标题")

        human = service.generate_upgrade_markdown(edited, "md")
        ai = service.generate_upgrade_markdown(edited, "ai-md")

        self.assertEqual(human["filename"], "customer-upgrade-file-list.md")
        self.assertIn("用户校对后的标题", human["content"])
        self.assertEqual(ai["filename"], "customer-upgrade-file-list-ai.md")
        self.assertIn("action: `migrate`", ai["content"])
        self.assertIn("reason: `black-context-file`", ai["content"])
        self.assertEqual(ai["stats"]["qc"], 1)
        self.assertEqual(ai["stats"]["unique_files"], 2)

    def test_download_filename_uses_a_cross_platform_safe_customer_name(self):
        list_text = (
            "QC123 下载文件名 —— 门户\n"
            "[red] $/A客户:测试*?\\名称/src/Test.java(V1)\n"
        )
        human = service.generate_upgrade_markdown(list_text, "md")
        ai = service.generate_upgrade_markdown(list_text, "ai-md")
        self.assertEqual(human["filename"], "A客户_测试_名称-upgrade-file-list.md")
        self.assertEqual(ai["filename"], "A客户_测试_名称-upgrade-file-list-ai.md")
        self.assertNotRegex(human["filename"], r'[<>:"/\\|?*]')

    def test_color_filter_applies_before_version_merge_and_duplicate_stats(self):
        list_text = (
            "QC123 混合颜色 —— 门户\n"
            "[red] $/customer/src/Shared.java(V12)\n"
            "[red] $/customer/src/Shared.java(V12)\n"
            "[black] $/customer/src/Shared.java(V13)\n"
            "[black] $/customer/src/Shared.java(V13)\n"
            "\nQC124 仅黑色 —— 门户\n"
            "[black] $/customer/src/Black.java(V14)\n"
        )
        for output_format in ("md", "ai-md"):
            with self.subTest(format=output_format):
                red = service.generate_upgrade_markdown(
                    list_text, output_format, include_black=False)
                self.assertIn("Shared.java", red["content"])
                self.assertIn("V12", red["content"])
                self.assertNotIn("V13", red["content"])
                self.assertNotIn("Black.java", red["content"])
                self.assertNotIn("QC124", red["content"])
                self.assertEqual(red["stats"]["qc"], 1)
                self.assertEqual(red["stats"]["unique_files"], 1)
                black = service.generate_upgrade_markdown(
                    list_text, output_format, include_red=False)
                self.assertNotIn("V12", black["content"])
                self.assertIn("V13", black["content"])
                self.assertIn("Black.java", black["content"])
                self.assertEqual(black["stats"]["skip_black_context"], 2)

    def test_color_filter_defaults_preserve_existing_output(self):
        list_text = service.extract_upgrade_list(SAMPLE_HTML)["list_text"]
        for output_format in ("md", "ai-md"):
            self.assertEqual(
                service.generate_upgrade_markdown(list_text, output_format),
                service.generate_upgrade_markdown(list_text, output_format, True, True))

    def test_color_filter_empty_selection_and_no_matching_files(self):
        list_text = "QC123 仅红色 —— 门户\n[red] $/customer/src/A.java(V1)\n"
        for red, black, code in ((False, False, "no_color_selected"),
                                 (False, True, "no_matching_files")):
            with self.subTest(code=code):
                with self.assertRaises(service.UpgradeWebError) as raised:
                    service.generate_upgrade_markdown(list_text, "md", red, black)
                self.assertEqual(raised.exception.code, code)

    def test_color_filter_preserves_unmarked_red_and_selected_customer(self):
        list_text = (
            "QC123 多客户 —— 门户\n"
            "$/red-customer/src/A.java(V1)\n"
            "[black] $/black-customer/src/B.java(V2)\n"
            "[black] $/black-customer/src/C.java(V3)\n"
        )
        red = service.generate_upgrade_markdown(list_text, "ai-md", include_black=False)
        self.assertEqual(red["customer"], "red-customer")
        self.assertEqual(red["warnings"], [])
        self.assertIn("A.java", red["content"])
        self.assertNotIn("black-customer", red["content"])

    def test_color_filter_does_not_hide_invalid_input(self):
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.generate_upgrade_markdown(
                "QC123 错误清单 —— 门户\n[red] $/customer/src/A.java(V1)\n"
                "[black] 无效路径\n", "md", include_black=False)
        self.assertEqual(raised.exception.code, "invalid_svn_url")

    def test_download_filename_has_a_utf8_byte_limit_and_fallback(self):
        long_customer = "客户" * 200
        long_list = (
            "QC123 长客户名 —— 门户\n"
            "[red] $/%s/src/Test.java(V1)\n" % long_customer
        )
        result = service.generate_upgrade_markdown(long_list, "ai-md")
        self.assertLessEqual(
            len(result["filename"].encode("utf-8")),
            service.MAX_DOWNLOAD_FILENAME_BYTES,
        )
        self.assertTrue(result["filename"].endswith("-upgrade-file-list-ai.md"))

        fallback = service.generate_upgrade_markdown(
            "QC123 空安全名称 —— 门户\n[red] $/.../src/Test.java(V1)\n",
            "md",
        )
        self.assertEqual(fallback["filename"], "customer-upgrade-file-list.md")

    def test_generate_reports_multiple_customer_names(self):
        list_text = (
            "QC123 多客户清单 —— 门户\n"
            "[red] https://svn.example.com/svn/customer-a/ecology/src/A.java(V12)\n"
            "[red] https://svn.example.com/svn/customer-b/ecology/src/B.java(V13)\n"
        )
        result = service.generate_upgrade_markdown(list_text, "md")
        self.assertEqual(result["warnings"][0]["code"], "multiple_customers")
        self.assertEqual(result["filename"], "customer-a-upgrade-file-list.md")

    def test_standard_ecology_file_does_not_trigger_multiple_customer_warning(self):
        list_text = (
            "QC123 标准文件与客户文件 —— 门户\n"
            "[red] https://svn.example.com/svn/ecology/trunk/src/Standard.java(V11)\n"
            "[red] https://svn.example.com/svn/customer-a/ecology/src/Customer.java(V12)\n"
        )
        result = service.generate_upgrade_markdown(list_text, "md")
        self.assertEqual(result["customer"], "customer-a")
        self.assertEqual(result["warnings"], [])

    def test_svn_url_in_qc_title_does_not_trigger_multiple_customer_warning(self):
        list_text = (
            "QC123 说明 https://svn.example.com/svn/customer-b/ecology/src/Note.java(V11) —— 门户\n"
            "[red] https://svn.example.com/svn/customer-a/ecology/src/Customer.java(V12)\n"
        )
        result = service.generate_upgrade_markdown(list_text, "md")
        self.assertEqual(result["customer"], "customer-a")
        self.assertEqual(result["warnings"], [])

    def test_generate_maps_core_error_without_returning_input_line(self):
        malicious = "</textarea><script>alert('x')</script>"
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.generate_upgrade_markdown(malicious, "md")
        self.assertEqual(raised.exception.code, "invalid_qc_header")
        self.assertNotIn("script", raised.exception.message)

    def test_input_size_limit_is_enforced_before_core_processing(self):
        oversized = "x" * (service.MAX_HTML_BYTES + 1)
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.extract_upgrade_list(oversized)
        self.assertEqual(raised.exception.status_code, 413)

    def test_html_complexity_limits_are_enforced_before_core_processing(self):
        limits = (
            ("MAX_HTML_TAGS", 1),
            ("MAX_STYLE_BYTES", 1),
            ("MAX_CSS_RULES", 0),
            ("MAX_SELECTOR_CHECKS", 1),
            ("MAX_SELECTOR_BYTES", 1),
            ("MAX_TOTAL_SELECTOR_BYTES", 1),
            ("MAX_SELECTOR_SCAN_BYTES", 1),
        )
        for constant, limit in limits:
            with self.subTest(constant=constant):
                with mock.patch.object(service, constant, limit):
                    with self.assertRaises(service.UpgradeWebError) as raised:
                        service.extract_upgrade_list(SAMPLE_HTML)
                self.assertEqual(raised.exception.code, "html_too_complex")
                self.assertEqual(raised.exception.status_code, 413)

    def test_malformed_tag_and_style_shapes_are_rejected_linearly(self):
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.extract_upgrade_list("<" * (service.MAX_HTML_TAGS + 1))
        self.assertEqual(raised.exception.code, "html_too_complex")

        with self.assertRaises(service.UpgradeWebError) as raised:
            service.extract_upgrade_list("<style>" + ("x" * 100_000))
        self.assertEqual(raised.exception.code, "html_too_complex")

    def test_list_shape_limits_are_enforced_before_core_processing(self):
        list_text = (
            "QC123 清单限制 —— 门户\n"
            "[red] https://svn.example.com/svn/customer/ecology/src/A.java(V12)\n"
        )
        for constant, limit in (("MAX_LIST_LINES", 1), ("MAX_LINE_BYTES", 4)):
            with self.subTest(constant=constant):
                with mock.patch.object(service, constant, limit):
                    with self.assertRaises(service.UpgradeWebError) as raised:
                        service.generate_upgrade_markdown(list_text, "md")
                self.assertEqual(raised.exception.code, "list_too_large")
                self.assertEqual(raised.exception.status_code, 413)

    def test_invalid_format_is_rejected(self):
        list_text = service.extract_upgrade_list(SAMPLE_HTML)["list_text"]
        with self.assertRaises(service.UpgradeWebError) as raised:
            service.generate_upgrade_markdown(list_text, "html")
        self.assertEqual(raised.exception.code, "invalid_format")


if __name__ == "__main__":
    unittest.main()
