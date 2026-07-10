# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest
from unittest import mock

import svn_sync_cli as cli


class CliConfigTest(unittest.TestCase):
    def test_normalize_revision_input_removes_surrogate_and_hidden_text(self):
        self.assertEqual(cli.normalize_revision_input("499,\udcef500abc"), "499,500")

    def test_save_defaults_is_atomic_and_sanitizes_surrogate(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "cli.json")
            defaults = {"svn_url": "http://example/svn/客户", "revisions": "30"}
            with mock.patch.object(cli, "CONFIG_DIR", directory), \
                    mock.patch.object(cli, "CONFIG_PATH", config_path):
                cli.save_defaults(defaults, revisions="499,\udcef500")
            with open(config_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["svn_url"], "http://example/svn/客户")
            self.assertNotIn("\udcef", saved["revisions"])
            self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
