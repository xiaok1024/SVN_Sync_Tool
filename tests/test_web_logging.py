import errno
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import web_logging
from web_standard_service import StandardJob, StandardJobManager


class WebLoggingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_handlers = list(web_logging.LOGGER.handlers)
        self.old_level = web_logging.LOGGER.level
        self.old_commit = web_logging._commit
        self.addCleanup(self.restore_logging)

    def restore_logging(self):
        for handler in list(web_logging.LOGGER.handlers):
            web_logging.LOGGER.removeHandler(handler)
            handler.close()
        for handler in self.old_handlers:
            web_logging.LOGGER.addHandler(handler)
        web_logging.LOGGER.setLevel(self.old_level)
        web_logging._commit = self.old_commit

    def configure(self, **kwargs):
        return web_logging.configure_logging(self.root / "logs", commit="a" * 40, **kwargs)

    def records(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_json_whitelist_redaction_and_exception_locations(self):
        path = self.configure()
        try:
            raise PermissionError(errno.EACCES, "secret-password token-value", "/private/secret")
        except PermissionError as exc:
            web_logging.log_event("example_failed", secrets=("secret-password",),
                                  message="failed secret-password\nnext line", password="secret-password",
                                  cookie="token-value", body={"secret": "value"},
                                  **web_logging.exception_fields(exc))
        raw = path.read_text()
        record = self.records(path)[0]
        self.assertEqual(len(raw.splitlines()), 1)
        for secret in ("secret-password", "token-value", "/private/secret", '"password"', '"body"'):
            self.assertNotIn(secret, raw)
        self.assertEqual(record["errno"], errno.EACCES)
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertTrue(record["frames"])
        self.assertEqual(record["commit"], "a" * 40)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rotation_retention_and_restart_append(self):
        path = self.configure(max_bytes=500, backups=2)
        for i in range(20):
            web_logging.log_event("event", message=str(i) * 80)
        self.assertTrue(Path(str(path) + ".1").is_file())
        self.assertLessEqual(len(list(path.parent.glob("web.jsonl*"))), 3)
        stale = Path(str(path) + ".2")
        stale.touch()
        old = time.time() - 31 * 86400
        os.utime(stale, (old, old))
        previous = path.read_text()
        self.configure()
        self.assertFalse(stale.exists())
        web_logging.log_event("service_started")
        self.assertTrue(path.read_text().startswith(previous))
        self.assertEqual(self.records(path)[-1]["event"], "service_started")

    def test_log_write_failure_does_not_break_business_or_echo_record(self):
        self.configure()
        handler = web_logging.LOGGER.handlers[0]
        with mock.patch.object(handler.stream, "write", side_effect=OSError("secret")), \
                mock.patch("web_logging.sys.stderr.write") as error_output:
            web_logging.log_event("task_event", message="safe")
        self.assertEqual(error_output.call_count, 1)
        self.assertNotIn("secret", str(error_output.call_args))

    def test_cleanup_failure_retry_and_completion_are_persisted(self):
        path = self.configure()
        manager = StandardJobManager(temp_root=self.root / "jobs", require_password_stdin=False)
        self.addCleanup(manager.stop)
        job_id = "b" * 32
        directory = manager.temp_root / job_id
        directory.mkdir()
        manager._write_marker(job_id, directory, time.time())
        job = StandardJob(
            job_id=job_id, access_token_hash="not-for-logs", created_at=time.time(),
            expires_at=time.time()+100, svn_url="https://svn.example.com/repo",
            username="svn-user", password="private-password", profile_id="default",
            source_relative="customer/ecology", source_unc_prefix="", selection_mode="list",
            relative_paths=["src/A.java"], commit_message="private-commit-message",
            job_dir=directory, wc_dir=directory / "wc", config_dir=directory / "config",
            actor="colleague", request_id="c" * 32, state="committed", revision=123,
            finished_at=time.time())
        manager.jobs[job_id] = job
        with mock.patch("web_standard_service.shutil.rmtree",
                        side_effect=PermissionError(errno.EACCES, "private-password")):
            self.assertFalse(manager._delete_job_directory(job))
        manager.cleanup_expired()
        records = self.records(path)
        failed = next(x for x in records if x["event"] == "cleanup_failed")
        succeeded = next(x for x in records if x["event"] == "cleanup_succeeded")
        self.assertEqual(failed["errno"], errno.EACCES)
        self.assertEqual(succeeded["attempt"], 2)
        self.assertEqual(succeeded["revision"], 123)
        self.assertEqual(succeeded["actor"], "colleague")
        self.assertEqual(succeeded["cleanup_status"], "cleaned")
        self.assertEqual(job.events[-1]["message"], "服务器临时文件已清理")
        self.assertFalse(directory.exists())
        for secret in ("private-password", "not-for-logs", "private-commit-message"):
            self.assertNotIn(secret, path.read_text())
        manager._delete_job_directory(job)
        self.assertEqual(len(self.records(path)), len(records))

    def test_log_directory_cannot_be_in_deployment_tree(self):
        with self.assertRaisesRegex(ValueError, "部署目录之外"):
            web_logging.configure_logging(Path(web_logging.__file__).parent / "logs")


if __name__ == "__main__":
    unittest.main()
