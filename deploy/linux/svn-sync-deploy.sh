#!/usr/bin/env bash
# 从 GitHub main 发布 SVN Sync Web 到本机（root 执行，装在 /usr/local/sbin/svn-sync-deploy）。
#
# 为什么只从 GitHub 发布：两个人共同维护仓库，线上必须等于 main 上的某个提交，
# 而不是某人工作区里未提交的状态；同事只需要能触发这个脚本，不需要登录主机。
#
# 流程：fetch main → 检出到 releases/<commit> → 在那里跑完整测试 → 同步到线上目录
#       → 重启服务 → 健康检查；检查失败自动回滚到上一版。
#
# 用法：
#   svn-sync-deploy [--by=名字] [--force]   发布 main 最新提交（已是最新则跳过）
#   svn-sync-deploy --rollback              回到上一个成功发布的版本
#
# 同事通过专用账号 svn-deploy 的强制命令触发（见 svn-sync-deploy-grant.sh），
# 除了看到本脚本的输出，在主机上做不了任何其他事。

set -euo pipefail
umask 022

STATE_ROOT="${SVN_SYNC_DEPLOY_STATE:-/var/lib/svn-sync-deploy}"
REPO_URL="${SVN_SYNC_DEPLOY_REPO:-git@github.com:xiaok1024/SVN_Sync_Tool.git}"
BRANCH="${SVN_SYNC_DEPLOY_BRANCH:-main}"
LIVE_DIR="${SVN_SYNC_DEPLOY_DIR:-/opt/svn-sync-tool}"
SERVICE="${SVN_SYNC_DEPLOY_SERVICE:-svn-sync-web}"
HEALTH_URL="${SVN_SYNC_DEPLOY_HEALTH:-https://127.0.0.1:8443/api/health}"
PIP_INDEX="${SVN_SYNC_DEPLOY_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
KEEP_RELEASES=5

REPO="$STATE_ROOT/repo"
RELEASES="$STATE_ROOT/releases"
PYBIN="$LIVE_DIR/.venv-web/bin/python"

by="${SUDO_USER:-$(id -un)}"
force=0
rollback=0
for arg in "$@"; do
    case "$arg" in
        --by=*) by="${arg#--by=}" ;;
        --force) force=1 ;;
        --rollback) rollback=1 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done
# 触发人只用于记录；限制字符，避免写进 JSON/日志时夹带别的东西
[[ "$by" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || by="unknown"

say() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "错误: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "必须以 root 运行"
[ -x "$PYBIN" ] || die "线上 venv 不存在: $PYBIN"
mkdir -p "$STATE_ROOT" "$RELEASES"
exec 9>"$STATE_ROOT/.lock"
flock -n 9 || die "另一个发布正在进行，请稍候再试"

current="$(cat "$STATE_ROOT/current" 2>/dev/null || true)"
previous="$(cat "$STATE_ROOT/previous" 2>/dev/null || true)"

wait_healthy() {
    local i
    for i in $(seq 1 30); do
        if curl -sk --max-time 2 "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

write_marker() {
    local sha="$1" release="$2"
    local subject
    subject="$(git -C "$REPO" log -1 --format=%s "$sha" 2>/dev/null || echo "")"
    SHA="$sha" SUBJECT="$subject" BY="$by" "$PYBIN" - "$release/.deployed.json" <<'PYEOF'
import json, os, sys, time
sha = os.environ["SHA"]
json.dump({
    "commit": sha,
    "short": sha[:7],
    "subject": os.environ["SUBJECT"][:120],
    "deployed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "by": os.environ["BY"],
}, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
}

# 把某个 release 目录切成线上：同步文件、重启、健康检查
activate() {
    local sha="$1" release="$RELEASES/$1"
    [ -d "$release" ] || die "release 不存在: $release"
    write_marker "$sha" "$release"
    rsync -a --delete \
        --exclude='/.venv-web' --exclude='__pycache__' --exclude='*.pyc' \
        "$release/" "$LIVE_DIR/"
    systemctl restart "$SERVICE"
    if wait_healthy; then
        say "健康检查通过：$SERVICE 已在 ${sha:0:7} 上运行"
        return 0
    fi
    say "健康检查失败（${sha:0:7}）"
    journalctl -u "$SERVICE" -n 15 --no-pager -o cat 2>/dev/null | sed 's/^/    /' || true
    return 1
}

if [ "$rollback" -eq 1 ]; then
    [ -n "$previous" ] || die "没有可回滚的上一版本"
    say "回滚：${current:0:7} → ${previous:0:7}"
    activate "$previous" || die "回滚后健康检查仍失败，请人工介入"
    printf '%s\n' "$previous" > "$STATE_ROOT/current"
    printf '%s\n' "$current" > "$STATE_ROOT/previous"
    say "回滚完成"
    exit 0
fi

# ── 1. 同步仓库（blobless + 跳过 outputs/，历史里的 exe/app.zip 一个字节都不拉）──
say "从 GitHub 获取 $BRANCH（触发人: $by）"
if [ ! -d "$REPO/.git" ]; then
    git clone -q --filter=blob:none --no-checkout "$REPO_URL" "$REPO"
fi
git -C "$REPO" config core.sparseCheckout true
printf '/*\n!/outputs/\n' > "$REPO/.git/info/sparse-checkout"
git -C "$REPO" fetch -q origin "$BRANCH"
sha="$(git -C "$REPO" rev-parse "origin/$BRANCH")"
short="${sha:0:7}"
say "main 最新: $short  $(git -C "$REPO" log -1 --format=%s "$sha")"

if [ "$sha" = "$current" ] && [ "$force" -eq 0 ]; then
    say "线上已经是这个版本，无需发布（加 --force 可强制重发）"
    exit 0
fi

# ── 2. 导出 release ──
release="$RELEASES/$sha"
git -C "$REPO" checkout -q --detach "$sha"
mkdir -p "$release"
rsync -a --delete --exclude='/.git' "$REPO/" "$release/"

# ── 3. 依赖变了才装 ──
req_hash="$(sha256sum "$release/requirements-web.txt" | cut -d' ' -f1)"
if [ "$req_hash" != "$(cat "$STATE_ROOT/requirements.sha256" 2>/dev/null || true)" ]; then
    say "requirements-web.txt 有变化，安装依赖…"
    "$PYBIN" -m pip install -q -r "$release/requirements-web.txt" -i "$PIP_INDEX"
    printf '%s\n' "$req_hash" > "$STATE_ROOT/requirements.sha256"
fi

# ── 4. 先在 release 目录里跑完整测试，不通过线上不动 ──
say "运行测试…"
if ! (cd "$release" && "$PYBIN" -m unittest discover -s tests 2>&1 | tail -4 | sed 's/^/    /'; exit "${PIPESTATUS[0]}"); then
    die "测试未通过，线上保持 ${current:0:7}，本次发布中止"
fi

# ── 5. 切换 + 健康检查，失败回滚 ──
say "发布 $short 到 $LIVE_DIR"
if activate "$sha"; then
    [ -n "$current" ] && printf '%s\n' "$current" > "$STATE_ROOT/previous"
    printf '%s\n' "$sha" > "$STATE_ROOT/current"
    # 只保留最近几个 release，当前与上一版永不删除
    ls -1t "$RELEASES" | tail -n +$((KEEP_RELEASES + 1)) | while read -r old; do
        [ "$old" = "$sha" ] || [ "$old" = "$current" ] || rm -rf "${RELEASES:?}/$old"
    done
    say "发布完成：$short（$by）"
    exit 0
fi

if [ -n "$current" ] && [ -d "$RELEASES/$current" ]; then
    say "自动回滚到 ${current:0:7}"
    activate "$current" || die "回滚后健康检查仍失败，请人工介入"
    die "新版本健康检查失败，已回滚到 ${current:0:7}"
fi
die "新版本健康检查失败，且没有可回滚的版本，请人工介入"
