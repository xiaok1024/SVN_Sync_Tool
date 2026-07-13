# -*- coding: utf-8 -*-

import os
import shutil
import subprocess
import tempfile
import unittest

from svn_sync_core import SyncEngine, decode_svn_output
from svn_standard_file_core import StandardFileItem, StandardFileService, extract_relative_path


class FakeEngine:
    def __init__(self):
        self.commands = []

    def _resolve_source_path(self, path, _log=None):
        return path

    def _run_svn(self, _log, *args):
        targets = None
        if "--targets" in args:
            filename = args[args.index("--targets") + 1]
            with open(filename, "r", encoding="utf-8") as handle:
                targets = [line.strip() for line in handle if line.strip()]
        self.commands.append((args, targets))
        if args[0] == "commit":
            return 0, "Committed revision 123."
        return 0, ""

    @staticmethod
    def _run_svn_bytes(*args, **_kwargs):
        if args and args[0] == "info":
            return 0, '<?xml version="1.0"?><info></info>'
        return 0, '<?xml version="1.0"?><status><target path="."></target></status>'

    @staticmethod
    def _parse_revision(_output):
        return 123

    @staticmethod
    def _get_revision_urls(_target, revision):
        path = "T天逸金融服务/src/A.java"
        return ["https://svn.example.com/svn/customer/%s(V%d)" % (path, revision)], [path]


class StandardFileCoreTest(unittest.TestCase):
    def test_decode_svn_output_prefers_utf8_and_falls_back_to_gbk(self):
        text = "T天逸金融服务"
        self.assertEqual(decode_svn_output(text.encode("utf-8")), text)
        self.assertEqual(decode_svn_output(text.encode("gbk")), text)

    def test_extract_relative_path_rejects_escape(self):
        self.assertIsNone(extract_relative_path("../../outside.txt"))
        self.assertEqual(
            extract_relative_path("https://svn.example.com/svn/customer/ecology/src/A.java(V12)",
                                  "https://svn.example.com/svn/customer/ecology"),
            "src/A.java")

    def test_scan_prefers_standard_ecology_directory(self):
        engine = FakeEngine()
        service = StandardFileService(engine)
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            standard = os.path.join(root, "standard")
            historical = os.path.join(root, "historical")
            os.makedirs(os.path.join(standard, "ecology", "src"))
            os.makedirs(os.path.join(historical, "ecology", "src"))
            os.makedirs(target)
            for base, text in ((standard, "standard"), (historical, "historical")):
                with open(os.path.join(base, "ecology", "src", "A.java"), "w", encoding="utf-8") as handle:
                    handle.write(text)
            items, parsed, _details = service.scan(
                ["src/A.java"], "", target, "upgrade", standard, historical)
            self.assertEqual(parsed, 1)
            self.assertEqual(items[0].source_label, "标准文件")
            self.assertEqual(items[0].status, "待覆盖")

    def test_scan_marks_identical_existing_file_as_noop(self):
        engine = FakeEngine()
        service = StandardFileService(engine)
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            standard = os.path.join(root, "standard")
            os.makedirs(os.path.join(target, "src"))
            os.makedirs(os.path.join(standard, "ecology", "src"))
            for filename in (os.path.join(target, "src", "A.java"),
                             os.path.join(standard, "ecology", "src", "A.java")):
                with open(filename, "w", encoding="utf-8") as handle:
                    handle.write("same")
            items, _parsed, _details = service.scan(["src/A.java"], "", target, "upgrade", standard, "")
            self.assertEqual(items[0].status, "内容相同")

    def test_prepare_adds_only_covered_files_then_commits_target_root(self):
        engine = FakeEngine()
        service = StandardFileService(engine)
        with tempfile.TemporaryDirectory() as target:
            path = os.path.join(target, "src", "A.java")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            item = StandardFileItem("src/A.java", path, "标准文件", path, True, "已覆盖", "")
            ok, _output, revision, urls, _paths = service.commit(target, [item], "test")
            self.assertTrue(ok)
            self.assertEqual(revision, 123)
            self.assertEqual(len(engine.commands), 2)
            self.assertNotIn("--targets", engine.commands[0][0])
            self.assertEqual(os.path.realpath(engine.commands[0][0][-1]), os.path.realpath(path))
            self.assertEqual(engine.commands[1][0][0], "commit")
            self.assertEqual(os.path.realpath(engine.commands[1][0][1]), os.path.realpath(target))
            self.assertEqual(
                urls,
                ["https://svn.example.com/svn/customer/T天逸金融服务/src/A.java(V123)"])

    @unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "需要本机 SVN CLI")
    def test_whole_target_commit_keeps_unversioned_file_but_commits_tracked_changes(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            wc = os.path.join(root, "wc")
            repo_url = "file://" + repo
            subprocess.run(["svnadmin", "create", repo], check=True, capture_output=True)
            subprocess.run(["svn", "mkdir", repo_url + "/trunk", "-m", "init"], check=True,
                           capture_output=True)
            subprocess.run(["svn", "checkout", repo_url + "/trunk", wc], check=True, capture_output=True)
            unrelated = os.path.join(wc, "unrelated.txt")
            with open(unrelated, "w", encoding="utf-8") as handle:
                handle.write("initial")
            subprocess.run(["svn", "add", unrelated], check=True, capture_output=True)
            subprocess.run(["svn", "commit", unrelated, "-m", "seed"], check=True, capture_output=True)
            with open(unrelated, "w", encoding="utf-8") as handle:
                handle.write("modified but must stay local")
            unversioned = os.path.join(wc, "must-not-be-added.txt")
            with open(unversioned, "w", encoding="utf-8") as handle:
                handle.write("unversioned")

            source = os.path.join(root, "source.java")
            target = os.path.join(wc, "src", "com", "新目录", "impl", "A.java")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write("class A {}")
            item = StandardFileItem("src/com/新目录/impl/A.java", source, "标准文件",
                                    target, False, "待覆盖", "")
            engine = SyncEngine()
            service = StandardFileService(engine)
            covered, errors = service.cover([item])
            self.assertFalse(errors)
            ok, output, revision, _urls, _paths = service.commit(wc, covered, "targeted")
            self.assertTrue(ok, output)
            self.assertIsNotNone(revision)
            status = subprocess.run(["svn", "status", wc], check=True, capture_output=True,
                                    text=True).stdout
            self.assertNotIn("M       " + unrelated, status)
            self.assertNotIn("A       ", status)
            self.assertIn("?       " + unversioned, status)


if __name__ == "__main__":
    unittest.main()
