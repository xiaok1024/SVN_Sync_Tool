# -*- coding: utf-8 -*-

import ast
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from web_auth_service import (
    MIN_PASSWORD_LENGTH,
    SESSION_COOKIE,
    AuthError,
    AuthService,
    hash_password,
    normalize_username,
    verify_password,
)


class PasswordHashingTest(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self):
        first, second = hash_password("correct-horse"), hash_password("correct-horse")
        self.assertNotEqual(first, second, "相同密码应产生不同 salt")
        for encoded in (first, second):
            self.assertTrue(verify_password("correct-horse", encoded))
            self.assertFalse(verify_password("Correct-Horse", encoded))
            self.assertFalse(verify_password("", encoded))

    def test_hash_never_contains_the_password(self):
        self.assertNotIn("correct-horse", hash_password("correct-horse"))

    def test_malformed_hash_fails_closed(self):
        for broken in ("", "not-a-hash", "scrypt$bad", "md5$1$1$1$YQ==$YQ==", None):
            self.assertFalse(verify_password("x", broken))

    def test_username_normalization_and_rules(self):
        self.assertEqual(normalize_username("  LiangZeRui "), "liangzerui")
        for bad in ("", "a", "_leading", "有中文", "x" * 33, "with space", "a/b"):
            with self.assertRaises(AuthError, msg=bad):
                normalize_username(bad)


class AuthServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name, "cfg", "web_users.json")
        self.auth = AuthService(store_path=self.store)

    def _register(self, username="demo", password="correct-horse"):
        return self.auth.register(username=username, password=password)

    def test_register_login_and_public_profile_excludes_secrets(self):
        profile = self._register()
        self.assertEqual(profile["username"], "demo")
        self.assertFalse(profile["has_svn_credentials"])
        self.assertNotIn("password", json.dumps(profile))
        self.assertEqual(
            self.auth.authenticate(username="DEMO", password="correct-horse"), "demo")

    def test_duplicate_registration_is_rejected(self):
        self._register()
        with self.assertRaises(AuthError) as caught:
            self._register()
        self.assertEqual(caught.exception.code, "username_taken")
        self.assertEqual(caught.exception.status_code, 409)

    def test_weak_password_is_rejected(self):
        with self.assertRaises(AuthError) as caught:
            self.auth.register(username="demo", password="x" * (MIN_PASSWORD_LENGTH - 1))
        self.assertEqual(caught.exception.code, "weak_password")

    def test_unknown_user_and_wrong_password_are_indistinguishable(self):
        self._register()
        errors = []
        for username, password in (("demo", "wrong"), ("nobody", "wrong")):
            with self.assertRaises(AuthError) as caught:
                self.auth.authenticate(username=username, password=password)
            errors.append((caught.exception.code, caught.exception.message))
        self.assertEqual(errors[0], errors[1], "不得据此枚举已存在的账号")

    def test_login_password_is_hashed_but_svn_password_is_plaintext_by_design(self):
        """使用者明确要求 SVN 密码明文保存；登录密码没有还原需求，仍做哈希。"""
        self._register(password="login-secret-value")
        self.auth.set_svn_credentials(
            "demo", svn_username="lzr", svn_password="svn-secret-value")
        raw = self.store.read_text(encoding="utf-8")
        self.assertNotIn("login-secret-value", raw)
        self.assertIn("svn-secret-value", raw)

    def test_store_file_is_owner_only(self):
        self._register()
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.parent).st_mode), 0o700)

    def test_svn_credentials_round_trip_and_clear(self):
        self._register()
        with self.assertRaises(AuthError) as caught:
            self.auth.get_svn_credentials("demo")
        self.assertEqual(caught.exception.code, "svn_credentials_missing")
        self.assertEqual(caught.exception.status_code, 428)

        self.auth.set_svn_credentials("demo", svn_username="lzr", svn_password="s3cret")
        self.assertEqual(self.auth.get_svn_credentials("demo"), ("lzr", "s3cret"))
        self.assertTrue(self.auth.public_profile("demo")["has_svn_credentials"])

        self.auth.clear_svn_credentials("demo")
        self.assertFalse(self.auth.public_profile("demo")["has_svn_credentials"])
        with self.assertRaises(AuthError):
            self.auth.get_svn_credentials("demo")

    def test_svn_credentials_reject_newline_injection(self):
        self._register()
        for bad in ("user\nname", "user\rname", "user\x00name"):
            with self.assertRaises(AuthError):
                self.auth.set_svn_credentials(
                    "demo", svn_username=bad, svn_password="ok")
            with self.assertRaises(AuthError):
                self.auth.set_svn_credentials(
                    "demo", svn_username="ok", svn_password=bad)

    def test_sessions_resolve_expire_and_revoke(self):
        self._register()
        token = self.auth.create_session("demo")
        self.assertEqual(self.auth.resolve_session(token), "demo")
        self.assertIsNone(self.auth.resolve_session("forged-token"))
        self.assertIsNone(self.auth.resolve_session(""))

        self.auth.destroy_session(token)
        self.assertIsNone(self.auth.resolve_session(token))

    def test_expired_session_is_rejected_and_purged(self):
        self._register()
        token = self.auth.create_session("demo")
        self.auth._sessions[token]["expires_at"] = 0
        self.assertIsNone(self.auth.resolve_session(token))
        self.assertNotIn(token, self.auth._sessions)

    def test_change_password_revokes_every_session(self):
        self._register()
        tokens = [self.auth.create_session("demo") for _ in range(3)]
        self.auth.change_password(
            "demo", current_password="correct-horse", new_password="a-new-password")
        for token in tokens:
            self.assertIsNone(self.auth.resolve_session(token))
        self.assertEqual(
            self.auth.authenticate(username="demo", password="a-new-password"), "demo")

    def test_change_password_requires_the_current_one(self):
        self._register()
        with self.assertRaises(AuthError) as caught:
            self.auth.change_password(
                "demo", current_password="wrong", new_password="a-new-password")
        self.assertEqual(caught.exception.status_code, 401)

    def test_corrupt_store_fails_loudly_instead_of_resetting_accounts(self):
        self._register()
        self.store.write_text("{not json", encoding="utf-8")
        with self.assertRaises(AuthError) as caught:
            self.auth.authenticate(username="demo", password="correct-horse")
        self.assertEqual(caught.exception.code, "user_store_corrupt")

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self._register()
        self.auth.set_svn_credentials("demo", svn_username="lzr", svn_password="s3cret")
        leftovers = [p.name for p in self.store.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_default_store_path_lives_outside_the_repository(self):
        """明文凭据文件绝不能落在仓库里，否则会被提交并推送到 GitHub。"""
        from web_auth_service import default_store_path
        repo = Path(__file__).resolve().parent.parent
        self.assertNotIn(repo, Path(default_store_path()).resolve().parents)


class RouteProtectionTest(unittest.TestCase):
    """全站登录：除登录入口外，任何 /api/v1 路由都必须校验会话。"""

    EXEMPT = {"/api/v1/auth/register", "/api/v1/auth/login",
              "/api/v1/auth/logout", "/api/v1/auth/me"}

    def test_every_business_route_requires_login(self):
        source = Path(__file__).resolve().parent.parent.joinpath("web_app.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        unguarded = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            paths = []
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"get", "post", "put", "delete"}
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)):
                    paths.append(decorator.args[0].value)
            body = ast.get_source_segment(source, node) or ""
            for path in paths:
                if path.startswith("/api/v1/") and path not in self.EXEMPT:
                    if "current_user(request)" not in body:
                        unguarded.append(path)
        self.assertEqual(unguarded, [], "这些路由缺少登录校验")

    def test_svn_credentials_are_not_accepted_from_the_browser(self):
        """SVN 账号密码只能来自服务端存储，不得再由请求体传入。"""
        source = Path(__file__).resolve().parent.parent.joinpath("web_app.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Request"):
                if node.name == "SvnCredentialRequest":
                    continue  # 保存凭据的接口本身必须接收一次
                for field in node.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        if field.target.id in {"svn_username", "svn_password"}:
                            offenders.append("%s.%s" % (node.name, field.target.id))
        self.assertEqual(offenders, [], "这些请求模型仍在接收浏览器提交的 SVN 凭据")

    def test_session_cookie_is_httponly_and_samesite_strict(self):
        source = Path(__file__).resolve().parent.parent.joinpath("web_app.py").read_text(
            encoding="utf-8")
        self.assertIn("httponly=True", source)
        self.assertIn('samesite="strict"', source)
        # Cookie 名必须复用共享常量，不得在路由层另写字面量
        self.assertIn("SESSION_COOKIE", source)
        self.assertNotIn('"%s"' % SESSION_COOKIE, source)


if __name__ == "__main__":
    unittest.main()
