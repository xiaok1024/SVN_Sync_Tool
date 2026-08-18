#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动仅供本机预览的升级工具中心 Web 服务。"""

import argparse


def build_parser():
    parser = argparse.ArgumentParser(description="启动 LZR 升级工具中心（仅监听本机）")
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口，默认 8765")
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
    uvicorn.run(
        "web_app:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
        access_log=False,
    )


if __name__ == "__main__":
    main()
