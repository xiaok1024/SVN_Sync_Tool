#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 账号的命令行维护工具（列出、重置密码、删除）。

为什么需要它：登录密码只存 scrypt 哈希，不可还原，页面上也没有找回入口。
管理员在服务所在主机上用本工具重置即可——密码走隐藏输入，不进命令行参数、
不进 shell 历史、不打印到终端。

重置登录密码**不影响**该账号已保存的 SVN 凭据：两者是独立字段，
SVN 密码按明文单独保存（svn 命令需要真实密码）。

用法：
    python3 manage_web_users.py list
    python3 manage_web_users.py reset-password <账号>
    python3 manage_web_users.py delete <账号>

账号库位置默认取 SVN_SYNC_WEB_USER_STORE，未设置时用 web_auth_service 的默认路径。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from web_auth_service import AuthError, AuthService, validate_login_password


def _service(args):
    return AuthService(store_path=args.store or None)


def command_list(args):
    auth = _service(args)
    data = auth._read()["users"]
    if not data:
        print("账号库为空")
        return 0
    print("%-20s %-16s %-12s %s" % ("账号", "显示名", "SVN 账号", "已设 SVN 密码"))
    print("-" * 64)
    for name, record in sorted(data.items()):
        print("%-20s %-16s %-12s %s" % (
            name,
            record.get("display_name") or "",
            record.get("svn_username") or "-",
            "是" if record.get("svn_password") else "否",
        ))
    return 0


def command_reset_password(args):
    auth = _service(args)
    if args.username not in auth._read()["users"]:
        print("账号不存在: %s" % args.username, file=sys.stderr)
        return 1

    first = getpass.getpass("为 %s 设置新登录密码（输入不回显）: " % args.username)
    second = getpass.getpass("再输入一次确认: ")
    if first != second:
        print("两次输入不一致，未做任何修改", file=sys.stderr)
        return 1
    try:
        validate_login_password(first)
    except AuthError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    # 直接改哈希：管理员重置不需要提供原密码，change_password 则要求原密码
    with auth._lock:
        data = auth._read()
        from web_auth_service import hash_password
        data["users"][args.username]["password_hash"] = hash_password(first)
        auth._write(data)
    auth.revoke_all_sessions(args.username)
    print("已重置 %s 的登录密码；该账号的既有会话已全部失效。" % args.username)
    print("已保存的 SVN 凭据不受影响。")
    return 0


def command_delete(args):
    auth = _service(args)
    data = auth._read()
    if args.username not in data["users"]:
        print("账号不存在: %s" % args.username, file=sys.stderr)
        return 1
    answer = input("确认删除账号 %s 及其保存的 SVN 凭据？输入 yes 确认: " % args.username)
    if answer.strip().lower() != "yes":
        print("已取消")
        return 1
    with auth._lock:
        data = auth._read()
        data["users"].pop(args.username, None)
        auth._write(data)
    auth.revoke_all_sessions(args.username)
    print("已删除 %s" % args.username)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Web 账号维护")
    parser.add_argument("--store", default="", help="账号库路径，默认取环境变量或内置默认值")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出全部账号（不显示任何密码）")
    reset = sub.add_parser("reset-password", help="重置某个账号的登录密码")
    reset.add_argument("username")
    delete = sub.add_parser("delete", help="删除账号")
    delete.add_argument("username")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {
        "list": command_list,
        "reset-password": command_reset_password,
        "delete": command_delete,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
