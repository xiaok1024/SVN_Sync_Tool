# -*- coding: utf-8 -*-
"""SVN Sync Tool 的本地 Web 应用。"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from json import JSONDecodeError
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from web_upgrade_service import UpgradeWebError, extract_upgrade_list, generate_upgrade_markdown
from web_standard_service import StandardJobManager, StandardWebError


LOGGER = logging.getLogger("svn_sync_web")
PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class ExtractRequest(BaseModel):
    html: str


class GenerateRequest(BaseModel):
    list_text: str
    format: str


class StandardJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    svn_url: str
    svn_username: str
    svn_password: str
    source_profile_id: str
    customer_standard_path: str
    file_list: str
    cover_all_confirmed: bool = False
    commit_message: str


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
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "lzr-mac-mini.local", "testserver"],
)
app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=WEB_ROOT / "templates")


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
    return response


@app.exception_handler(UpgradeWebError)
async def handle_upgrade_error(_request, exc):
    return _error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(StandardWebError)
async def handle_standard_error(_request, exc):
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
        context={"app_version": "0.2.0"},
    )


@app.get("/api/health")
async def health():
    profiles = app.state.standard_jobs.public_profiles()
    return {
        "ok": True,
        "service": "svn-sync-toolbox",
        "version": "0.2.0",
        "standard_files_configured": any(
            profile["available"] for profile in profiles["profiles"]),
    }


@app.post("/api/v1/upgrade-list/extract")
async def extract(request: Request):
    payload = await _read_json(request, ExtractRequest)
    return await run_in_threadpool(extract_upgrade_list, payload.html)


@app.post("/api/v1/upgrade-list/generate")
async def generate(request: Request):
    payload = await _read_json(request, GenerateRequest)
    return await run_in_threadpool(generate_upgrade_markdown, payload.list_text, payload.format)


def _job_token(request):
    value = request.headers.get("x-lzr-job-token", "").strip()
    if not value:
        raise StandardWebError("job_token_required", "缺少任务访问凭证", 401)
    return value


@app.get("/api/v1/standard-files/source-profiles")
async def standard_source_profiles(request: Request):
    return request.app.state.standard_jobs.public_profiles()


@app.post("/api/v1/standard-files/tasks", status_code=202)
async def create_standard_task(request: Request):
    payload = await _read_json(request, StandardJobRequest)
    if not payload.customer_standard_path.strip():
        raise StandardWebError(
            "invalid_customer_path", "请填写客户标准文件 ecology 目录")
    return await run_in_threadpool(
        request.app.state.standard_jobs.create_job,
        svn_url=payload.svn_url,
        username=payload.svn_username,
        password=payload.svn_password,
        profile_id=payload.source_profile_id,
        customer_standard_path=payload.customer_standard_path,
        file_list=payload.file_list,
        cover_all_confirmed=payload.cover_all_confirmed,
        commit_message=payload.commit_message,
    )


@app.get("/api/v1/standard-files/tasks/{job_id}")
async def get_standard_task(request: Request, job_id: str):
    return await run_in_threadpool(
        request.app.state.standard_jobs.snapshot, job_id, _job_token(request))


@app.post("/api/v1/standard-files/tasks/{job_id}/commit", status_code=202)
async def commit_standard_task(request: Request, job_id: str):
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
    return await run_in_threadpool(
        request.app.state.standard_jobs.cancel, job_id, _job_token(request))
