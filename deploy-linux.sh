#!/usr/bin/env bash
# 把当前工作区的 Web 服务同步到 Linux 主机并重启。
#
# 只推送源码（约 200 KB），不含 .git、venv、outputs 下的 exe/app.zip——
# 服务器用不到那些，而且仓库历史里的二进制会让 git clone 极慢且易断。
#
# 用法：
#   ./deploy-linux.sh              # 同步 + 重启
#   ./deploy-linux.sh --test       # 同步 + 在服务器上跑测试（不重启服务）
#   ./deploy-linux.sh --deps       # 同步 + 重装依赖 + 重启（改了 requirements-web.txt 时用）

set -euo pipefail

SSH_HOST="${SVN_SYNC_DEPLOY_HOST:-ubuntu-root}"
REMOTE_DIR="${SVN_SYNC_DEPLOY_DIR:-/opt/svn-sync-tool}"
SERVICE="${SVN_SYNC_DEPLOY_SERVICE:-svn-sync-web}"
SSH_OPTS=(-o ConnectTimeout=20 -o ClearAllForwardings=yes)

run_tests=0
reinstall_deps=0
for arg in "$@"; do
    case "$arg" in
        --test) run_tests=1 ;;
        --deps) reinstall_deps=1 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")"
bundle="$(mktemp -t svn-sync-deploy.XXXXXX).tar.gz"
trap 'rm -f "$bundle"' EXIT

# COPYFILE_DISABLE=1 避免 macOS 的 ._ 副产品混进包里
COPYFILE_DISABLE=1 tar czf "$bundle" \
    --exclude='./.git' --exclude='./.venv*' --exclude='./dist' --exclude='./build' \
    --exclude='./outputs' --exclude='./.idea' --exclude='__pycache__' \
    --exclude='.DS_Store' --exclude='./README.assets' --exclude='./qt_assets' \
    .
echo "源码包 $(du -h "$bundle" | cut -f1) → ${SSH_HOST}:${REMOTE_DIR}"

scp -q "${SSH_OPTS[@]}" "$bundle" "${SSH_HOST}:/tmp/svn-sync-deploy.tar.gz"
ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
    "mkdir -p '$REMOTE_DIR' && tar xzf /tmp/svn-sync-deploy.tar.gz -C '$REMOTE_DIR' && rm -f /tmp/svn-sync-deploy.tar.gz"

if [ "$reinstall_deps" -eq 1 ]; then
    echo "重装依赖…"
    ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
        "cd '$REMOTE_DIR' && .venv-web/bin/python -m pip install -q -r requirements-web.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
fi

if [ "$run_tests" -eq 1 ]; then
    echo "在服务器上运行测试…"
    ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
        "cd '$REMOTE_DIR' && SVN_SYNC_WEB_USER_STORE=/tmp/lzr-deploy-test-users.json .venv-web/bin/python -m unittest discover -s tests 2>&1 | tail -5"
    exit 0
fi

echo "重启 ${SERVICE}…"
ssh "${SSH_OPTS[@]}" "$SSH_HOST" "systemctl restart '$SERVICE' && sleep 2 && systemctl is-active '$SERVICE'"
echo "完成。日志：ssh $SSH_HOST journalctl -u $SERVICE -f"
