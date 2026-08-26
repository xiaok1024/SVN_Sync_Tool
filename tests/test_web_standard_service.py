# -*- coding: utf-8 -*-

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from svn_standard_file_core import iter_standard_file_lines, parse_file_input
from web_standard_service import (
    SourceProfile,
    StandardJobManager,
    StandardWebError,
    WebSvnEngine,
    load_source_profiles,
    parse_customer_standard_path,
    parse_web_file_list,
)
from svn_sync_workflow import SparseCheckoutResult, prepare_sparse_working_copy


def wait_for_job(manager, job_id, access_token, terminal_states, timeout=20):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = manager.snapshot(job_id, access_token)["task"]
        if last["status"] in terminal_states:
            return last
        time.sleep(0.05)
    raise AssertionError("任务未在限定时间内完成: %r" % (last,))


class StandardWebParsingTest(unittest.TestCase):
    def test_source_profile_discovers_credentials_from_e9_paths_registry(self):
        with tempfile.TemporaryDirectory() as root:
            secrets_root = Path(root, "secrets")
            secrets_root.mkdir()
            credentials = secrets_root / "e9-smb-credentials.toml"
            credentials.write_text("[standard]\n", encoding="utf-8")
            config = Path(root, "e9-paths.json")
            config.write_text(json.dumps({"secrets.root": str(secrets_root)}), encoding="utf-8")
            profiles = load_source_profiles({"E9_PATHS_FILE": str(config)})
            self.assertEqual(len(profiles), 1)
            self.assertEqual(
                profiles[0].smb_credentials_file, os.path.realpath(credentials))
            self.assertTrue(profiles[0].available)

    def test_repository_relative_path_maps_checkout_root_and_keeps_spaces(self):
        root = "https://svn.example.com/svn/Y示例客户/ecology"
        relative, local = parse_file_input(
            "$/Y示例客户/ecology/sql/for Oracle/demo.sql(V12) - 说明", root)
        self.assertEqual(relative, "sql/for Oracle/demo.sql")
        self.assertIsNone(local)
        encoded, _local = parse_file_input(
            "https://svn.example.com/svn/Y%E7%A4%BA%E4%BE%8B%E5%AE%A2%E6%88%B7/ecology/"
            "sql/for%20Oracle/%E6%BC%94%E7%A4%BA.sql(V12)",
            root,
        )
        self.assertEqual(encoded, "sql/for Oracle/演示.sql")

    def test_color_marker_on_separate_line_skips_black_file(self):
        lines = list(iter_standard_file_lines([
            "QC123456 示例任务",
            "[red]",
            "src/Keep.java(V1)",
            "[black]",
            "src/Skip.java(V1)",
            "src/Untagged.java(V1)",
        ]))
        self.assertEqual(lines, ["src/Keep.java(V1)", "src/Untagged.java(V1)"])

    def test_web_parser_only_accepts_ecology_relative_files_and_blank_means_all(self):
        parsed = parse_web_file_list(
            "src/A.java\nWEB-INF/prop/demo file.properties\nsrc\\B.java")
        self.assertEqual(parsed, [
            "src/A.java", "WEB-INF/prop/demo file.properties", "src/B.java"])
        self.assertEqual(parse_web_file_list("\n  \n"), [])
        with self.assertRaisesRegex(StandardWebError, "绝对路径"):
            parse_web_file_list("/tmp/ecology/src/A.java")
        for unsupported in (
                "[red]\nsrc/A.java",
                "$/customer/ecology/src/A.java(V1)",
                "https://svn.example.com/svn/customer/ecology/src/A.java",
                "ecology/src/A.java",
                "../src/A.java"):
            with self.subTest(unsupported=unsupported):
                with self.assertRaises(StandardWebError):
                    parse_web_file_list(unsupported)

    def test_customer_standard_path_is_limited_to_fixed_share_and_qc_ecology(self):
        prefix = r"\\192.168.7.215\ECOLOGY_customer"
        value = prefix + r"\Y\Y示例客户\QC123456\ecology"
        self.assertEqual(
            parse_customer_standard_path(value, prefix),
            "Y/Y示例客户/QC123456/ecology",
        )
        for invalid in (
                r"\\192.168.7.216\ECOLOGY_customer\Y\客户\QC1\ecology",
                prefix + r"\Y\客户\..\ecology",
                prefix + r"\Y\客户\QC1\other",
                prefix + r"\Y\客户\QC1\ecology\src"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(StandardWebError):
                    parse_customer_standard_path(invalid, prefix)


class WebSvnCredentialIsolationTest(unittest.TestCase):
    def test_password_uses_stdin_and_never_enters_argv_or_config_files(self):
        with tempfile.TemporaryDirectory() as root:
            engine = WebSvnEngine("demo-user", "secret-value", root)
            command = engine._build_svn_cmd("status", ".")
            self.assertNotIn("secret-value", command)
            self.assertIn("--password-from-stdin", command)
            self.assertIn("--no-auth-cache", command)
            self.assertEqual(command[command.index("--config-dir") + 1], root)
            self.assertEqual(engine._svn_password_input(), b"secret-value\n")
            for filename in ("config", "servers"):
                self.assertNotIn(
                    "secret-value", Path(root, filename).read_text(encoding="utf-8"))


class StandardJobSafetyTest(unittest.TestCase):
    def make_manager(self, root, source):
        return StandardJobManager(
            profiles=[SourceProfile("default", "测试来源", standard_path=source)],
            temp_root=root,
            min_free_bytes=0,
            max_root_bytes=1024 * 1024 * 1024,
            require_password_stdin=False,
        )

    def test_empty_list_requires_explicit_intersection_confirmation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = self.make_manager(root, source)
            try:
                with self.assertRaisesRegex(StandardWebError, "必须确认"):
                    manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="",
                        cover_all_confirmed=False,
                        commit_message="QC123456 标准文件",
                    )
                self.assertFalse(manager.jobs)
            finally:
                manager.stop()

    def test_customer_source_suffix_maps_under_configured_share_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            target = Path(source, "Y", "Y示例客户", "QC123456", "ecology")
            target.mkdir(parents=True)
            profile = SourceProfile("default", "测试来源", standard_path=source)
            manager = StandardJobManager(
                profiles=[profile], temp_root=root, min_free_bytes=0,
                require_password_stdin=False)
            try:
                job = mock.Mock(source_relative="Y/Y示例客户/QC123456/ecology")
                self.assertEqual(manager._customer_source_path(job, profile), target.resolve())
            finally:
                manager.stop()

    def test_smb_profile_mount_is_read_only_noninteractive_and_clears_credentials(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as mounted:
            profile = SourceProfile(
                "default", "测试共享", standard_path="",
                smb_credentials_file="/private/not-read-in-test.toml")
            manager = StandardJobManager(
                profiles=[profile], temp_root=root, min_free_bytes=0,
                require_password_stdin=False)
            manager._source_mount_engine._find_existing_smb_mount = mock.Mock(
                return_value=None)
            manager._source_mount_engine._mount_smb_macos = mock.Mock(
                return_value=mounted)
            try:
                with mock.patch.object(
                        manager, "_read_smb_credentials",
                        return_value=("fixed-user", "fixed-password")):
                    self.assertEqual(
                        manager._profile_source_root(profile), Path(mounted).resolve())
                manager._source_mount_engine._mount_smb_macos.assert_called_once_with(
                    profile.unc_prefix, readonly=True, no_prompt=True)
                self.assertEqual(manager._source_mount_engine.smb_user, "")
                self.assertEqual(manager._source_mount_engine.smb_pass, "")
            finally:
                manager.stop()

    def test_job_marker_and_public_snapshot_do_not_persist_credentials_or_paths(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            manager = self.make_manager(root, source)
            try:
                manager._executor.submit = mock.Mock()
                result = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="private-user",
                    password="private-password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                task_id = result["task"]["id"]
                token = result["task"]["access_token"]
                marker = Path(root, task_id, ".lzr-standard-job.json").read_text(encoding="utf-8")
                self.assertNotIn("private-user", marker)
                self.assertNotIn("private-password", marker)
                self.assertNotIn(source, marker)
                snapshot = json.dumps(manager.snapshot(task_id, token), ensure_ascii=False)
                self.assertNotIn("private-user", snapshot)
                self.assertNotIn("private-password", snapshot)
                self.assertNotIn(source, snapshot)
                self.assertIn("[LZR-WEB:%s]" % task_id, snapshot)
                profiles = json.dumps(manager.public_profiles(), ensure_ascii=False)
                self.assertNotIn(source, profiles)
            finally:
                job = next(iter(manager.jobs.values()), None)
                if job:
                    manager._delete_job_directory(job)
                manager.stop()

    def test_large_preview_keeps_full_job_state_but_caps_browser_rows(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock()
            try:
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user", password="password", profile_id="default",
                    file_list="src/A.java", commit_message="QC123456 标准文件")
                job = manager.jobs[created["task"]["id"]]
                with job.lock:
                    job.state = "preview_ready"
                    job.preview_items = [
                        {"path": "src/%06d.java" % index, "source": "客户标准文件",
                         "result": "已覆盖", "detail": ""}
                        for index in range(1200)
                    ]
                preview = manager.snapshot(
                    job.job_id, created["task"]["access_token"])["task"]["preview"]
                self.assertEqual(preview["items_total"], 1200)
                self.assertTrue(preview["items_truncated"])
                self.assertEqual(len(preview["items"]), 1000)
                self.assertEqual(len(job.preview_items), 1200)
            finally:
                manager.stop()

    def test_confirmation_is_single_use_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = self.make_manager(root, source)
            try:
                manager._executor.submit = mock.Mock()
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job_id = created["task"]["id"]
                access_token = created["task"]["access_token"]
                job = manager.jobs[job_id]
                confirmation = "confirm-token"
                with job.lock:
                    job.state = "preview_ready"
                    job.can_commit = True
                    job.confirmation_token = confirmation
                    from web_standard_service import _token_hash
                    job.confirmation_token_hash = _token_hash(confirmation)
                manager._executor.submit.reset_mock()
                key = "idempotency_key_123456"
                manager.request_commit(job_id, access_token, confirmation, key)
                manager.request_commit(job_id, access_token, confirmation, key)
                self.assertEqual(manager._executor.submit.call_count, 1)
                with self.assertRaisesRegex(StandardWebError, "已经发起"):
                    manager.request_commit(
                        job_id, access_token, confirmation, "different_key_123456")
            finally:
                job = next(iter(manager.jobs.values()), None)
                if job:
                    manager._delete_job_directory(job)
                manager.stop()

    def test_orphan_cleanup_requires_valid_marker_and_skips_symlink(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            manager = self.make_manager(root, source)
            try:
                valid_id = "a" * 32
                valid = Path(root, valid_id)
                valid.mkdir()
                manager._write_marker(valid_id, valid, time.time())
                invalid = Path(root, "b" * 32)
                invalid.mkdir()
                outside = Path(root).parent / ("outside-standard-job-" + os.path.basename(root))
                outside.mkdir(exist_ok=True)
                symlink = Path(root, "c" * 32)
                try:
                    symlink.symlink_to(outside, target_is_directory=True)
                    manager._cleanup_orphan_directories()
                    self.assertFalse(valid.exists())
                    self.assertTrue(invalid.exists())
                    self.assertTrue(symlink.is_symlink())
                    self.assertTrue(outside.exists())
                finally:
                    if symlink.is_symlink():
                        symlink.unlink()
                    shutil.rmtree(outside, ignore_errors=True)
            finally:
                manager.stop()

    def test_single_instance_lock_prevents_cross_process_orphan_cleanup(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            first = self.make_manager(root, source)
            second = self.make_manager(root, source)
            try:
                first.start()
                with self.assertRaisesRegex(RuntimeError, "另一个 Web 进程"):
                    second.start()
            finally:
                first.stop()
                second.stop()

    def test_stop_keeps_root_lock_until_running_worker_finishes(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            first = self.make_manager(root, source)
            second = self.make_manager(root, source)
            worker_started = threading.Event()
            release_worker = threading.Event()

            def blocked_prepare(_job_id):
                worker_started.set()
                release_worker.wait(5)

            first._prepare_job = blocked_prepare
            stop_thread = None
            try:
                first.start()
                first.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                self.assertTrue(worker_started.wait(5))
                stop_thread = threading.Thread(target=first.stop)
                stop_thread.start()
                time.sleep(0.05)
                self.assertTrue(stop_thread.is_alive())
                with self.assertRaisesRegex(RuntimeError, "另一个 Web 进程"):
                    second.start()
                release_worker.set()
                stop_thread.join(5)
                self.assertFalse(stop_thread.is_alive())
                second.start()
            finally:
                release_worker.set()
                if stop_thread and stop_thread.is_alive():
                    stop_thread.join(5)
                first.stop()
                second.stop()

    def test_stop_keeps_root_lock_until_cleanup_thread_finishes(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            first = self.make_manager(root, source)
            second = self.make_manager(root, source)
            join_started = threading.Event()
            release_cleanup = threading.Event()

            class BlockingCleanupThread:
                timeout = "unset"

                @staticmethod
                def is_alive():
                    return True

                def join(self, timeout=None):
                    self.timeout = timeout
                    join_started.set()
                    if timeout is None:
                        release_cleanup.wait(5)

            cleanup_thread = BlockingCleanupThread()
            stop_thread = None
            try:
                first.start()
                first._cleanup_thread = cleanup_thread
                stop_thread = threading.Thread(target=first.stop)
                stop_thread.start()
                self.assertTrue(join_started.wait(5))
                self.assertIsNone(cleanup_thread.timeout)
                self.assertTrue(stop_thread.is_alive())
                with self.assertRaisesRegex(RuntimeError, "另一个 Web 进程"):
                    second.start()
                release_cleanup.set()
                stop_thread.join(5)
                self.assertFalse(stop_thread.is_alive())
                second.start()
            finally:
                release_cleanup.set()
                if stop_thread and stop_thread.is_alive():
                    stop_thread.join(5)
                first.stop()
                second.stop()

    def test_orphan_cleanup_cannot_delete_job_during_atomic_creation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock()
            marker_written = threading.Event()
            resume_create = threading.Event()
            cleanup_done = threading.Event()
            original_write_marker = manager._write_marker

            def blocked_marker(*args):
                original_write_marker(*args)
                marker_written.set()
                resume_create.wait(5)

            def run_cleanup():
                try:
                    manager._cleanup_orphan_directories()
                finally:
                    cleanup_done.set()

            try:
                with mock.patch.object(manager, "_write_marker", side_effect=blocked_marker):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            manager.create_job,
                            svn_url="https://svn.example.com/svn/customer/ecology",
                            username="user",
                            password="password",
                            profile_id="default",
                            file_list="src/A.java",
                            commit_message="QC123456 标准文件",
                        )
                        self.assertTrue(marker_written.wait(5))
                        cleanup_thread = threading.Thread(target=run_cleanup)
                        cleanup_thread.start()
                        self.assertFalse(cleanup_done.wait(0.1))
                        resume_create.set()
                        created = future.result(timeout=5)
                        cleanup_thread.join(5)
                job_id = created["task"]["id"]
                self.assertIn(job_id, manager.jobs)
                self.assertTrue(manager.jobs[job_id].job_dir.is_dir())
            finally:
                resume_create.set()
                manager.stop()

    def test_live_job_limit_is_atomic_for_concurrent_creates(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                min_free_bytes=0,
                max_live_jobs=1,
                require_password_stdin=False,
            )
            manager._executor.submit = mock.Mock()

            def create():
                try:
                    return manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="src/A.java",
                        commit_message="QC123456 标准文件",
                    )["task"]["id"]
                except StandardWebError as exc:
                    return exc.code

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    values = list(pool.map(lambda _index: create(), range(2)))
                self.assertEqual(sum(value == "too_many_jobs" for value in values), 1)
                self.assertEqual(len(manager.jobs), 1)
            finally:
                for job in manager.jobs.values():
                    manager._delete_job_directory(job)
                manager.stop()

    def test_cleanup_retries_terminal_job_after_transient_delete_failure(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock()
            try:
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job = manager.jobs[created["task"]["id"]]
                job.state = "committed"
                job.finished_at = time.time()
                with mock.patch("web_standard_service.shutil.rmtree", side_effect=OSError("busy")):
                    self.assertFalse(manager._delete_job_directory(job))
                self.assertEqual(job.cleanup_status, "failed")
                manager.cleanup_expired()
                self.assertEqual(job.cleanup_status, "cleaned")
                self.assertFalse(job.job_dir.exists())
            finally:
                manager.stop()

    def test_preview_expires_without_browser_polling_near_ttl(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                preview_ttl=0.08,
                cleanup_interval=0.02,
                min_free_bytes=0,
                require_password_stdin=False,
            )
            manager._executor.submit = mock.Mock()
            try:
                manager.start()
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job = manager.jobs[created["task"]["id"]]
                with job.lock:
                    job.state = "preview_ready"
                    job.can_commit = True
                    job.expires_at = time.time() + 0.08
                deadline = time.time() + 1
                while time.time() < deadline:
                    with job.lock:
                        if job.state == "expired" and job.cleanup_status == "cleaned":
                            break
                    time.sleep(0.01)
                with job.lock:
                    self.assertEqual(job.state, "expired")
                    self.assertIsNone(job.password)
                    self.assertEqual(job.cleanup_status, "cleaned")
                self.assertFalse(job.job_dir.exists())
            finally:
                manager.stop()

    def test_commit_acceptance_renews_deadline_and_expiry_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock()
            try:
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job_id = created["task"]["id"]
                token = created["task"]["access_token"]
                job = manager.jobs[job_id]
                confirmation = "confirm-token"
                from web_standard_service import _token_hash
                with job.lock:
                    job.state = "preview_ready"
                    job.can_commit = True
                    job.expires_at = time.time() + 0.1
                    job.confirmation_token = confirmation
                    job.confirmation_token_hash = _token_hash(confirmation)
                manager.request_commit(
                    job_id, token, confirmation, "deadline_commit_123456")
                self.assertGreater(job.expires_at, time.time() + 60)
                job.expires_at = time.time() - 1
                manager._commit_job(job_id)
                self.assertEqual(job.state, "expired")
                self.assertEqual(job.cleanup_status, "cleaned")
                self.assertIsNone(job.error)
            finally:
                manager.stop()

    def test_capacity_checks_account_for_pristine_each_file_and_sources(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                min_free_bytes=0,
                max_root_bytes=1024 * 1024,
                max_job_bytes=100 * 1024,
                require_password_stdin=False,
            )
            manager._executor.submit = mock.Mock()
            try:
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job = manager.jobs[created["task"]["id"]]
                with self.assertRaisesRegex(RuntimeError, "仓库文件"):
                    manager._prepare_safety_check(job, 1, "remote", 20 * 1024)

                oversized = job.job_dir / "oversized.bin"
                oversized.write_bytes(b"x" * (101 * 1024))
                with self.assertRaisesRegex(RuntimeError, "单任务容量"):
                    manager._prepare_safety_check(job, 1, "after", None)
                oversized.unlink()

                source_file = Path(source, "large.bin")
                source_file.write_bytes(b"x" * (101 * 1024))
                item = mock.Mock(source_file=str(source_file), rel_path="large.bin")
                with self.assertRaisesRegex(RuntimeError, "标准来源文件"):
                    manager._check_source_capacity(job, [item])
            finally:
                manager.stop()

    def test_capacity_reservations_prevent_concurrent_root_overcommit(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock()
            try:
                jobs = []
                for name in ("A.java", "B.java"):
                    created = manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="src/" + name,
                        commit_message="QC123456 标准文件",
                    )
                    jobs.append(manager.jobs[created["task"]["id"]])
                manager.max_root_bytes = 100
                barrier = threading.Barrier(2)

                def reserve(job):
                    barrier.wait()
                    try:
                        manager._reserve_estimated_capacity(job, 60, "测试文件")
                        return "accepted"
                    except RuntimeError:
                        return "rejected"

                with mock.patch("web_standard_service._directory_size", return_value=0), \
                        mock.patch("web_standard_service.shutil.disk_usage",
                                   return_value=mock.Mock(free=100)):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(reserve, jobs))
                self.assertEqual(results.count("accepted"), 1)
                self.assertEqual(results.count("rejected"), 1)
            finally:
                for job in manager.jobs.values():
                    manager._release_capacity_reservation(job)
                manager.stop()

    def test_blocked_preview_immediately_clears_credentials_and_directory(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()

            class FakeEngine:
                def __init__(self, _username, _password, _config):
                    pass

                def release_credentials(self):
                    pass

            def fake_sparse(_engine, _url, destination, _paths, safety_check=None):
                Path(destination).mkdir(parents=True)
                return SparseCheckoutResult(1, [], ["src/Missing.java"])

            missing_item = mock.Mock(
                rel_path="src/Missing.java",
                source_label="",
                status="未找到来源",
                detail="来源目录中不存在",
            )
            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                min_free_bytes=0,
                require_password_stdin=False,
                engine_factory=FakeEngine,
            )
            try:
                with mock.patch("web_standard_service.prepare_sparse_working_copy", fake_sparse), \
                        mock.patch.object(manager, "_repository_identity",
                                          return_value=("uuid", "https://svn.example.com/svn")), \
                        mock.patch("web_standard_service.StandardFileService.scan",
                                   return_value=([missing_item], 1, [])):
                    created = manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="src/Missing.java",
                        commit_message="QC123456 标准文件",
                    )
                    job_id = created["task"]["id"]
                    task = wait_for_job(
                        manager, job_id, created["task"]["access_token"],
                        {"preview_ready", "failed"},
                    )
                job = manager.jobs[job_id]
                self.assertEqual(task["status"], "preview_ready", task.get("error"))
                self.assertFalse(task["can_commit"])
                self.assertEqual(task["cleanup"]["status"], "cleaned")
                self.assertIsNone(job.password)
                self.assertFalse(job.job_dir.exists())
            finally:
                manager.stop()

    def test_commit_launch_failure_is_known_failure_not_unknown(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology").mkdir()

            class LaunchFailEngine:
                def __init__(self, _username, _password, _config):
                    self.commit_process_started = False

                def reset_commit_tracking(self):
                    self.commit_process_started = False

                def _run_svn(self, _log, *_args):
                    raise OSError("cannot launch svn")

                def release_credentials(self):
                    pass

            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                min_free_bytes=0,
                require_password_stdin=False,
                engine_factory=LaunchFailEngine,
            )
            manager._executor.submit = mock.Mock()
            try:
                created = manager.create_job(
                    svn_url="https://svn.example.com/svn/customer/ecology",
                    username="user",
                    password="password",
                    profile_id="default",
                    file_list="src/A.java",
                    commit_message="QC123456 标准文件",
                )
                job = manager.jobs[created["task"]["id"]]
                target = job.wc_dir / "src" / "A.java"
                target.parent.mkdir(parents=True)
                target.write_text("new", encoding="utf-8")
                entries = [{
                    "path": "src/A.java", "item": "added", "props": "none",
                    "kind": "file", "properties": {},
                }]
                job.state = "commit_queued"
                job.preview_items = [{"path": "src/A.java", "result": "已覆盖"}]
                job.commit_targets = ["src/A.java"]
                job.preview_fingerprint = manager._fingerprint(job, entries)
                with mock.patch.object(manager, "_read_status_entries", return_value=entries):
                    manager._commit_job(job.job_id)
                self.assertEqual(job.state, "failed")
                self.assertEqual(job.error["code"], "commit_failed")
            finally:
                manager.stop()

    def test_cancel_during_sparse_checkout_cannot_write_preview_ready_back(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            started = threading.Event()
            resume = threading.Event()

            class FakeEngine:
                def __init__(self, _username, _password, _config):
                    pass

                def release_credentials(self):
                    pass

            def fake_sparse(_engine, _url, _destination, _paths, safety_check=None):
                started.set()
                resume.wait(5)
                safety_check(1, "after", 1)
                return SparseCheckoutResult(1, [], [])

            manager = StandardJobManager(
                profiles=[SourceProfile("default", "测试来源", standard_path=source)],
                temp_root=root,
                min_free_bytes=0,
                require_password_stdin=False,
                engine_factory=FakeEngine,
            )
            try:
                with mock.patch("web_standard_service.prepare_sparse_working_copy", fake_sparse):
                    created = manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="src/A.java",
                        commit_message="QC123456 标准文件",
                    )
                    self.assertTrue(started.wait(5))
                    job_id = created["task"]["id"]
                    token = created["task"]["access_token"]
                    manager.cancel(job_id, token)
                    resume.set()
                    task = wait_for_job(manager, job_id, token, {"cancelled"})
                    self.assertEqual(task["status"], "cancelled")
                    self.assertEqual(task["cleanup"]["status"], "cleaned")
                    self.assertFalse(manager.jobs[job_id].job_dir.exists())
                    self.assertIsNone(manager.jobs[job_id].password)
            finally:
                resume.set()
                manager.stop()

    def test_executor_submit_failure_cleans_new_job_immediately(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as source:
            Path(source, "ecology", "src").mkdir(parents=True)
            Path(source, "ecology", "src", "A.java").write_text("x", encoding="utf-8")
            manager = self.make_manager(root, source)
            manager._executor.submit = mock.Mock(side_effect=RuntimeError("shutdown"))
            try:
                with self.assertRaisesRegex(StandardWebError, "队列当前不可用"):
                    manager.create_job(
                        svn_url="https://svn.example.com/svn/customer/ecology",
                        username="user",
                        password="password",
                        profile_id="default",
                        file_list="src/A.java",
                        commit_message="QC123456 标准文件",
                    )
                self.assertFalse(manager.jobs)
                self.assertFalse(any(path.is_dir() for path in Path(root).iterdir()))
            finally:
                manager.stop()


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "需要本机 SVN CLI")
class StandardJobLocalRepositoryIntegrationTest(unittest.TestCase):
    def test_sparse_allows_custom_and_inherited_properties(self):
        """客户 SVN 目录总量有明确上限，容量交给运行时门禁负责；
        不再因为检出根、文件或祖先目录带自定义/未知属性而提前拒绝
        （svn:keywords、svn:special 仍然是硬性拒绝，见下面的用例）。"""
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root, "repo")
            seed = Path(root, "seed")
            destination = Path(root, "sparse")
            config = Path(root, "svn-config")
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            repo_url = repo.as_uri()
            subprocess.run(["svn", "mkdir", repo_url + "/trunk", "-m", "init"],
                           check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repo_url + "/trunk", str(seed)],
                           check=True, capture_output=True)
            subprocess.run(["svn", "propset", "demo:root", "root-value", str(seed)],
                           check=True, capture_output=True)
            binary_file = seed / "WEB-INF" / "lib" / "gson.jar"
            binary_file.parent.mkdir(parents=True)
            binary_file.write_bytes(b"PK\x03\x04binary-jar-payload")
            subprocess.run(["svn", "add", "--parents", str(binary_file)],
                           check=True, capture_output=True)
            subprocess.run(
                ["svn", "propset", "svn:mime-type", "application/octet-stream",
                 str(binary_file)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["svn", "propset", "demo:dir", "dir-value", str(binary_file.parent)],
                check=True, capture_output=True,
            )
            subprocess.run(["svn", "commit", str(seed), "-m", "custom property seed"],
                           check=True, capture_output=True)

            engine = WebSvnEngine("local-test-user", "local-test-password", config)
            safety_calls = []
            try:
                result = prepare_sparse_working_copy(
                    engine,
                    repo_url + "/trunk",
                    str(destination),
                    ["WEB-INF/lib/gson.jar"],
                    safety_check=lambda index, phase, size: safety_calls.append(
                        (index, phase, size)),
                )
                self.assertEqual(result.materialized_paths, ["WEB-INF/lib/gson.jar"])
                self.assertEqual(
                    (destination / "WEB-INF" / "lib" / "gson.jar").read_bytes(),
                    b"PK\x03\x04binary-jar-payload")
                self.assertIn((1, "remote", len(b"PK\x03\x04binary-jar-payload")), safety_calls)
            finally:
                engine.release_credentials()

    def test_sparse_rejects_keyword_expansion_before_materializing_file(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root, "repo")
            seed = Path(root, "seed")
            destination = Path(root, "sparse")
            config = Path(root, "svn-config")
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            repo_url = repo.as_uri()
            subprocess.run(["svn", "mkdir", repo_url + "/trunk", "-m", "init"],
                           check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repo_url + "/trunk", str(seed)],
                           check=True, capture_output=True)
            keyword_file = seed / "src" / "Keyword.txt"
            keyword_file.parent.mkdir(parents=True)
            keyword_file.write_text("$Id$\n" * 100, encoding="utf-8")
            subprocess.run(["svn", "add", "--parents", str(keyword_file)],
                           check=True, capture_output=True)
            subprocess.run(["svn", "propset", "svn:keywords", "Id", str(keyword_file)],
                           check=True, capture_output=True)
            subprocess.run(["svn", "commit", str(seed), "-m", "keyword seed"],
                           check=True, capture_output=True)

            engine = WebSvnEngine("local-test-user", "local-test-password", config)
            safety_calls = []
            try:
                with self.assertRaisesRegex(ValueError, "svn:keywords"):
                    prepare_sparse_working_copy(
                        engine,
                        repo_url + "/trunk",
                        str(destination),
                        ["src/Keyword.txt"],
                        safety_check=lambda index, phase, size: safety_calls.append(
                            (index, phase, size)),
                    )
                self.assertEqual(safety_calls, [
                    (0, "checkout_before", None),
                    (0, "checkout_after", None),
                    (1, "before", None),
                ])
                self.assertFalse((destination / "src" / "Keyword.txt").exists())
            finally:
                engine.release_credentials()

    def test_sparse_preview_exact_commit_and_immediate_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root, "repo")
            seed = Path(root, "seed")
            source = Path(root, "standard")
            jobs = Path(root, "jobs")
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            repo_url = repo.as_uri()
            subprocess.run(["svn", "mkdir", repo_url + "/trunk", "-m", "init"],
                           check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repo_url + "/trunk", str(seed)],
                           check=True, capture_output=True)
            existing = seed / "src" / "现有 File.txt"
            percent_existing = seed / "src" / "%2e%2e" / "Percent.bin"
            existing.parent.mkdir(parents=True)
            percent_existing.parent.mkdir(parents=True)
            existing.write_text("old", encoding="utf-8")
            percent_existing.write_bytes(b"old-percent" * 128)
            subprocess.run(["svn", "add", "--parents", str(existing), str(percent_existing)],
                           check=True, capture_output=True)
            subprocess.run(["svn", "commit", str(seed), "-m", "seed"],
                           check=True, capture_output=True)

            standard_existing = source / "ecology" / "src" / "现有 File.txt"
            standard_percent = source / "ecology" / "src" / "%2e%2e" / "Percent.bin"
            standard_new = source / "ecology" / "src" / "新目录" / "新增.java"
            standard_existing.parent.mkdir(parents=True)
            standard_percent.parent.mkdir(parents=True)
            standard_new.parent.mkdir(parents=True)
            standard_existing.write_text("new-standard", encoding="utf-8")
            standard_percent.write_bytes(b"new-percent")
            standard_new.write_text("class Demo {}", encoding="utf-8")
            executable_source = source / "ecology" / "scripts" / "upgrade.sh"
            include_executable = os.name != "nt"
            if include_executable:
                executable_source.parent.mkdir(parents=True)
                executable_source.write_text("#!/bin/sh\necho upgrade\n", encoding="utf-8")
                executable_source.chmod(0o755)

            manager = StandardJobManager(
                profiles=[SourceProfile("default", "本地测试来源", standard_path=str(source))],
                temp_root=jobs,
                min_free_bytes=0,
                max_root_bytes=1024 * 1024 * 1024,
                max_job_bytes=128 * 1024 * 1024,
                require_password_stdin=True,
                allow_file_urls=True,
            )
            remote_sizes = []
            original_safety_check = manager._prepare_safety_check

            def recording_safety_check(job, index, phase, remote_size=None):
                if phase == "remote":
                    remote_sizes.append((job.relative_paths[index - 1], remote_size))
                return original_safety_check(job, index, phase, remote_size)

            manager._prepare_safety_check = recording_safety_check
            try:
                created = manager.create_job(
                    svn_url=repo_url + "/trunk",
                    username="local-test-user",
                    password="local-test-password",
                    profile_id="default",
                    file_list="src/现有 File.txt\nsrc/%2e%2e/Percent.bin",
                    commit_message="QC123456 Web 标准文件集成测试",
                )
                job_id = created["task"]["id"]
                token = created["task"]["access_token"]
                preview = wait_for_job(manager, job_id, token, {"preview_ready", "failed"})
                self.assertEqual(preview["status"], "preview_ready", preview.get("error"))
                self.assertTrue(preview["can_commit"])
                self.assertEqual(
                    preview["preview"]["summary"]["changed"],
                    2,
                )
                self.assertIn(
                    ("src/%2e%2e/Percent.bin", len(b"old-percent" * 128)),
                    remote_sizes,
                )
                manager.request_commit(
                    job_id,
                    token,
                    preview["preview"]["confirmation_token"],
                    "integration_commit_123456",
                )
                result = wait_for_job(
                    manager, job_id, token,
                    {"committed", "failed", "commit_unknown"}, timeout=30)
                self.assertEqual(result["status"], "committed", result.get("error"))
                self.assertIsNotNone(result["result"]["revision"])
                self.assertEqual(result["cleanup"]["status"], "cleaned")
                self.assertFalse(Path(jobs, job_id).exists())
                log_result = subprocess.run(
                    ["svn", "log", "--xml", "-r", str(result["result"]["revision"]), repo_url],
                    check=True, capture_output=True, text=True,
                )
                self.assertIn("[LZR-WEB:%s]" % job_id, log_result.stdout)

                verify = Path(root, "verify")
                subprocess.run(["svn", "checkout", repo_url + "/trunk", str(verify)],
                               check=True, capture_output=True)
                self.assertEqual(
                    (verify / "src" / "现有 File.txt").read_text(encoding="utf-8"),
                    "new-standard",
                )
                self.assertFalse((verify / "src" / "新目录" / "新增.java").exists())
                self.assertEqual(
                    (verify / "src" / "%2e%2e" / "Percent.bin").read_bytes(),
                    b"new-percent",
                )
                self.assertFalse((verify / "src" / "忽略.txt").exists())
                if include_executable:
                    self.assertFalse((verify / "scripts" / "upgrade.sh").exists())

                standard_existing.write_text("intersection-latest", encoding="utf-8")
                all_created = manager.create_job(
                    svn_url=repo_url + "/trunk",
                    username="local-test-user",
                    password="local-test-password",
                    profile_id="default",
                    file_list="",
                    cover_all_confirmed=True,
                    commit_message="QC123456 Web 全部交集测试",
                )
                all_id = all_created["task"]["id"]
                all_token = all_created["task"]["access_token"]
                all_preview = wait_for_job(
                    manager, all_id, all_token, {"preview_ready", "failed"}, timeout=30)
                self.assertEqual(all_preview["status"], "preview_ready", all_preview.get("error"))
                self.assertEqual(
                    all_preview["checkout_revision"], result["result"]["revision"])
                self.assertEqual(all_preview["selection_mode"], "intersection")
                self.assertEqual(all_preview["preview"]["summary"]["requested"], 2)
                self.assertEqual(all_preview["preview"]["summary"]["changed"], 1)
                manager.request_commit(
                    all_id,
                    all_token,
                    all_preview["preview"]["confirmation_token"],
                    "intersection_commit_123456",
                )
                all_result = wait_for_job(
                    manager, all_id, all_token,
                    {"committed", "failed", "commit_unknown"}, timeout=30)
                self.assertEqual(all_result["status"], "committed", all_result.get("error"))
                verify_latest = Path(root, "verify-latest")
                subprocess.run(["svn", "checkout", repo_url + "/trunk", str(verify_latest)],
                               check=True, capture_output=True)
                self.assertEqual(
                    (verify_latest / "src" / "现有 File.txt").read_text(encoding="utf-8"),
                    "intersection-latest",
                )
                self.assertFalse(
                    (verify_latest / "src" / "新目录" / "新增.java").exists())
            finally:
                manager.stop()


if __name__ == "__main__":
    unittest.main()
