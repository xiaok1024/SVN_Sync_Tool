# -*- coding: utf-8 -*-

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import svn_sync_cli as cli


class FailingCopyEngine:
    def __init__(self, source):
        self.source = source
        self.unlock_called = False

    @staticmethod
    def _run_svn(_widget, *_args):
        return 0, ""

    def _resolve_source_path(self, _source, log=None):
        return self.source

    @staticmethod
    def _scan_cross_files(_target, _source):
        return [("A.java", "/missing/A.java", "/target/A.java")]

    @staticmethod
    def _copy_cross_files(_entries, on_result=None):
        if on_result:
            on_result("A.java", False, "copy failed")
        return [], [("A.java", "copy failed")]

    def _unlock_svn_locks_before_commit(self, _widget, _target):
        self.unlock_called = True
        return True


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

    def test_cli_reuses_shared_macos_html_clipboard_reader(self):
        engine = cli.CliEngine()
        with mock.patch.object(cli.core, "IS_WINDOWS", False), \
                mock.patch.object(cli.core, "IS_MACOS", True), \
                mock.patch.object(cli.core, "read_clipboard_html_macos", return_value="<b>QC</b>"):
            text, kind = engine._read_clipboard_content()
        self.assertEqual((text, kind), ("<b>QC</b>", "html"))

    def test_auto_pipeline_does_not_commit_after_partial_copy_failure(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            source = os.path.join(root, "source")
            os.makedirs(os.path.join(target, ".svn"))
            os.makedirs(source)
            engine = FailingCopyEngine(source)
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli.run_auto(
                    engine, "https://svn.example.com/svn/customer", target, source,
                    "update", "test", assume_yes=True)
        self.assertFalse(result)
        self.assertFalse(engine.unlock_called)


if __name__ == "__main__":
    unittest.main()
