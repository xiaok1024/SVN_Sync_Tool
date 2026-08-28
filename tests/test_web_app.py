# -*- coding: utf-8 -*-

import json
import unittest
from pathlib import Path


try:
    from fastapi.testclient import TestClient
    from web_app import MAX_REQUEST_BYTES, app
    WEB_AVAILABLE = True
except ModuleNotFoundError:
    TestClient = None
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
        cls.client = TestClient(app)

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
        password = "do-not-echo-this-password"
        response = self.client.post(
            "/api/v1/standard-files/tasks",
            json={
                "svn_url": "https://svn.example.com/svn/customer/ecology",
                "svn_username": "demo-user",
                "svn_password": password,
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
                "svn_username": "demo-user",
                "svn_password": "demo-password",
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

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/", headers={"host": "untrusted.example"})
        self.assertEqual(response.status_code, 400)

    def test_local_mac_mini_hostname_is_allowed(self):
        response = self.client.get(
            "/api/health", headers={"host": "lzr-mac-mini.local:8765"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_cross_site_write_request_is_rejected(self):
        response = self.client.post(
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
        password = "do-not-echo-this-password"
        response = self.client.post(
            "/api/v1/revision-paths/query",
            json={
                "svn_url": "https://svn.example.com/svn/customer",
                "svn_username": "demo-user",
                "svn_password": password,
                "revision_spec": "not-a-revision",
                "sort": "rev",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_revision_spec")
        self.assertNotIn(password, response.text)

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
        for path in ("/api/v1/revision-paths/query", "/api/v1/revision-paths/sort"):
            response = self.client.post(
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

    def test_static_route_cannot_read_repository_files(self):
        response = self.client.get("/static/%2e%2e/README.md")
        self.assertEqual(response.status_code, 404)


class WebSourceSafetyTest(unittest.TestCase):
    def test_frontend_never_assigns_untrusted_content_to_inner_html(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", javascript)

    def test_entry_requires_explicit_lan_mode(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "svn_sync_web.py").read_text(encoding="utf-8")
        self.assertIn('"0.0.0.0" if args.lan else "127.0.0.1"', source)
        self.assertIn('parser.add_argument(\n        "--lan"', source)


if __name__ == "__main__":
    unittest.main()
