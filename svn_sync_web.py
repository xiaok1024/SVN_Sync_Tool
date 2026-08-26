#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动升级工具中心 Web 服务。"""

import argparse
import os
import socket


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


def build_parser():
    parser = argparse.ArgumentParser(description="启动 LZR 升级工具中心")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="允许局域网访问；未指定时仍只监听本机",
    )
    parser.add_argument("--reload", action="store_true", help="开发时自动重载")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1-65535 之间")
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "缺少 Web 依赖，请先执行：.venv-web/bin/python -m pip install -r requirements-web.txt"
        ) from None
    if args.lan:
        os.environ["SVN_SYNC_WEB_ALLOWED_HOSTS"] = ",".join(_lan_allowed_hosts())
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0" if args.lan else "127.0.0.1",
        port=args.port,
        reload=args.reload,
        access_log=False,
    )


if __name__ == "__main__":
    main()
