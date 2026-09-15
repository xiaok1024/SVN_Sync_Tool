# -*- coding: utf-8 -*-
"""SVN Sync Tool 的本地 Web 应用。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from json import JSONDecodeError
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from web_auth_service import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    AuthError,
    AuthService,
)
from web_upgrade_service import UpgradeWebError, extract_upgrade_list, generate_upgrade_markdown
from web_path_service import PathQueryService, PathWebError, sort_revision_path_text
from web_standard_service import StandardJobManager, StandardWebError


LOGGER = logging.getLogger("svn_sync_web")
PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class NormalizeHostHeader:
    """把 Host 头统一转小写后再交给 TrustedHostMiddleware。

    主机名本身大小写不敏感（RFC 3986），但 Starlette 的 TrustedHostMiddleware
    是逐字比较，而可信列表是小写的。某些客户端会原样发送用户输入的大小写
    （本机真实主机名就是 LZR-MBP-M5.local），不归一化会被误拒为 400。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = []
            for key, value in scope["headers"]:
                if key == b"host":
                    value = value.lower()
                headers.append((key, value))
            scope = dict(scope, headers=headers)
        await self.app(scope, receive, send)


def _allowed_hosts():
    """可信 Host：本机回环 + 环境变量（``--lan`` 启动时由 svn_sync_web 注入探测结果）。"""
    configured = os.environ.get("SVN_SYNC_WEB_ALLOWED_HOSTS", "")
    hosts = {"127.0.0.1", "localhost"}
    hosts.update(value.strip().lower() for value in configured.split(",") if value.strip())
    return sorted(hosts)


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_text: str
    format: str


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


class SvnCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    svn_username: str
    svn_password: str


class StandardJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    svn_url: str
    source_profile_id: str
    customer_standard_path: str
    file_list: str
    cover_all_confirmed: bool = False
    commit_message: str


class RevisionPathQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    svn_url: str
    revision_spec: str
    sort: str = "rev"


class RevisionPathSortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    sort: str = "rev"


class StandardCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str
    idempotency_key: str


def _error_response(status_code, code, message):
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


async def _read_json(request, model_type):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise UpgradeWebError(
            "unsupported_media_type",
            "接口只接受 application/json",
            status_code=415,
        )

    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_REQUEST_BYTES:
                raise UpgradeWebError("request_too_large", "请求内容超过大小限制", status_code=413)
        except ValueError:
            raise UpgradeWebError("invalid_content_length", "Content-Length 格式不正确", status_code=400) from None

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise UpgradeWebError("request_too_large", "请求内容超过大小限制", status_code=413)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError):
        raise UpgradeWebError("malformed_json", "请求 JSON 格式不正确", status_code=400) from None
    if not isinstance(payload, dict):
        raise UpgradeWebError("invalid_json", "请求 JSON 必须是对象", status_code=400)
    try:
        return model_type.model_validate(payload)
    except ValidationError:
        raise UpgradeWebError("invalid_field", "请求字段缺失或类型不正确") from None


def _deploy_info():
    """线上版本标记，由 svn-sync-deploy 发布时写入；开发机上没有该文件则为空。"""
    path = os.environ.get("SVN_SYNC_WEB_DEPLOY_INFO") or str(PROJECT_ROOT / ".deployed.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key) or "")[:120]
            for key in ("commit", "short", "subject", "deployed_at", "by")}


PATH_QUERY_SERVICE = PathQueryService.from_environment()
AUTH_SERVICE = AuthService.from_environment()

try:
    STANDARD_JOB_MANAGER = StandardJobManager.from_environment()
except ValueError as exc:
    LOGGER.error("Web standard source configuration is invalid: %s", exc)
    STANDARD_JOB_MANAGER = StandardJobManager(profiles=[])


@asynccontextmanager
async def lifespan(application):
    application.state.standard_jobs.start()
    try:
        yield
    finally:
        application.state.standard_jobs.stop()


app = FastAPI(
    title="LZR 升级工具中心",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.state.standard_jobs = STANDARD_JOB_MANAGER
app.state.path_queries = PATH_QUERY_SERVICE
app.state.auth = AUTH_SERVICE
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_allowed_hosts(),
)
# 后加入者位于更外层：先归一化 Host，再交给上面的可信 Host 校验
app.add_middleware(NormalizeHostHeader)
app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=WEB_ROOT / "templates")


@app.middleware("http")
async def reject_cross_site_requests(request, call_next):
    """拒绝浏览器从其他站点发起的写请求，降低局域网模式下的 CSRF 风险。"""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return _error_response(403, "cross_site_request", "不允许跨站请求")
        origin = request.headers.get("origin", "").strip()
        if origin:
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin.rstrip("/").lower() != expected.rstrip("/").lower():
                return _error_response(403, "origin_mismatch", "请求来源与当前网站不一致")
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        # 本地工具会频繁改前端；必须每次回源校验，避免页面跑旧 JS/CSS。
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    # 会话滑动续期：接口已签发新令牌时重新下发 cookie；接口自己已经写过
    # 该 cookie（登出、改密的删除动作）则不再覆盖。
    renewed = getattr(request.state, "renewed_session", None)
    if renewed and not any(
            value.startswith(SESSION_COOKIE + "=")
            for value in response.headers.getlist("set-cookie")):
        _set_session_cookie(response, renewed, request.url.scheme == "https")
    return response


@app.exception_handler(UpgradeWebError)
async def handle_upgrade_error(_request, exc):
    return _error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(StandardWebError)
@app.exception_handler(PathWebError)
@app.exception_handler(AuthError)
async def handle_svn_web_error(_request, exc):
    return _error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request, _exc):
    return _error_response(422, "invalid_request", "请求参数不正确")


@app.exception_handler(Exception)
async def handle_unknown_error(request, exc):
    LOGGER.exception("Web request failed: %s %s", request.method, request.url.path, exc_info=exc)
    return _error_response(500, "internal_error", "处理失败，请稍后重试")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_version": "0.2.0", "deployed": _deploy_info()},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """老客户端和 Safari 不看 <link rel=icon> 里的 SVG，会直接请求根目录的 ico。"""
    return FileResponse(WEB_ROOT / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/api/health")
async def health():
    profiles = app.state.standard_jobs.public_profiles()
    return {
        "ok": True,
        "service": "svn-sync-toolbox",
        "version": "0.2.0",
        "standard_files_configured": any(
            profile["available"] for profile in profiles["profiles"]),
        "deployed": _deploy_info(),
    }


def _session_user(request):
    """解析会话 cookie；令牌已过半寿命时顺带续期，由 add_security_headers 下发。"""
    auth = request.app.state.auth
    token = request.cookies.get(SESSION_COOKIE, "")
    username = auth.resolve_session(token)
    if username:
        renewed = auth.renew_session(token)
        if renewed:
            request.state.renewed_session = renewed
    return username


def current_user(request):
    """返回当前会话对应的账号；未登录抛 401。

    全站功能都要求登录：既是使用者的明确要求，也让后续执行 SVN 时
    能确定该用谁的凭据。
    """
    username = _session_user(request)
    if not username:
        raise AuthError("login_required", "请先登录", 401)
    return username


def _set_session_cookie(response, token, secure):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


@app.post("/api/v1/auth/register")
async def register(request: Request):
    payload = await _read_json(request, RegisterRequest)
    profile = await run_in_threadpool(
        request.app.state.auth.register,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
    )
    # 与其余接口保持同一响应外形：前端统一以 ok 判定成败。
    return {"ok": True, "user": profile}


@app.post("/api/v1/auth/login")
async def login(request: Request):
    payload = await _read_json(request, LoginRequest)
    auth = request.app.state.auth
    username = await run_in_threadpool(
        auth.authenticate, username=payload.username, password=payload.password)
    token = auth.create_session(username)
    profile = await run_in_threadpool(auth.public_profile, username)
    response = JSONResponse({"ok": True, "user": profile})
    _set_session_cookie(response, token, request.url.scheme == "https")
    return response


@app.post("/api/v1/auth/logout")
async def logout(request: Request):
    request.app.state.auth.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/v1/auth/me")
async def whoami(request: Request):
    """未登录不算错误，返回 authenticated=false 供前端决定显示登录页。"""
    username = _session_user(request)
    if not username:
        return {"ok": True, "authenticated": False, "user": None}
    profile = await run_in_threadpool(request.app.state.auth.public_profile, username)
    return {"ok": True, "authenticated": True, "user": profile}


@app.post("/api/v1/auth/password")
async def change_password(request: Request):
    username = current_user(request)
    payload = await _read_json(request, ChangePasswordRequest)
    await run_in_threadpool(
        request.app.state.auth.change_password, username,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    # 改密会吊销全部会话，当前浏览器也需要重新登录。
    response = JSONResponse({"ok": True, "reauth_required": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/v1/auth/svn-credentials")
async def read_svn_credentials(request: Request):
    """回显当前登录人自己保存的 SVN 账号与密码。

    只有这个接口返回密码，且只返回调用者本人的：/me 与保存接口都不含密码，
    避免每次加载页面都把它传一遍。使用者明确要求弹窗里明文显示。
    """
    username = current_user(request)
    auth = request.app.state.auth
    profile = await run_in_threadpool(auth.public_profile, username)
    if not profile["has_svn_credentials"]:
        return {"ok": True, "svn_username": "", "svn_password": "", "configured": False}
    svn_user, svn_pass = await run_in_threadpool(auth.get_svn_credentials, username)
    return {"ok": True, "svn_username": svn_user, "svn_password": svn_pass,
            "configured": True}


@app.put("/api/v1/auth/svn-credentials")
async def save_svn_credentials(request: Request):
    username = current_user(request)
    payload = await _read_json(request, SvnCredentialRequest)
    await run_in_threadpool(
        request.app.state.auth.set_svn_credentials, username,
        svn_username=payload.svn_username,
        svn_password=payload.svn_password,
    )
    profile = await run_in_threadpool(request.app.state.auth.public_profile, username)
    return {"ok": True, "user": profile}


@app.delete("/api/v1/auth/svn-credentials")
async def delete_svn_credentials(request: Request):
    username = current_user(request)
    await run_in_threadpool(request.app.state.auth.clear_svn_credentials, username)
    profile = await run_in_threadpool(request.app.state.auth.public_profile, username)
    return {"ok": True, "user": profile}


@app.post("/api/v1/upgrade-list/extract")
async def extract(request: Request):
    current_user(request)
    payload = await _read_json(request, ExtractRequest)
    return await run_in_threadpool(extract_upgrade_list, payload.html)


@app.post("/api/v1/upgrade-list/generate")
async def generate(request: Request):
    current_user(request)
    payload = await _read_json(request, GenerateRequest)
    return await run_in_threadpool(generate_upgrade_markdown, payload.list_text, payload.format)


@app.post("/api/v1/revision-paths/query")
async def query_revision_paths_endpoint(request: Request):
    username = current_user(request)
    payload = await _read_json(request, RevisionPathQueryRequest)
    # 一律使用登录人保存的 SVN 凭据，查询结果与提交归属保持同一账号。
    svn_user, svn_pass = await run_in_threadpool(
        request.app.state.auth.get_svn_credentials, username)
    return await run_in_threadpool(
        request.app.state.path_queries.query,
        svn_url=payload.svn_url,
        username=svn_user,
        password=svn_pass,
        revision_spec=payload.revision_spec,
        sort=payload.sort,
    )


@app.post("/api/v1/revision-paths/sort")
async def sort_revision_paths_endpoint(request: Request):
    current_user(request)
    payload = await _read_json(request, RevisionPathSortRequest)
    return await run_in_threadpool(sort_revision_path_text, payload.text, payload.sort)


def _job_token(request):
    value = request.headers.get("x-lzr-job-token", "").strip()
    if not value:
        raise StandardWebError("job_token_required", "缺少任务访问凭证", 401)
    return value


@app.get("/api/v1/standard-files/source-profiles")
async def standard_source_profiles(request: Request):
    current_user(request)
    return request.app.state.standard_jobs.public_profiles()


@app.post("/api/v1/standard-files/tasks", status_code=202)
async def create_standard_task(request: Request):
    username = current_user(request)
    payload = await _read_json(request, StandardJobRequest)
    if not payload.customer_standard_path.strip():
        raise StandardWebError(
            "invalid_customer_path", "请填写客户标准文件 ecology 目录")
    # 提交动作一律用当前登录人保存的 SVN 账号，确保 commit 归属正确。
    svn_user, svn_pass = await run_in_threadpool(
        request.app.state.auth.get_svn_credentials, username)
    return await run_in_threadpool(
        request.app.state.standard_jobs.create_job,
        svn_url=payload.svn_url,
        username=svn_user,
        password=svn_pass,
        profile_id=payload.source_profile_id,
        customer_standard_path=payload.customer_standard_path,
        file_list=payload.file_list,
        cover_all_confirmed=payload.cover_all_confirmed,
        commit_message=payload.commit_message,
    )


@app.get("/api/v1/standard-files/tasks/{job_id}")
async def get_standard_task(request: Request, job_id: str):
    current_user(request)
    return await run_in_threadpool(
        request.app.state.standard_jobs.snapshot, job_id, _job_token(request))


@app.post("/api/v1/standard-files/tasks/{job_id}/commit", status_code=202)
async def commit_standard_task(request: Request, job_id: str):
    current_user(request)
    token = _job_token(request)
    payload = await _read_json(request, StandardCommitRequest)
    return await run_in_threadpool(
        request.app.state.standard_jobs.request_commit,
        job_id,
        token,
        payload.confirmation_token,
        payload.idempotency_key,
    )


@app.delete("/api/v1/standard-files/tasks/{job_id}")
async def cancel_standard_task(request: Request, job_id: str):
    current_user(request)
    return await run_in_threadpool(
        request.app.state.standard_jobs.cancel, job_id, _job_token(request))
