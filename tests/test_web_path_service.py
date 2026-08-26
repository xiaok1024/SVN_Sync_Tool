# -*- coding: utf-8 -*-

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from svn_path_generator import build_revision_url_rows, query_revision_paths
from web_svn_common import HostAuthSvnEngine, WebSvnError
from web_path_service import (
    MAX_REVISIONS,
    PathQueryService,
    PathWebError,
    normalize_revision_spec,
    sort_revision_path_text,
)


SVN_AVAILABLE = bool(shutil.which("svn") and shutil.which("svnadmin"))


def _svn(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


class RevisionSpecValidationTest(unittest.TestCase):
    def test_accepts_single_multiple_and_range_forms(self):
        self.assertEqual(normalize_revision_spec("123"), [123])
        self.assertEqual(normalize_revision_spec("123,456 789"), [123, 456, 789])
        self.assertEqual(normalize_revision_spec("189-192"), [189, 190, 191, 192])
        self.assertEqual(
            normalize_revision_spec("192,189-191,192"), [189, 190, 191, 192])

    def test_accepts_fullwidth_separators_like_the_desktop_tool(self):
        self.assertEqual(normalize_revision_spec("123，456"), [123, 456])
        self.assertEqual(normalize_revision_spec("189–190"), [189, 190])

    def test_rejects_empty_and_unparseable_input(self):
        for value in ("", "   ", "\n"):
            with self.assertRaises(PathWebError) as caught:
                normalize_revision_spec(value)
            self.assertEqual(caught.exception.code, "empty_revision_spec")
        for value in ("abc", "r123", "123;456", "1 OR 1=1", "--help"):
            with self.assertRaises(PathWebError) as caught:
                normalize_revision_spec(value)
            self.assertEqual(caught.exception.code, "invalid_revision_spec")

    def test_rejects_zero_and_out_of_range_revisions(self):
        with self.assertRaises(PathWebError) as zero:
            normalize_revision_spec("0")
        self.assertEqual(zero.exception.code, "invalid_revision_spec")
        with self.assertRaises(PathWebError) as huge:
            normalize_revision_spec("9999999999")
        self.assertEqual(huge.exception.code, "invalid_revision_spec")

    def test_caps_revision_count_per_query(self):
        self.assertEqual(len(normalize_revision_spec("1-%d" % MAX_REVISIONS)), MAX_REVISIONS)
        with self.assertRaises(PathWebError) as caught:
            normalize_revision_spec("1-%d" % (MAX_REVISIONS + 1))
        self.assertEqual(caught.exception.code, "too_many_revisions")

    def test_rejects_oversized_spec(self):
        with self.assertRaises(PathWebError) as caught:
            normalize_revision_spec("1," * 4096)
        self.assertEqual(caught.exception.code, "revision_spec_too_large")


class LocalSortTest(unittest.TestCase):
    SAMPLE = (
        "http://svn.example.com/svn/R/b/Zeta.java(V192)\n"
        "\n"
        "http://svn.example.com/svn/R/a/Alpha.java(V189)\n"
        "http://svn.example.com/svn/R/c/mid.jsp(V190)\n"
    )

    def test_sort_by_revision_path_and_filename(self):
        by_revision = sort_revision_path_text(self.SAMPLE, "rev")["text"].splitlines()
        self.assertEqual([line[-5:] for line in by_revision], ["V189)", "V190)", "V192)"])

        by_path = sort_revision_path_text(self.SAMPLE, "path")["text"].splitlines()
        self.assertTrue(by_path[0].endswith("a/Alpha.java(V189)"))
        self.assertTrue(by_path[-1].endswith("c/mid.jsp(V190)"))

        by_name = sort_revision_path_text(self.SAMPLE, "name")["text"].splitlines()
        self.assertEqual(
            [line.rsplit("/", 1)[-1] for line in by_name],
            ["Alpha.java(V189)", "mid.jsp(V190)", "Zeta.java(V192)"],
        )

    def test_drops_blank_lines_and_reports_count(self):
        result = sort_revision_path_text(self.SAMPLE, "rev")
        self.assertEqual(result["stats"]["file_count"], 3)
        self.assertEqual(result["errors"], [])
        self.assertNotIn("\n\n", result["text"])

    def test_rejects_empty_oversized_and_invalid_input(self):
        with self.assertRaises(PathWebError) as empty:
            sort_revision_path_text("   \n ", "rev")
        self.assertEqual(empty.exception.code, "empty_path_text")

        with self.assertRaises(PathWebError) as sort_key:
            sort_revision_path_text("a(V1)", "按版本排序")
        self.assertEqual(sort_key.exception.code, "invalid_sort")

        with self.assertRaises(PathWebError) as oversized:
            sort_revision_path_text("x" * (1024 * 1024 + 1), "rev")
        self.assertEqual(oversized.exception.code, "path_text_too_large")

        with self.assertRaises(PathWebError) as nul:
            sort_revision_path_text("a(V1)\x00", "rev")
        self.assertEqual(nul.exception.code, "invalid_path_text")

    def test_sort_never_touches_svn(self):
        with tempfile.TemporaryDirectory() as root:
            service = PathQueryService(temp_root=Path(root, "queries"))
            # 纯本地排序不经过服务实例，也不应产生任何临时 SVN 配置目录
            sort_revision_path_text(self.SAMPLE, "rev")
            self.assertEqual(list(service.temp_root.iterdir()), [])


class QueryInputSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = PathQueryService(
            temp_root=Path(self.temp.name, "queries"),
            require_password_stdin=False,
        )

    def _query(self, **kwargs):
        payload = {
            "svn_url": "https://svn.example.com/svn/customer/ecology",
            "username": "",
            "password": "",
            "revision_spec": "123",
            "sort": "rev",
        }
        payload.update(kwargs)
        return self.service.query(**payload)

    def test_rejects_non_http_and_credential_bearing_urls(self):
        for url in ("ftp://svn.example.com/svn/R", "file:///tmp/repo",
                    "https://user:pw@svn.example.com/svn/R",
                    "https://svn.example.com/svn/R?a=1", "not-a-url"):
            with self.assertRaises(PathWebError, msg=url) as caught:
                self._query(svn_url=url)
            self.assertEqual(caught.exception.code, "invalid_svn_url")

    def test_enforces_server_side_prefix_allowlist(self):
        service = PathQueryService(
            temp_root=Path(self.temp.name, "allowlist"),
            allowed_svn_prefixes=("https://svn.example.com/svn/",),
            require_password_stdin=False,
        )
        with self.assertRaises(PathWebError) as caught:
            service.query(svn_url="https://other.example.com/svn/R", username="",
                          password="", revision_spec="1", sort="rev")
        self.assertEqual(caught.exception.code, "svn_url_not_allowed")
        self.assertEqual(caught.exception.status_code, 403)

    def test_rejects_credentials_with_newline_injection(self):
        with self.assertRaises(PathWebError) as user:
            self._query(username="demo\nadmin", password="secret")
        self.assertEqual(user.exception.code, "invalid_username")
        with self.assertRaises(PathWebError) as password:
            self._query(username="demo", password="secret\nmore")
        self.assertEqual(password.exception.code, "invalid_password")

    def test_partial_credentials_are_rejected(self):
        for username, password in (("demo", ""), ("  ", "secret"), ("", "secret")):
            with self.assertRaises(PathWebError) as caught:
                self._query(username=username, password=password)
            self.assertEqual(caught.exception.code, "incomplete_credentials")

    def test_blank_credentials_use_host_auth_cache(self):
        used = {}

        class RecordingHostEngine(HostAuthSvnEngine):
            def _run_svn_bytes(self, *args, **kwargs):
                used["args"] = args
                return 0, "<log/>"

        service = PathQueryService(
            temp_root=Path(self.temp.name, "hostcache"),
            host_engine_factory=RecordingHostEngine,
            require_password_stdin=False,
        )
        result = service.query(svn_url="https://svn.example.com/svn/R", username="",
                               password="", revision_spec="123", sort="rev")
        self.assertEqual(result["auth_mode"], "host-cache")
        self.assertEqual(used["args"][0], "log")
        # 主机缓存分支不接收凭据，因此不需要（也不应留下）临时配置目录
        self.assertEqual(list(service.temp_root.iterdir()), [])

    def test_supplied_credentials_stay_isolated_from_host_cache(self):
        seen = {}

        class RecordingEngine:
            def __init__(self, username, password, config_dir):
                seen["config_dir"] = Path(config_dir)
                seen["username"] = username

            def _run_svn_bytes(self, *_args, **_kwargs):
                return 0, "<log/>"

            def release_credentials(self):
                seen["released"] = True

        service = PathQueryService(
            temp_root=Path(self.temp.name, "isolated"),
            engine_factory=RecordingEngine,
            require_password_stdin=False,
        )
        result = service.query(svn_url="https://svn.example.com/svn/R", username="demo",
                               password="secret", revision_spec="1", sort="rev")
        self.assertEqual(result["auth_mode"], "supplied")
        self.assertEqual(seen["username"], "demo")
        self.assertIn("query-", str(seen["config_dir"]))
        self.assertTrue(seen["released"])
        self.assertEqual(list(service.temp_root.iterdir()), [])

    def test_host_auth_cache_can_be_disabled_server_side(self):
        service = PathQueryService(
            temp_root=Path(self.temp.name, "nohost"),
            allow_host_auth_cache=False,
            require_password_stdin=False,
        )
        with self.assertRaises(PathWebError) as caught:
            service.query(svn_url="https://svn.example.com/svn/R", username="",
                          password="", revision_spec="1", sort="rev")
        self.assertEqual(caught.exception.code, "host_auth_cache_disabled")
        self.assertEqual(caught.exception.status_code, 403)

    def test_environment_flag_controls_host_auth_cache(self):
        self.assertTrue(PathQueryService.from_environment({}).allow_host_auth_cache)
        for value in ("0", "false", "no", "off", "FALSE"):
            service = PathQueryService.from_environment(
                {"SVN_SYNC_WEB_ALLOW_HOST_SVN_CACHE": value})
            self.assertFalse(service.allow_host_auth_cache, value)

    def test_rejects_query_when_svn_cannot_take_password_from_stdin(self):
        service = PathQueryService(
            temp_root=Path(self.temp.name, "stdin"),
            require_password_stdin=True,
        )
        with mock.patch(
                "web_path_service.supports_password_from_stdin", return_value=False):
            with self.assertRaises(PathWebError) as caught:
                service.query(svn_url="https://svn.example.com/svn/R", username="demo",
                              password="secret", revision_spec="1", sort="rev")
        self.assertEqual(caught.exception.code, "svn_password_stdin_unsupported")
        self.assertEqual(caught.exception.status_code, 503)

    def test_rejects_extra_concurrent_queries(self):
        release = threading.Event()
        entered = threading.Event()

        class BlockingEngine:
            def __init__(self, _username, _password, _config_dir):
                pass

            def _run_svn_bytes(self, *_args, **_kwargs):
                entered.set()
                release.wait(10)
                return 0, "<log/>"

            def release_credentials(self):
                pass

        service = PathQueryService(
            temp_root=Path(self.temp.name, "slots"),
            max_workers=1,
            engine_factory=BlockingEngine,
            require_password_stdin=False,
        )
        worker = threading.Thread(target=lambda: service.query(
            svn_url="https://svn.example.com/svn/R", username="demo", password="secret",
            revision_spec="1", sort="rev"))
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            with self.assertRaises(PathWebError) as caught:
                service.query(svn_url="https://svn.example.com/svn/R", username="demo",
                              password="secret", revision_spec="2", sort="rev")
            self.assertEqual(caught.exception.code, "too_many_queries")
            self.assertEqual(caught.exception.status_code, 429)
        finally:
            release.set()
            worker.join(10)

    def test_query_errors_never_echo_the_password(self):
        password = "do-not-echo-this-password"

        class FailingEngine:
            def __init__(self, _username, _password, _config_dir):
                pass

            def _run_svn_bytes(self, *_args, **_kwargs):
                return 1, "svn: E170001: 认证失败 %s" % password

            def release_credentials(self):
                pass

        service = PathQueryService(
            temp_root=Path(self.temp.name, "redact"),
            engine_factory=FailingEngine,
            require_password_stdin=False,
        )
        result = service.query(svn_url="https://svn.example.com/svn/R", username="demo",
                               password=password, revision_spec="123", sort="rev")
        self.assertEqual(result["stats"]["file_count"], 0)
        self.assertEqual(result["stats"]["error_count"], 1)
        self.assertNotIn(password, repr(result))
        self.assertIn("版本 123", result["errors"][0])

    def test_startup_sweeps_stale_query_directories_only(self):
        root = Path(self.temp.name, "orphans")
        root.mkdir(parents=True)
        stale = root / "query-stale"
        fresh = root / "query-fresh"
        unrelated = root / "keep-me"
        for directory in (stale, fresh, unrelated):
            directory.mkdir()
        old_time = time.time() - 2 * 60 * 60
        os.utime(stale, (old_time, old_time))

        PathQueryService(temp_root=root, require_password_stdin=False)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_time_budget_stops_query_and_reports_skipped_revisions(self):
        class SlowEngine:
            def __init__(self, _username, _password, _config_dir):
                pass

            def _run_svn_bytes(self, *_args, **_kwargs):
                return 0, "<log/>"

            def release_credentials(self):
                pass

        service = PathQueryService(
            temp_root=Path(self.temp.name, "budget"),
            engine_factory=SlowEngine,
            require_password_stdin=False,
            time_budget=0,
        )
        result = service.query(svn_url="https://svn.example.com/svn/R", username="demo",
                               password="secret", revision_spec="1-3", sort="rev")
        self.assertEqual(result["stats"]["file_count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("3 个版本未查询", result["errors"][0])


@unittest.skipUnless(SVN_AVAILABLE, "需要本机安装 svn 与 svnadmin")
class QueryAgainstLocalRepositoryTest(unittest.TestCase):
    """用本地仓库验证 Web 结果与桌面版共享逻辑完全一致。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        repo = root / "repo"
        work = root / "work"
        _svn("svnadmin", "create", str(repo))
        # 与桌面版一致：基址是仓库根，svn log 返回的是仓库绝对路径。
        cls.repo_url = repo.as_uri()
        _svn("svn", "mkdir", cls.repo_url + "/trunk", "-m", "init")
        _svn("svn", "checkout", cls.repo_url + "/trunk", str(work))

        first = work / "src" / "weaver" / "Alpha.java"
        first.parent.mkdir(parents=True)
        first.write_text("class Alpha {}", encoding="utf-8")
        chinese = work / "odoc" / "中文 目录" / "iWebOffice.jsp"
        chinese.parent.mkdir(parents=True)
        chinese.write_text("<%-- demo --%>", encoding="utf-8")
        _svn("svn", "add", "--parents", str(first), str(chinese))
        _svn("svn", "commit", str(work), "-m", "r2 两个文件")

        second = work / "src" / "weaver" / "Beta.java"
        second.write_text("class Beta {}", encoding="utf-8")
        _svn("svn", "add", str(second))
        _svn("svn", "commit", str(work), "-m", "r3 新增一个文件")

        cls.service = PathQueryService(
            temp_root=root / "queries",
            allow_file_urls=True,
            require_password_stdin=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_single_revision_builds_full_urls_with_version_suffix(self):
        result = self.service.query(svn_url=self.repo_url, username="", password="",
                                    revision_spec="3", sort="rev")
        self.assertEqual(result["stats"]["file_count"], 1)
        self.assertEqual(result["stats"]["matched_revisions"], [3])
        self.assertEqual(
            result["text"],
            "%s/trunk/src/weaver/Beta.java(V3)" % self.repo_url)

    def test_range_query_decodes_chinese_paths_and_sorts_by_revision(self):
        result = self.service.query(svn_url=self.repo_url, username="", password="",
                                    revision_spec="2-3", sort="rev")
        lines = result["text"].splitlines()
        self.assertEqual(result["stats"]["revision_count"], 2)
        self.assertEqual(result["stats"]["matched_revisions"], [2, 3])
        self.assertEqual([line[-4:] for line in lines], ["(V2)", "(V2)", "(V3)"])
        self.assertTrue(any("/trunk/odoc/中文 目录/iWebOffice.jsp(V2)" in line for line in lines))
        self.assertNotIn("%E4%B8%AD", result["text"])

    def test_sort_modes_match_the_shared_pure_logic(self):
        for sort_key in ("rev", "path", "name"):
            web = self.service.query(svn_url=self.repo_url, username="", password="",
                                     revision_spec="2,3", sort=sort_key)
            results, errors = query_revision_paths(self.repo_url, "2,3")
            self.assertEqual(errors, [])
            expected = [row[0] for row in
                        build_revision_url_rows(results, self.repo_url, sort_key)]
            self.assertEqual([row["url"] for row in web["rows"]], expected, sort_key)

    def test_missing_revision_is_reported_without_failing_the_query(self):
        result = self.service.query(svn_url=self.repo_url, username="", password="",
                                    revision_spec="3,900", sort="rev")
        self.assertEqual(result["stats"]["file_count"], 1)
        self.assertEqual(result["stats"]["error_count"], 1)
        self.assertIn("版本 900", result["errors"][0])

    def test_query_leaves_no_temporary_svn_configuration_behind(self):
        self.service.query(svn_url=self.repo_url, username="", password="",
                           revision_spec="2", sort="rev")
        self.assertEqual(list(self.service.temp_root.iterdir()), [])


class HostAuthEngineTest(unittest.TestCase):
    """主机缓存认证通道必须永远无法写入仓库。"""

    def test_write_subcommands_are_refused(self):
        engine = HostAuthSvnEngine()
        for subcommand in ("commit", "add", "delete", "propset", "import", "checkout",
                          "update", "mkdir", "copy", "move", "lock", "unlock"):
            with self.assertRaises(WebSvnError, msg=subcommand) as caught:
                engine._run_svn_bytes(subcommand, ".")
            self.assertEqual(caught.exception.code, "read_only_engine_violation")
            with self.assertRaises(WebSvnError, msg=subcommand):
                engine._run_svn(None, subcommand, ".")

    def test_empty_command_is_refused(self):
        with self.assertRaises(WebSvnError):
            HostAuthSvnEngine()._run_svn_bytes()

    def test_never_carries_browser_credentials(self):
        engine = HostAuthSvnEngine()
        self.assertEqual(engine.svn_user, "")
        self.assertEqual(engine.svn_pass, "")
        self.assertFalse(str(engine.svn_config_dir or ""))
        self.assertFalse(engine.svn_no_auth_cache)


if __name__ == "__main__":
    unittest.main()
