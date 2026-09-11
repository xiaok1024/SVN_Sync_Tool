#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动升级工具中心 Web 服务。

支持两种监听方式：

- 纯 HTTP（默认，本机预览用）；
- HTTPS：传入 ``--ssl-certfile`` / ``--ssl-keyfile`` 后由 uvicorn 直接终止 TLS。
  可选 ``--redirect-port`` 再起一个极简 HTTP 监听，把旧地址 301 到 HTTPS，
  并在 ``/ca.crt`` 提供私有 CA 证书，方便同事首次安装信任。

局域网通过 IP 访问时 HSTS 不起作用（RFC 6797 明确排除 IP 字面量），因此不设该头；
把旧端口做成跳转即可保证所有人最终都落到 HTTPS。
"""

import argparse
import http.server
import os
import re
import socket
import threading

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def _lan_allowed_hosts():
    """收集当前主机可用的精确 Host，避免局域网模式放开任意 Host。"""
    hosts = {"127.0.0.1", "localhost"}
    hostname = socket.gethostname().strip().lower()
    if hostname:
        hosts.add(hostname)
        short_name = hostname.split(".", 1)[0]
        if short_name:
            hosts.add(short_name)
            hosts.add(f"{short_name}.local")
    for candidate in tuple(hosts):
        try:
            for info in socket.getaddrinfo(candidate, None, family=socket.AF_INET):
                address = info[4][0]
                if address:
                    hosts.add(address)
        except OSError:
            continue
    configured = os.environ.get("SVN_SYNC_WEB_ALLOWED_HOSTS", "")
    hosts.update(value.strip().lower() for value in configured.split(",") if value.strip())
    return sorted(hosts)


class HttpsRedirectHandler(http.server.BaseHTTPRequestHandler):
    """把所有 HTTP 请求 301 到 HTTPS；唯一例外是 ``/ca.crt`` 直接下发 CA 证书。

    跳转目标取请求里的 Host（去掉端口）拼上 HTTPS 端口：同事用什么地址进来，
    就跳到同一地址的 HTTPS 版本。Host 只接受主机名合法字符，且必须在可信列表内，
    否则退回默认主机，避免把请求头里的内容原样写进 Location。
    """

    https_port = 8443
    ca_certfile = ""
    allowed_hosts = frozenset()
    fallback_host = "localhost"
    server_version = "svn-sync-redirect/1"
    sys_version = ""

    def _redirect_target(self):
        raw = self.headers.get("Host", "")
        host = raw.split(":", 1)[0].strip().lower()
        if not _HOST_RE.match(host) or (self.allowed_hosts and host not in self.allowed_hosts):
            host = self.fallback_host
        path = self.path if self.path.startswith("/") else "/"
        return "https://%s:%d%s" % (host, self.https_port, path)

    def _send_redirect(self, include_body=True):
        target = self._redirect_target()
        self.send_response(301)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = ("请使用 HTTPS 访问：%s\n" % target).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _send_ca(self, include_body=True):
        try:
            with open(self.ca_certfile, "rb") as handle:
                data = handle.read()
        except OSError:
            self.send_error(404, "CA certificate not available")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Disposition", 'attachment; filename="lzr-svn-sync-ca.crt"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/ca.crt" and self.ca_certfile:
            self._send_ca()
        else:
            self._send_redirect()

    def do_HEAD(self):
        if self.path.split("?", 1)[0] == "/ca.crt" and self.ca_certfile:
            self._send_ca(include_body=False)
        else:
            self._send_redirect(include_body=False)

    # 其余方法一律跳转；浏览器会把非幂等请求改成 GET，这里本就没有可提交的内容
    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def log_message(self, _format, *_args):
        return


def start_https_redirect(bind_host, redirect_port, https_port, ca_certfile,
                         allowed_hosts, fallback_host):
    """在守护线程里起 HTTP→HTTPS 跳转监听，返回服务器对象（便于测试关闭）。"""
    handler = type("BoundRedirectHandler", (HttpsRedirectHandler,), {
        "https_port": https_port,
        "ca_certfile": ca_certfile or "",
        "allowed_hosts": frozenset(h.lower() for h in allowed_hosts),
        "fallback_host": fallback_host,
    })
    server = http.server.ThreadingHTTPServer((bind_host, redirect_port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="https-redirect", daemon=True)
    thread.start()
    return server


def build_parser():
    parser = argparse.ArgumentParser(description="启动 LZR 升级工具中心")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="允许局域网访问；未指定时仍只监听本机",
    )
    parser.add_argument("--reload", action="store_true", help="开发时自动重载")
    tls = parser.add_argument_group("HTTPS")
    tls.add_argument("--ssl-certfile", default="", help="服务器证书（PEM）；与 --ssl-keyfile 同时给出即启用 HTTPS")
    tls.add_argument("--ssl-keyfile", default="", help="服务器私钥（PEM）")
    tls.add_argument("--ca-certfile", default="", help="私有 CA 证书；给出后在跳转端口的 /ca.crt 提供下载")
    tls.add_argument(
        "--redirect-port", type=int, default=0,
        help="额外监听一个 HTTP 端口，把请求 301 到 HTTPS；仅在启用 HTTPS 时有效")
    return parser


def validate_tls_args(args):
    """校验 TLS 相关参数组合，返回是否启用 HTTPS。"""
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        raise SystemExit("--ssl-certfile 与 --ssl-keyfile 必须同时提供")
    enabled = bool(args.ssl_certfile)
    if enabled:
        for label, path in (("证书", args.ssl_certfile), ("私钥", args.ssl_keyfile)):
            if not os.path.isfile(path):
                raise SystemExit("HTTPS %s文件不存在: %s" % (label, path))
    if args.ca_certfile and not os.path.isfile(args.ca_certfile):
        raise SystemExit("CA 证书文件不存在: %s" % args.ca_certfile)
    if args.redirect_port:
        if not enabled:
            raise SystemExit("--redirect-port 只能与 HTTPS 一起使用")
        if not 1 <= args.redirect_port <= 65535 or args.redirect_port == args.port:
            raise SystemExit("--redirect-port 必须是 1-65535 且不能与 --port 相同")
    return enabled


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1-65535 之间")
    https_enabled = validate_tls_args(args)
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "缺少 Web 依赖，请先执行：.venv-web/bin/python -m pip install -r requirements-web.txt"
        ) from None
    allowed_hosts = _lan_allowed_hosts() if args.lan else ["127.0.0.1", "localhost"]
    if args.lan:
        os.environ["SVN_SYNC_WEB_ALLOWED_HOSTS"] = ",".join(allowed_hosts)
    bind_host = "0.0.0.0" if args.lan else "127.0.0.1"

    if args.redirect_port:
        # 跳转目标的兜底主机：优先取显式配置的第一个非回环地址
        fallback = next(
            (h for h in allowed_hosts if h not in {"127.0.0.1", "localhost"}), "localhost")
        start_https_redirect(
            bind_host, args.redirect_port, args.port, args.ca_certfile, allowed_hosts, fallback)

    run_kwargs = {
        "host": bind_host,
        "port": args.port,
        "reload": args.reload,
        "access_log": False,
    }
    if https_enabled:
        run_kwargs["ssl_certfile"] = args.ssl_certfile
        run_kwargs["ssl_keyfile"] = args.ssl_keyfile
    uvicorn.run("web_app:app", **run_kwargs)


if __name__ == "__main__":
    main()
