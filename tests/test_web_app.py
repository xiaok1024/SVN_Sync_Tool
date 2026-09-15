# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest
from pathlib import Path

# 必须在导入 web_app 之前设置：账号库落到临时目录（绝不能碰真实账号库），
# 可信 Host 加入 TestClient 的默认主机名。
_TEST_HOME = tempfile.mkdtemp(prefix="lzr-web-test-")
os.environ["SVN_SYNC_WEB_USER_STORE"] = os.path.join(_TEST_HOME, "web_users.json")
# lzr-dev-host.local 模拟 --lan 探测到的真实主机名，用来验证大小写与端口处理
os.environ["SVN_SYNC_WEB_ALLOWED_HOSTS"] = ",".join(
    value for value in (
        os.environ.get("SVN_SYNC_WEB_ALLOWED_HOSTS", ""), "testserver", "lzr-dev-host.local")
    if value)

try:
    from fastapi.testclient import TestClient
    import web_app
    from web_app import MAX_REQUEST_BYTES, app
    WEB_AVAILABLE = True
except ModuleNotFoundError:
    TestClient = None
    web_app = None
    MAX_REQUEST_BYTES = 2 * 1024 * 1024
    app = None
    WEB_AVAILABLE = False


SAMPLE_HTML = (
    '<style>.red { color: #ff0000; }</style>'
    '<div>QC321 Web 接口验证 —— 门户</div>'
    '<div class="red">https://svn.example.com/svn/customer/ecology/src/Test.java(V20)</div>'
)


@unittest.skipUnless(WEB_AVAILABLE, "需要 requirements-web.txt 中的 Web 依赖")
class WebAppApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 全站需登录：业务用例统一使用已登录且已保存 SVN 凭据的客户端。
        cls.client = TestClient(app)
        cls.client.post("/api/v1/auth/register",
                        json={"username": "apitester", "password": "correct-horse"})
        cls.client.post("/api/v1/auth/login",
                        json={"username": "apitester", "password": "correct-horse"})
        cls.client.put("/api/v1/auth/svn-credentials",
                       json={"svn_username": "svc", "svn_password": "svc-password"})

    @staticmethod
    def anonymous_client():
        return TestClient(app)

    def test_index_has_brand_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("升级工具中心", response.text)
        self.assertIn("LZR", response.text)
        self.assertIn("SVN 标准文件提交", response.text)
        self.assertIn("版本号路径生成", response.text)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_allowed_hosts_only_come_from_loopback_and_environment(self):
        """旧开发机名和测试桩不该写死在生产白名单里。"""
        hosts = web_app._allowed_hosts()
        self.assertIn("testserver", hosts, "由测试环境变量注入")
        self.assertNotIn("lzr-mac-mini.local", hosts)
        self.assertEqual(
            {h for h in hosts if h not in {"127.0.0.1", "localhost"}},
            {h.strip().lower() for h in os.environ["SVN_SYNC_WEB_ALLOWED_HOSTS"].split(",")})

    def test_upgrade_list_requests_reject_unknown_fields(self):
        for path, payload in (
                ("/api/v1/upgrade-list/extract", {"html": SAMPLE_HTML, "extra": 1}),
                ("/api/v1/upgrade-list/generate",
                 {"list_text": "x", "format": "md", "extra": 1})):
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 422, path)
            self.assertEqual(response.json()["error"]["code"], "invalid_field", path)

    def test_session_survives_auth_service_restart_and_is_renewed_when_old(self):
        """会话是签名令牌：换一个 AuthService 实例（模拟服务重启）仍可识别；
        令牌过半寿命后接口会重新下发 cookie。"""
        from unittest import mock
        import web_auth_service
        from web_auth_service import SESSION_COOKIE, SESSION_TTL_SECONDS, AuthService

        client = TestClient(app)
        client.post("/api/v1/auth/register",
                    json={"username": "restartuser", "password": "correct-horse"})
        client.post("/api/v1/auth/login",
                    json={"username": "restartuser", "password": "correct-horse"})
        token = client.cookies.get(SESSION_COOKIE)
        self.assertTrue(token)

        original = app.state.auth
        app.state.auth = AuthService(store_path=original.store_path)
        try:
            me = client.get("/api/v1/auth/me")
            self.assertTrue(me.json()["authenticated"])
            self.assertNotIn("set-cookie", {k.lower() for k in me.headers})

            later = web_auth_service._now() + SESSION_TTL_SECONDS * 2 // 3
            with mock.patch.object(web_auth_service, "_now", return_value=later):
                renewed = client.get("/api/v1/auth/me")
            self.assertTrue(renewed.json()["authenticated"])
            raw_cookie = renewed.headers.get("set-cookie", "")
            self.assertIn(SESSION_COOKIE + "=", raw_cookie)
            self.assertNotIn(token, raw_cookie, "应签发新令牌而不是原样重发")
        finally:
            app.state.auth = original

    def test_site_icons_are_declared_and_served(self):
        page = self.client.get("/").text
        self.assertIn('rel="icon"', page)
        self.assertIn("apple-touch-icon", page)
        for path, prefix in (("/favicon.ico", b"\x00\x00\x01\x00"),
                             ("/static/favicon.svg", b"<svg"),
                             ("/static/favicon-32.png", b"\x89PNG")):
            response = self.anonymous_client().get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response.content.startswith(prefix), path)

    def test_deployed_commit_is_shown_in_health_and_footer(self):
        """线上版本标记由发布脚本写入；页面与健康接口都要能看到是哪个提交。"""
        from unittest import mock
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, ".deployed.json")
            marker.write_text(json.dumps({
                "commit": "abc1234def", "short": "abc1234", "subject": "示例提交",
                "deployed_at": "2026-09-15 10:00:00", "by": "tester"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SVN_SYNC_WEB_DEPLOY_INFO": str(marker)}):
                health = self.client.get("/api/health").json()
                self.assertEqual(health["deployed"]["short"], "abc1234")
                self.assertEqual(health["deployed"]["by"], "tester")
                self.assertIn("abc1234", self.client.get("/").text)
            with mock.patch.dict(os.environ, {"SVN_SYNC_WEB_DEPLOY_INFO": str(Path(root, "missing"))}):
                self.assertEqual(self.client.get("/api/health").json()["deployed"], {})

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_standard_source_profiles_do_not_expose_server_paths(self):
        response = self.client.get("/api/v1/standard-files/source-profiles")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertNotIn("standard_path", response.text)
        self.assertNotIn("historical_path", response.text)

    def test_standard_task_rejects_unconfigured_source_without_echoing_password(self):
        password = "svc-password"
        response = self.client.post(
            "/api/v1/standard-files/tasks",
            json={
                "svn_url": "https://svn.example.com/svn/customer/ecology",
                "source_profile_id": "missing",
                "customer_standard_path": r"\\192.168.7.215\ECOLOGY_customer\Y\示例客户\QC123456\ecology",
                "file_list": "src/A.java",
                "cover_all_confirmed": False,
                "commit_message": "QC123456 标准文件",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "source_profile_not_found")
        self.assertNotIn(password, response.text)

    def test_standard_task_rejects_arbitrary_server_path_fields(self):
        response = self.client.post(
            "/api/v1/standard-files/tasks",
            json={
                "svn_url": "https://svn.example.com/svn/customer/ecology",
                "source_profile_id": "missing",
                "customer_standard_path": r"\\192.168.7.215\ECOLOGY_customer\Y\示例客户\QC123456\ecology",
                "file_list": "src/A.java",
                "cover_all_confirmed": False,
                "commit_message": "QC123456 标准文件",
                "standard_path": "/tmp/should-not-be-accepted",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_field")

    def test_extract_and_generate_round_trip(self):
        extract = self.client.post(
            "/api/v1/upgrade-list/extract",
            json={"html": SAMPLE_HTML},
        )
        self.assertEqual(extract.status_code, 200)
        list_text = extract.json()["list_text"]
        edited = list_text.replace("Web 接口验证", "浏览器校对生效")

        generate = self.client.post(
            "/api/v1/upgrade-list/generate",
            json={"list_text": edited, "format": "md"},
        )
        self.assertEqual(generate.status_code, 200)
        self.assertIn("浏览器校对生效", generate.json()["content"])
        self.assertEqual(generate.json()["filename"], "customer-upgrade-file-list.md")

    def test_non_json_and_malformed_json_have_stable_errors(self):
        unsupported = self.client.post(
            "/api/v1/upgrade-list/extract",
            content="html",
            headers={"content-type": "text/plain"},
        )
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(unsupported.json()["error"]["code"], "unsupported_media_type")

        malformed = self.client.post(
            "/api/v1/upgrade-list/extract",
            content="{not-json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "malformed_json")

    def test_request_size_limit(self):
        body = json.dumps({"html": "x" * MAX_REQUEST_BYTES})
        response = self.client.post(
            "/api/v1/upgrade-list/extract",
            content=body,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")

    def test_error_does_not_echo_untrusted_html(self):
        payload = "</textarea><script>alert('x')</script>"
        response = self.client.post(
            "/api/v1/upgrade-list/extract",
            json={"html": payload},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("script", response.text)

    def test_host_header_matching_is_case_insensitive(self):
        """主机名大小写不敏感；真实主机名常带大写，不能因此被拒。"""
        for host in ("lzr-dev-host.local:8765", "LZR-DEV-HOST.local:8765",
                     "LZR-Dev-Host.LOCAL:8765", "LOCALHOST"):
            response = self.client.get("/api/health", headers={"host": host})
            self.assertEqual(response.status_code, 200, host)

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/", headers={"host": "untrusted.example"})
        self.assertEqual(response.status_code, 400)

    def test_environment_configured_hostname_is_allowed_with_port(self):
        """--lan 探测到的主机名经环境变量注入；带端口的 Host 也要放行。"""
        response = self.client.get(
            "/api/health", headers={"host": "lzr-dev-host.local:8765"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_cross_site_write_request_is_rejected(self):
        response = self.anonymous_client().post(
            "/api/v1/upgrade-list/extract",
            json={"html": "test"},
            headers={"origin": "http://untrusted.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "origin_mismatch")

    def test_revision_path_sort_endpoint_reuses_shared_sorting(self):
        response = self.client.post(
            "/api/v1/revision-paths/sort",
            json={
                "text": ("http://svn.example.com/svn/R/b/Zeta.java(V192)\n"
                         "http://svn.example.com/svn/R/a/Alpha.java(V189)"),
                "sort": "rev",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sort"], "rev")
        self.assertEqual(payload["stats"]["file_count"], 2)
        self.assertTrue(payload["text"].startswith("http://svn.example.com/svn/R/a/Alpha.java(V189)"))

    def test_revision_path_query_validates_input_without_echoing_password(self):
        response = self.client.post(
            "/api/v1/revision-paths/query",
            json={
                "svn_url": "https://svn.example.com/svn/customer",
                "revision_spec": "not-a-revision",
                "sort": "rev",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_revision_spec")
        self.assertNotIn("svc-password", response.text)

    def test_revision_path_query_rejects_arbitrary_extra_fields(self):
        response = self.client.post(
            "/api/v1/revision-paths/query",
            json={
                "svn_url": "https://svn.example.com/svn/customer",
                "revision_spec": "123",
                "sort": "rev",
                "config_dir": "/tmp/should-not-be-accepted",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_field")

    def test_revision_path_endpoints_reject_cross_site_writes(self):
        client = self.anonymous_client()
        for path in ("/api/v1/revision-paths/query", "/api/v1/revision-paths/sort"):
            response = client.post(
                path,
                json={"svn_url": "https://svn.example.com/svn/R",
                      "revision_spec": "1", "sort": "rev", "text": "a(V1)"},
                headers={"origin": "http://untrusted.example"},
            )
            self.assertEqual(response.status_code, 403, path)

    def test_static_assets_are_always_revalidated(self):
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers["cache-control"])

    def test_business_routes_reject_anonymous_callers(self):
        """全站登录：未带会话时业务接口一律 401。"""
        client = self.anonymous_client()
        cases = [
            ("post", "/api/v1/upgrade-list/extract", {"html": "x"}),
            ("post", "/api/v1/upgrade-list/generate", {"list_text": "x", "format": "md"}),
            ("post", "/api/v1/revision-paths/query",
             {"svn_url": "https://svn.example.com/svn/R", "revision_spec": "1"}),
            ("post", "/api/v1/revision-paths/sort", {"text": "a(V1)"}),
            ("get", "/api/v1/standard-files/source-profiles", None),
        ]
        for method, path, payload in cases:
            call = getattr(client, method)
            response = call(path, json=payload) if payload is not None else call(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.json()["error"]["code"], "login_required", path)

    def test_register_login_flow_and_session_cookie_attributes(self):
        client = TestClient(app)
        registered = client.post(
            "/api/v1/auth/register",
            json={"username": "flowuser", "password": "correct-horse", "display_name": "流程"},
        )
        self.assertEqual(registered.status_code, 200)
        self.assertNotIn("correct-horse", registered.text)

        # 注册本身不建会话
        self.assertFalse(client.get("/api/v1/auth/me").json()["authenticated"])

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "flowuser", "password": "correct-horse"})
        self.assertEqual(login.status_code, 200)
        raw_cookie = login.headers.get("set-cookie", "").lower()
        self.assertIn("httponly", raw_cookie)
        self.assertIn("samesite=strict", raw_cookie)

        me = client.get("/api/v1/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["username"], "flowuser")
        self.assertFalse(me["user"]["has_svn_credentials"])

        client.post("/api/v1/auth/logout")
        self.assertFalse(client.get("/api/v1/auth/me").json()["authenticated"])

    def test_svn_password_only_comes_back_from_the_dedicated_endpoint(self):
        """弹窗需要明文回显，但仅限本人主动请求的专用接口；
        /me 与保存接口每次加载页面都会调用，必须不含密码。"""
        client = TestClient(app)
        client.post("/api/v1/auth/register",
                    json={"username": "creduser", "password": "correct-horse"})
        client.post("/api/v1/auth/login",
                    json={"username": "creduser", "password": "correct-horse"})

        saved = client.put(
            "/api/v1/auth/svn-credentials",
            json={"svn_username": "svc", "svn_password": "svn-secret-value"})
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("svn-secret-value", saved.text)
        self.assertTrue(saved.json()["user"]["has_svn_credentials"])
        self.assertEqual(saved.json()["user"]["svn_username"], "svc")

        # /me 不得回传密码
        self.assertNotIn("svn-secret-value", client.get("/api/v1/auth/me").text)

        # 专用接口回显本人凭据
        read = client.get("/api/v1/auth/svn-credentials")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["svn_username"], "svc")
        self.assertEqual(read.json()["svn_password"], "svn-secret-value")
        self.assertTrue(read.json()["configured"])

        cleared = client.delete("/api/v1/auth/svn-credentials")
        self.assertFalse(cleared.json()["user"]["has_svn_credentials"])
        empty = client.get("/api/v1/auth/svn-credentials")
        self.assertFalse(empty.json()["configured"])
        self.assertEqual(empty.json()["svn_password"], "")

    def test_svn_password_endpoint_never_leaks_another_users_credentials(self):
        """只能拿到自己的：会话决定身份，请求体无法指定他人。"""
        owner = TestClient(app)
        owner.post("/api/v1/auth/register",
                   json={"username": "owner1", "password": "correct-horse"})
        owner.post("/api/v1/auth/login",
                   json={"username": "owner1", "password": "correct-horse"})
        owner.put("/api/v1/auth/svn-credentials",
                  json={"svn_username": "owner-svc", "svn_password": "owner-only-secret"})

        other = TestClient(app)
        other.post("/api/v1/auth/register",
                   json={"username": "other1", "password": "correct-horse"})
        other.post("/api/v1/auth/login",
                   json={"username": "other1", "password": "correct-horse"})
        response = other.get("/api/v1/auth/svn-credentials")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("owner-only-secret", response.text)
        self.assertFalse(response.json()["configured"])

        # 未登录一律 401
        anon = TestClient(app).get("/api/v1/auth/svn-credentials")
        self.assertEqual(anon.status_code, 401)

    def test_svn_action_requires_saved_credentials_first(self):
        client = TestClient(app)
        client.post("/api/v1/auth/register",
                    json={"username": "nocreds", "password": "correct-horse"})
        client.post("/api/v1/auth/login",
                    json={"username": "nocreds", "password": "correct-horse"})
        response = client.post(
            "/api/v1/revision-paths/query",
            json={"svn_url": "https://svn.example.com/svn/R", "revision_spec": "1"})
        self.assertEqual(response.status_code, 428)
        self.assertEqual(response.json()["error"]["code"], "svn_credentials_missing")

    def test_browser_cannot_smuggle_svn_credentials_into_a_task(self):
        client = TestClient(app)
        client.post("/api/v1/auth/register",
                    json={"username": "smuggler", "password": "correct-horse"})
        client.post("/api/v1/auth/login",
                    json={"username": "smuggler", "password": "correct-horse"})
        response = client.post(
            "/api/v1/revision-paths/query",
            json={"svn_url": "https://svn.example.com/svn/R", "revision_spec": "1",
                  "svn_username": "someone-else", "svn_password": "their-password"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_field")

    def test_every_successful_response_carries_ok_true(self):
        """前端以 ok 判定成败；漏掉它会把成功的 200 报成失败。"""
        client = TestClient(app)
        calls = [
            ("register", lambda: client.post(
                "/api/v1/auth/register",
                json={"username": "okshape", "password": "correct-horse"})),
            ("login", lambda: client.post(
                "/api/v1/auth/login",
                json={"username": "okshape", "password": "correct-horse"})),
            ("me", lambda: client.get("/api/v1/auth/me")),
            ("save-svn", lambda: client.put(
                "/api/v1/auth/svn-credentials",
                json={"svn_username": "svc", "svn_password": "pw"})),
            ("source-profiles", lambda: client.get(
                "/api/v1/standard-files/source-profiles")),
            ("sort", lambda: client.post(
                "/api/v1/revision-paths/sort", json={"text": "a(V1)"})),
            ("clear-svn", lambda: client.delete("/api/v1/auth/svn-credentials")),
            ("logout", lambda: client.post("/api/v1/auth/logout")),
        ]
        for label, call in calls:
            response = call()
            self.assertEqual(response.status_code, 200, label)
            self.assertIs(response.json().get("ok"), True, "%s 响应缺少 ok=true" % label)

    def test_static_route_cannot_read_repository_files(self):
        response = self.client.get("/static/%2e%2e/README.md")
        self.assertEqual(response.status_code, 404)


class WebSourceSafetyTest(unittest.TestCase):
    def test_frontend_never_assigns_untrusted_content_to_inner_html(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", javascript)

    def test_copy_always_produces_plain_text(self):
        """办公软件的富文本框会优先取 text/html。

        降级复制若选中带样式的 DOM 节点，粘贴过去会带上底色；因此所有复制
        都必须经 copyPlainText（内部用临时 textarea，选区只有纯文本一种格式）。
        本服务多以内网 HTTP 访问，navigator.clipboard 不可用，降级路径才是常态。
        """
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
        # 不得再直接选中 DOM 节点来复制
        self.assertNotIn("selectNodeContents", javascript)
        # execCommand 只允许出现在统一的辅助函数里
        self.assertEqual(javascript.count('document.execCommand("copy")'), 1)
        self.assertIn("async function copyPlainText(text)", javascript)
        # 三个复制入口都走辅助函数
        self.assertGreaterEqual(javascript.count("await copyPlainText("), 3)

    def test_entry_requires_explicit_lan_mode(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "svn_sync_web.py").read_text(encoding="utf-8")
        self.assertIn('"0.0.0.0" if args.lan else "127.0.0.1"', source)
        self.assertIn('parser.add_argument(\n        "--lan"', source)


class HttpsEntryTest(unittest.TestCase):
    """HTTPS 入口：参数校验、uvicorn 收到的 TLS 参数、HTTP→HTTPS 跳转监听。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cert = Path(self.temp.name, "server.crt")
        self.key = Path(self.temp.name, "server.key")
        self.ca = Path(self.temp.name, "ca.crt")
        for path in (self.cert, self.key):
            path.write_text("-----BEGIN PLACEHOLDER-----\n", encoding="utf-8")
        self.ca.write_text("-----BEGIN CERTIFICATE-----\nfake-ca\n-----END CERTIFICATE-----\n",
                           encoding="utf-8")

    def test_cert_and_key_must_be_given_together(self):
        import svn_sync_web
        parser = svn_sync_web.build_parser()
        with self.assertRaises(SystemExit):
            svn_sync_web.validate_tls_args(parser.parse_args(["--ssl-certfile", str(self.cert)]))
        with self.assertRaises(SystemExit):
            svn_sync_web.validate_tls_args(parser.parse_args(["--ssl-keyfile", str(self.key)]))
        self.assertTrue(svn_sync_web.validate_tls_args(parser.parse_args(
            ["--ssl-certfile", str(self.cert), "--ssl-keyfile", str(self.key)])))
        self.assertFalse(svn_sync_web.validate_tls_args(parser.parse_args([])))

    def test_missing_cert_file_is_rejected_before_startup(self):
        import svn_sync_web
        args = svn_sync_web.build_parser().parse_args(
            ["--ssl-certfile", str(Path(self.temp.name, "nope.crt")), "--ssl-keyfile", str(self.key)])
        with self.assertRaises(SystemExit):
            svn_sync_web.validate_tls_args(args)

    def test_redirect_port_requires_https_and_distinct_port(self):
        import svn_sync_web
        parser = svn_sync_web.build_parser()
        with self.assertRaises(SystemExit):
            svn_sync_web.validate_tls_args(parser.parse_args(["--redirect-port", "8081"]))
        with self.assertRaises(SystemExit):
            svn_sync_web.validate_tls_args(parser.parse_args([
                "--port", "8081", "--redirect-port", "8081",
                "--ssl-certfile", str(self.cert), "--ssl-keyfile", str(self.key)]))

    def test_uvicorn_receives_tls_files(self):
        import sys
        import svn_sync_web
        from unittest import mock
        fake_uvicorn = mock.Mock()
        with mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            svn_sync_web.main([
                "--port", "8443",
                "--ssl-certfile", str(self.cert), "--ssl-keyfile", str(self.key)])
        kwargs = fake_uvicorn.run.call_args.kwargs
        self.assertEqual(kwargs["ssl_certfile"], str(self.cert))
        self.assertEqual(kwargs["ssl_keyfile"], str(self.key))
        self.assertEqual(kwargs["port"], 8443)
        # 未启用 HTTPS 时不得传 TLS 参数（uvicorn 会因 None 以外的空值报错）
        fake_uvicorn.reset_mock()
        with mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            svn_sync_web.main(["--port", "8765"])
        self.assertNotIn("ssl_certfile", fake_uvicorn.run.call_args.kwargs)

    def test_redirect_listener_sends_301_to_https_and_serves_ca(self):
        import http.client
        import svn_sync_web
        server = svn_sync_web.start_https_redirect(
            "127.0.0.1", 0, 8443, str(self.ca),
            ["127.0.0.1", "localhost", "192.168.30.178"], "192.168.30.178")
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/health?x=1", headers={"Host": "192.168.30.178:8081"})
        response = conn.getresponse()
        self.assertEqual(response.status, 301)
        self.assertEqual(response.getheader("Location"),
                         "https://192.168.30.178:8443/api/health?x=1")
        response.read()

        # Host 不在可信列表或含非法字符：退回默认主机，不把请求头写进 Location
        for bad_host in ("evil.example:8081", "a b", "x\ty"):
            conn.request("GET", "/", headers={"Host": bad_host})
            response = conn.getresponse()
            self.assertEqual(response.status, 301, bad_host)
            self.assertEqual(response.getheader("Location"), "https://192.168.30.178:8443/")
            response.read()

        # POST 同样跳转，不接受任何内容
        conn.request("POST", "/api/v1/auth/login", body="{}",
                     headers={"Host": "192.168.30.178", "Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 301)
        response.read()

        # CA 证书可直接下载，用于首次安装信任
        conn.request("GET", "/ca.crt", headers={"Host": "192.168.30.178"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/x-x509-ca-cert")
        self.assertIn(b"fake-ca", response.read())
        conn.close()

    def test_session_cookie_is_secure_under_https(self):
        client = TestClient(app, base_url="https://testserver")
        client.post("/api/v1/auth/register",
                    json={"username": "httpsuser", "password": "correct-horse"})
        login = client.post("/api/v1/auth/login",
                            json={"username": "httpsuser", "password": "correct-horse"})
        self.assertEqual(login.status_code, 200)
        self.assertIn("secure", login.headers.get("set-cookie", "").lower())
        # 同源 Origin 在 HTTPS 下仍应通过跨站校验
        ok = client.get("/api/v1/auth/me", headers={"origin": "https://testserver"})
        self.assertTrue(ok.json()["authenticated"])


if __name__ == "__main__":
    unittest.main()
