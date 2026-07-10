# -*- coding: utf-8 -*-

import unittest

from svn_path_generator import parse_revision_spec


class RevisionSpecTest(unittest.TestCase):
    def test_accepts_comma_and_whitespace_separators(self):
        self.assertEqual(parse_revision_spec("499,500"), [499, 500])
        self.assertEqual(parse_revision_spec("499 500"), [499, 500])
        self.assertEqual(parse_revision_spec("499, 500\t501"), [499, 500, 501])

    def test_accepts_ranges_and_mixed_separators(self):
        self.assertEqual(parse_revision_spec("499,500-502 510"), [499, 500, 501, 502, 510])


if __name__ == "__main__":
    unittest.main()
