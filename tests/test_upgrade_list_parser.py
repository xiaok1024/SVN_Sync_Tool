# -*- coding: utf-8 -*-

import unittest

import svn_sync_tool as tool


class UpgradeListParserTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
