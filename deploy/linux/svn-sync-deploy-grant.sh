#!/usr/bin/env bash
# 给同事一把"只能触发发布"的钥匙（root 执行）。
#
# 做法：专用账号 svn-deploy（锁定密码、不能交互登录）+ authorized_keys 强制命令
# + sudoers 只放行 /usr/local/sbin/svn-sync-deploy 这一条。持钥匙的人连上来只会
# 看到发布脚本的输出，开不了 shell、读不了文件、改不了配置；撤销就是删掉那行公钥。
#
# 用法：
#   svn-sync-deploy-grant.sh <名字> '<公钥整行>'    授予（重复执行会替换同名的旧钥匙）
#   svn-sync-deploy-grant.sh --revoke <名字>         撤销
#   svn-sync-deploy-grant.sh --list                  查看已授权的人

set -euo pipefail

ACCOUNT=svn-deploy
DEPLOY_BIN=/usr/local/sbin/svn-sync-deploy
SUDOERS=/etc/sudoers.d/svn-sync-deploy
HOME_DIR="/home/$ACCOUNT"
KEYS="$HOME_DIR/.ssh/authorized_keys"

[ "$(id -u)" -eq 0 ] || { echo "必须以 root 运行" >&2; exit 1; }

ensure_account() {
    if ! getent passwd "$ACCOUNT" >/dev/null; then
        # 强制命令由登录 shell 执行，因此 shell 不能是 nologin；密码锁定 + sshd 已关密码登录
        useradd -m -s /bin/bash -c "SVN Sync deploy trigger" "$ACCOUNT"
    fi
    passwd -l "$ACCOUNT" >/dev/null
    # .ssh 与 authorized_keys 归 root：账号本人改不了自己的钥匙（StrictModes 允许 root 属主）
    mkdir -p "$HOME_DIR/.ssh"
    chown root:root "$HOME_DIR/.ssh"; chmod 755 "$HOME_DIR/.ssh"
    touch "$KEYS"; chown root:root "$KEYS"; chmod 644 "$KEYS"

    local tmp
    tmp="$(mktemp)"
    printf '%s ALL=(root) NOPASSWD: %s --by=*\n' "$ACCOUNT" "$DEPLOY_BIN" > "$tmp"
    visudo -cf "$tmp" >/dev/null
    install -m 0440 -o root -g root "$tmp" "$SUDOERS"
    rm -f "$tmp"
}

case "${1:-}" in
    --list)
        [ -f "$KEYS" ] && sed -n 's/.*--by=\([^"]*\)".*/  \1/p' "$KEYS" || echo "  （无）"
        exit 0 ;;
    --revoke)
        name="${2:-}"
        [[ "$name" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || { echo "名字只能是字母数字 . _ -" >&2; exit 2; }
        [ -f "$KEYS" ] || exit 0
        grep -v -- "--by=$name\"" "$KEYS" > "$KEYS.tmp" || true
        install -m 0644 -o root -g root "$KEYS.tmp" "$KEYS"; rm -f "$KEYS.tmp"
        echo "已撤销 $name 的发布权限"
        exit 0 ;;
esac

name="${1:-}"; pubkey="${2:-}"
[[ "$name" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || { echo "用法: $0 <名字> '<公钥整行>'；名字只能是字母数字 . _ -" >&2; exit 2; }
[ -n "$pubkey" ] || { echo "缺少公钥" >&2; exit 2; }
[[ "$pubkey" != *$'\n'* ]] || { echo "公钥必须是一行" >&2; exit 2; }
tmp="$(mktemp)"; printf '%s\n' "$pubkey" > "$tmp"
if ! fingerprint="$(ssh-keygen -l -f "$tmp" 2>/dev/null)"; then
    rm -f "$tmp"; echo "这不是有效的 SSH 公钥" >&2; exit 2
fi
rm -f "$tmp"

ensure_account
grep -v -- "--by=$name\"" "$KEYS" > "$KEYS.tmp" || true
# restrict = 禁 pty/转发/agent/X11 等全部附加能力；command = 只能跑这一条
printf 'restrict,command="sudo %s --by=%s" %s\n' "$DEPLOY_BIN" "$name" "$pubkey" >> "$KEYS.tmp"
install -m 0644 -o root -g root "$KEYS.tmp" "$KEYS"; rm -f "$KEYS.tmp"

echo "已授予 $name 发布权限（$fingerprint）"
echo "对方在自己电脑上执行即可发布：  ssh $ACCOUNT@$(hostname -I | awk '{print $1}')"
