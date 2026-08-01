# -*- coding: utf-8 -*-

import ast
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from svn_sync_core import (
    SyncEngine,
    parse_svn_log_file_paths,
    redact_sensitive_text,
    redact_svn_command,
    run_svn_command,
)


class RevisionUrlEngine(SyncEngine):
    @staticmethod
    def _get_repo_root_http_url(_checkout_dir):
        return "https://svn.example.com/svn/客户"

    @staticmethod
    def _get_changed_paths(_checkout_dir, _revision):
        return ["/ecology/src/%E6%B5%8B%E8%AF%95.java", "ecology/src/B.java"]


class CapturingEngine(SyncEngine):
    def __init__(self):
        super().__init__()
        self.messages = []

    def _log(self, _widget, message):
        self.messages.append(message)


class FakeStdout(list):
    def close(self):
        pass


class FakeProcess:
    def __init__(self, output=b"ok\n"):
        self.stdout = FakeStdout([output])
        self.returncode = 0

    def wait(self):
        return self.returncode


class TimeoutProcess:
    returncode = -1

    def communicate(self, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(
                ["svn", "--password", "secret", "status"], timeout)
        return b"", b""

    def kill(self):
        pass


class SyncCoreTest(unittest.TestCase):
    def test_redact_svn_command_hides_password(self):
        command = ["svn", "--username", "user", "--password", "secret", "status"]
        redacted = redact_svn_command(command)
        self.assertEqual(redacted[-2], "******")
        self.assertNotIn("secret", " ".join(redacted))
        self.assertIn("secret", command)

    def test_run_svn_logs_only_redacted_command(self):
        engine = CapturingEngine()
        engine.svn_user = "user"
        engine.svn_pass = "secret"
        with mock.patch("svn_sync_core.subprocess.Popen", return_value=FakeProcess()) as popen:
            rc, output = engine._run_svn(None, "status", "/tmp/wc")
        self.assertEqual((rc, output), (0, "ok\n"))
        self.assertNotIn("secret", "".join(engine.messages))
        self.assertIn("******", "".join(engine.messages))
        self.assertIn("secret", popen.call_args.args[0])

    def test_run_svn_redacts_password_if_subprocess_echoes_it(self):
        engine = CapturingEngine()
        engine.svn_user = "user"
        engine.svn_pass = "secret"
        process = FakeProcess(b"authentication failed for secret\n")
        with mock.patch("svn_sync_core.subprocess.Popen", return_value=process):
            _rc, output = engine._run_svn(None, "status")
        self.assertNotIn("secret", output)
        self.assertNotIn("secret", "".join(engine.messages))

    def test_run_svn_timeout_does_not_expose_command_or_password(self):
        engine = SyncEngine()
        engine.svn_user = "user"
        engine.svn_pass = "secret"
        with mock.patch("svn_sync_core.subprocess.Popen", return_value=TimeoutProcess()):
            with self.assertRaises(TimeoutError) as raised:
                engine._run_svn_bytes("status", timeout=1)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("--password", str(raised.exception))

    def test_shared_runner_redacts_unexpected_exception_text(self):
        with mock.patch.object(
                SyncEngine, "_run_svn_bytes",
                side_effect=RuntimeError("failed command --password secret")):
            rc, output = run_svn_command(["status"], svn_user="user", svn_pass="secret")
        self.assertEqual(rc, -1)
        self.assertNotIn("secret", output)

    def test_sensitive_text_redacts_smb_userinfo_and_encoded_password(self):
        text = "mount smb://user:p%40ss@server/share failed for p@ss"
        redacted = redact_sensitive_text(text, ("p@ss",))
        self.assertNotIn("user", redacted)
        self.assertNotIn("p%40ss", redacted)
        self.assertNotIn("p@ss", redacted)

    def test_parse_svn_log_file_paths_filters_directories(self):
        xml = (
            '<?xml version="1.0"?><log><logentry revision="12"><paths>'
            '<path kind="dir" action="M">/ecology/src</path>'
            '<path kind="file" action="M">/ecology/src/A.java</path>'
            '</paths></logentry></log>'
        )
        self.assertEqual(parse_svn_log_file_paths(xml), ["/ecology/src/A.java"])

    def test_revision_urls_decode_paths_and_normalize_slashes(self):
        urls, relative_paths = RevisionUrlEngine()._get_revision_urls("/tmp/wc", 12)
        self.assertEqual(urls, [
            "https://svn.example.com/svn/客户/ecology/src/测试.java(V12)",
            "https://svn.example.com/svn/客户/ecology/src/B.java(V12)",
        ])
        self.assertEqual(relative_paths, ["ecology/src/测试.java", "ecology/src/B.java"])

    def test_copy_cross_files_is_shared_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.txt")
            target = os.path.join(root, "nested", "target.txt")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write("content")
            entries = [
                ("target.txt", source, target),
                ("missing.txt", os.path.join(root, "missing.txt"), os.path.join(root, "bad.txt")),
            ]
            copied, errors = SyncEngine._copy_cross_files(entries)
            self.assertEqual([item[0] for item in copied], ["target.txt"])
            self.assertEqual(errors[0][0], "missing.txt")
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "content")

    def test_gui_does_not_override_shared_core_operations(self):
        project_root = Path(__file__).resolve().parents[1]
        source = (project_root / "svn_sync_tool.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        gui_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "SvnSyncTool")
        gui_methods = {node.name for node in gui_class.body if isinstance(node, ast.FunctionDef)}
        shared_methods = {
            "_build_svn_cmd", "_svn_env", "_run_svn", "_run_svn_bytes",
            "_find_locked_svn_paths", "_unlock_svn_locks_before_commit",
            "_clean_share_text", "_is_share_address", "_share_to_smb_url", "_share_to_unc",
            "_precheck_source", "_resolve_source_path", "_find_existing_smb_mount",
            "_mount_smb_macos", "_safe_rmdir", "_cleanup_temp_mounts",
            "_scan_cross_files", "_copy_cross_files", "_parse_revision",
            "_get_wc_last_revision", "_get_repo_root_http_url", "_get_changed_paths",
            "_get_revision_urls",
        }
        self.assertFalse(gui_methods & shared_methods)

    def test_gui_mutating_shortcuts_keep_confirmation_gates(self):
        project_root = Path(__file__).resolve().parents[1]
        source = (project_root / "svn_sync_tool.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        gui_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "SvnSyncTool")
        methods = {node.name: node for node in gui_class.body if isinstance(node, ast.FunctionDef)}

        def ask_count(method_name):
            return sum(
                1 for node in ast.walk(methods[method_name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "askyesno"
            )

        self.assertGreaterEqual(ask_count("_confirm_quick_overwrite"), 1)
        self.assertGreaterEqual(ask_count("_start_auto_pipeline"), 2)

    def test_standard_tab_has_no_legacy_business_helpers(self):
        project_root = Path(__file__).resolve().parents[1]
        source = (project_root / "svn_standard_file_tab.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        top_level_functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertFalse(top_level_functions & {
            "_extract_rel_path", "_run_svn_cmd", "_load_customer_deploy_json",
            "_load_customer_env_info",
        })


if __name__ == "__main__":
    unittest.main()
