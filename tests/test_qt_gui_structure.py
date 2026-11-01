# -*- coding: utf-8 -*-

import ast
import importlib.util
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QtGuiStructureTest(unittest.TestCase):
    def test_windows_entry_defaults_to_software_rendering(self):
        source = (PROJECT_ROOT / "svn_sync_qt.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.setdefault("QT_OPENGL", "software")', source)
        self.assertIn('os.environ.setdefault("QT_QUICK_BACKEND", "software")', source)
        runtime_hook = (PROJECT_ROOT / "pyi_rth_qt_software.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.setdefault("QT_OPENGL", "software")', runtime_hook)
        spec = (PROJECT_ROOT / "SVN_Sync_Tool.spec").read_text(encoding="utf-8")
        self.assertIn("runtime_hooks=['pyi_rth_qt_software.py']", spec)

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要 PySide6-Essentials")
    def test_qt_gui_builds_all_six_pages_offscreen(self):
        script = textwrap.dedent("""
            from PySide6.QtWidgets import QApplication
            from qt_app import MainWindow

            app = QApplication([])
            window = MainWindow()
            assert window.stack.count() == 6
            assert window.navigation.count() == 6
            assert [window.navigation.item(i).text() for i in range(6)] == [
                "01   SVN 拉取", "02   交叉覆盖", "03   全自动流程",
                "04   升级清单", "05   版本路径", "06   标准文件",
            ]
            assert not window.pages[2].progress.isTextVisible()
            window.pages[0].destination.setText("C:/work/ecology")
            assert window.pages[1].target.text() == "C:/work/ecology"
            assert window.pages[5].target.text() == "C:/work/ecology"
            window.close()
        """)
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=environment,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_qt_pages_do_not_duplicate_core_file_copy_or_svn_runners(self):
        source = (PROJECT_ROOT / "qt_pages.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertFalse(names & {
            "_run_svn", "_run_svn_bytes", "_scan_cross_files", "_copy_cross_files",
            "_resolve_source_path", "_mount_smb_macos", "extract_relative_path",
        })
        self.assertNotIn("shutil.copy", source)

    def test_qt_mutating_flows_have_confirmation_dialogs(self):
        source = (PROJECT_ROOT / "qt_pages.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = {
            node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        }

        def method(class_name, method_name):
            return next(node for node in classes[class_name].body
                        if isinstance(node, ast.FunctionDef) and node.name == method_name)

        def messagebox_calls(node):
            return [call for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "QMessageBox"]

        self.assertTrue(messagebox_calls(method("OverwritePage", "start_cover")))
        self.assertGreaterEqual(
            len(messagebox_calls(method("AutoPipelinePage", "start_pipeline"))), 2)
        self.assertTrue(messagebox_calls(method("StandardFilesPage", "start_cover")))
        self.assertTrue(messagebox_calls(method("StandardFilesPage", "start_local_cover")))


if __name__ == "__main__":
    unittest.main()
