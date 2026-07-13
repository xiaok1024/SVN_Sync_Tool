# -*- coding: utf-8 -*-

import unittest
from unittest import mock

import svn_path_generator as path_generator
from svn_sync_core import run_svn_command


class RevisionSpecTest(unittest.TestCase):
    def test_accepts_comma_and_whitespace_separators(self):
        self.assertEqual(path_generator.parse_revision_spec("499,500"), [499, 500])
        self.assertEqual(path_generator.parse_revision_spec("499 500"), [499, 500])
        self.assertEqual(path_generator.parse_revision_spec("499, 500\t501"), [499, 500, 501])

    def test_accepts_ranges_and_mixed_separators(self):
        self.assertEqual(path_generator.parse_revision_spec("499,500-502 510"), [499, 500, 501, 502, 510])

    def test_uses_shared_svn_command_runner(self):
        self.assertIs(path_generator.run_svn_command, run_svn_command)

    def test_query_and_url_building_are_shared_by_gui_and_cli(self):
        xml = (
            '<?xml version="1.0"?><log><logentry revision="499"><paths>'
            '<path kind="dir" action="M">/ecology/src</path>'
            '<path kind="file" action="M">/ecology/src/%E6%B5%8B%E8%AF%95.java</path>'
            '<path kind="file" action="M">/ecology/src/B.java</path>'
            '</paths></logentry></log>'
        )
        with mock.patch.object(path_generator, "run_svn_command", return_value=(0, xml)):
            results, errors = path_generator.query_revision_paths(
                "https://svn.example.com/svn/customer", "499")
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        rows = path_generator.build_revision_url_rows(
            results, "https://svn.example.com/svn/customer", "name")
        self.assertEqual([row[0] for row in rows], [
            "https://svn.example.com/svn/customer/ecology/src/B.java(V499)",
            "https://svn.example.com/svn/customer/ecology/src/测试.java(V499)",
        ])


if __name__ == "__main__":
    unittest.main()
