# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

from svn_sync_workflow import run_auto_pipeline, validate_checkout_replacement


class WorkflowEngine:
    def __init__(self, source, copy_errors=None, status=""):
        self.source = source
        self.copy_errors = copy_errors or []
        self.status = status
        self.commands = []

    @staticmethod
    def _log(_target, _message):
        pass

    def _run_svn(self, _target, *args):
        self.commands.append(args)
        if args[0] == "status":
            return 0, self.status
        if args[0] == "commit":
            return 0, "Committed revision 88."
        return 0, ""

    def _resolve_source_path(self, _source, _log=None):
        return self.source

    @staticmethod
    def _scan_cross_files(_target, _source):
        return [("src/A.java", "source", "target")]

    def _copy_cross_files(self, entries, on_result=None):
        if self.copy_errors:
            if on_result:
                on_result(entries[0][0], False, self.copy_errors[0][1])
            return [], self.copy_errors
        if on_result:
            on_result(entries[0][0], True, "")
        return entries, []

    @staticmethod
    def _unlock_svn_locks_before_commit(_log, _target):
        return True

    @staticmethod
    def _get_wc_last_revision(_target):
        return 77

    @staticmethod
    def _parse_revision(_output):
        return 88

    @staticmethod
    def _get_revision_urls(_target, revision):
        return (["https://svn.example.com/svn/customer/src/A.java(V%d)" % revision],
                ["src/A.java"])


class SyncWorkflowTest(unittest.TestCase):
    def test_replacement_rejects_home_and_non_working_copy(self):
        with self.assertRaises(ValueError):
            validate_checkout_replacement(os.path.expanduser("~"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                validate_checkout_replacement(directory)

    def test_auto_pipeline_stops_before_commit_after_copy_failure(self):
        with tempfile.TemporaryDirectory() as root:
            destination = os.path.join(root, "wc")
            source = os.path.join(root, "source")
            os.makedirs(os.path.join(destination, ".svn"))
            os.makedirs(source)
            engine = WorkflowEngine(source, copy_errors=[("src/A.java", "copy failed")])
            result = run_auto_pipeline(
                engine, "https://svn.example.com/svn/customer", destination,
                source, "update", "test")
        self.assertFalse(result.ok)
        self.assertFalse(any(command[0] == "commit" for command in engine.commands))

    def test_auto_pipeline_exports_current_revision_when_no_changes(self):
        with tempfile.TemporaryDirectory() as root:
            destination = os.path.join(root, "wc")
            source = os.path.join(root, "source")
            os.makedirs(os.path.join(destination, ".svn"))
            os.makedirs(source)
            engine = WorkflowEngine(source, status="")
            result = run_auto_pipeline(
                engine, "https://svn.example.com/svn/customer", destination,
                source, "update", "test")
        self.assertTrue(result.ok)
        self.assertTrue(result.no_changes)
        self.assertEqual(result.revision, 77)
        self.assertEqual(len(result.urls), 1)


if __name__ == "__main__":
    unittest.main()
