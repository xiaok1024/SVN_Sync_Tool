#!/usr/bin/env bash
# 把当前工作区的 Web 服务同步到 Linux 主机并重启。
#
# 只推送源码（约 200 KB），不含 .git、venv、outputs 下的 exe/app.zip——
# 服务器用不到那些，而且仓库历史里的二进制会让 git clone 极慢且易断。
# 用 rsync --delete：本地删掉的模块在服务器上也会删掉，不留僵尸 .py；
# 排除项在接收端同样受保护（.venv-web 等不会被 --delete 清掉）。
#
# 用法：
#   ./deploy-linux.sh --from-git   # 正式发布：让服务器从 GitHub main 拉取、测试、重启（推荐）
#   ./deploy-linux.sh              # 开发中快速验证：把本地工作区（含未提交改动）rsync 过去并重启
#   ./deploy-linux.sh --test       # 同步 + 在服务器上跑测试（不重启服务）
#   ./deploy-linux.sh --deps       # 同步 + 重装依赖 + 重启（改了 requirements-web.txt 时用）
#
# 同事发布：拿到 svn-deploy 账号的授权后，设置 SVN_SYNC_DEPLOY_HOST=svn-deploy@192.168.30.178
# 再执行 --from-git；服务器上只会跑发布脚本，其他模式对该账号不可用。

set -euo pipefail

SSH_HOST="${SVN_SYNC_DEPLOY_HOST:-ubuntu-root}"
REMOTE_DIR="${SVN_SYNC_DEPLOY_DIR:-/opt/svn-sync-tool}"
SERVICE="${SVN_SYNC_DEPLOY_SERVICE:-svn-sync-web}"
SSH_OPTS=(-o ConnectTimeout=20 -o ClearAllForwardings=yes)

run_tests=0
reinstall_deps=0
from_git=0
for arg in "$@"; do
    case "$arg" in
        --test) run_tests=1 ;;
        --deps) reinstall_deps=1 ;;
        --from-git) from_git=1 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")"

if [ "$from_git" -eq 1 ]; then
    # 服务器自己从 GitHub 取 main：线上永远等于 main 上的某个提交。
    # 对 svn-deploy 账号，sshd 会忽略这里的命令改跑强制命令，效果相同。
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        echo "提示：本地有未提交的改动，--from-git 发布的是 GitHub 上的 main，不含这些改动。" >&2
    fi
    exec ssh "${SSH_OPTS[@]}" "$SSH_HOST" "svn-sync-deploy --by=$(id -un | tr -cd 'A-Za-z0-9._-')"
fi

EXCLUDES=(
    --exclude='/.git' --exclude='/.venv*' --exclude='/dist' --exclude='/build'
    --exclude='/outputs' --exclude='/.idea' --exclude='__pycache__' --exclude='*.pyc'
    --exclude='.DS_Store' --exclude='._*' --exclude='/README.assets' --exclude='/qt_assets'
)
echo "同步源码 → ${SSH_HOST}:${REMOTE_DIR}"
ssh "${SSH_OPTS[@]}" "$SSH_HOST" "mkdir -p '$REMOTE_DIR'"
rsync -a --delete "${EXCLUDES[@]}" -e "ssh ${SSH_OPTS[*]}" ./ "${SSH_HOST}:${REMOTE_DIR}/"

if [ "$reinstall_deps" -eq 1 ]; then
    echo "重装依赖…"
    ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
        "cd '$REMOTE_DIR' && .venv-web/bin/python -m pip install -q -r requirements-web.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
fi

if [ "$run_tests" -eq 1 ]; then
    echo "在服务器上运行测试…"
    # pipefail：unittest 失败时不能被 tail 的退出码掩盖成"成功"
    ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
        "set -o pipefail; cd '$REMOTE_DIR' && .venv-web/bin/python -m unittest discover -s tests 2>&1 | tail -5"
    exit 0
fi

echo "重启 ${SERVICE}…"
ssh "${SSH_OPTS[@]}" "$SSH_HOST" "systemctl restart '$SERVICE' && sleep 2 && systemctl is-active '$SERVICE'"
echo "完成。日志：ssh $SSH_HOST journalctl -u $SERVICE -f"
