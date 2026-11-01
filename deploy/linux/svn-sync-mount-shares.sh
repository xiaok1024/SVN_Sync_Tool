#!/usr/bin/env bash
# 挂载 E9 的标准文件与历史文件 SMB 共享，供 Web 服务以本地路径方式只读访问。
#
# 为什么需要这个脚本：
#   svn_sync_core 的自动挂载只实现了 macOS（mount_smbfs）与 Windows（UNC 直连）。
#   Linux 上预先挂好、让 SVN_SYNC_WEB_MOUNT_ROOT 指向挂载根，服务就命中
#   「本地路径存在」的分支，完全不会走那段平台代码。
#
# 挂载点规则必须与 SourceProfile.local_root_for 一致：<挂载根>/<主机>/<共享>
#
# 凭据：TOML，按角色分 [standard] / [history] 两节（三台历史主机共用一套账号）。
#   mount.cifs 要的是 username=/password= 纯文本格式，与 TOML 不通用；
#   这里临时生成、挂载后立即删除，且走 credentials= 文件形式——
#   不用 username=x,password=y，那会进 /proc/mounts 和 ps。
#
# 用法：
#   svn-sync-mount-shares.sh            挂载全部
#   svn-sync-mount-shares.sh --umount   卸载全部
#   svn-sync-mount-shares.sh --status   只看当前状态

set -uo pipefail

CRED_TOML="${SVN_SYNC_SMB_CREDENTIALS:-/root/.config/svn_sync_tool/smb-credentials.toml}"
MOUNT_ROOT="${SVN_SYNC_WEB_MOUNT_ROOT:-/mnt/ecology}"

# 角色 主机 共享名
SHARES=(
    "standard 192.168.7.215 ECOLOGY_customer"
    "history  192.168.7.108 ECOLOGY_customer"
    "history  192.168.7.173 ecology-customer"
    "history  192.168.7.173 ecology-customer2"
    # 192.168.7.106（客户升级记录*，共享名含中文、需 vers=2.1）暂不支持，
    # 如需启用：这里补回条目，同时把共享加进 DEFAULT_HISTORICAL_UNC_PREFIXES
)

mount_point_for() { printf '%s/%s/%s' "$MOUNT_ROOT" "$1" "$2"; }

if [ "${1:-}" = "--status" ]; then
    for entry in "${SHARES[@]}"; do
        read -r _role server share <<<"$entry"
        point="$(mount_point_for "$server" "$share")"
        if mountpoint -q "$point"; then echo "  已挂载  $point"; else echo "  未挂载  $point"; fi
    done
    exit 0
fi

if [ "${1:-}" = "--umount" ]; then
    for entry in "${SHARES[@]}"; do
        read -r _role server share <<<"$entry"
        point="$(mount_point_for "$server" "$share")"
        if mountpoint -q "$point"; then
            umount "$point" && echo "已卸载 $point"
        fi
    done
    exit 0
fi

[ -f "$CRED_TOML" ] || { echo "凭据文件不存在: $CRED_TOML" >&2; exit 1; }

# Ubuntu 22.04 是 Python 3.10，tomllib 要 3.11，走服务自带 venv 里的 tomli
PYBIN=/opt/svn-sync-tool/.venv-web/bin/python
[ -x "$PYBIN" ] || PYBIN=python3

# 每个角色的凭据只生成一次，减少明文落盘的次数
declare -A CRED_FILES=()
cleanup() { for f in "${CRED_FILES[@]}"; do rm -f "$f"; done; }
trap cleanup EXIT

credentials_for() {
    local role="$1"
    if [ -n "${CRED_FILES[$role]:-}" ]; then printf '%s' "${CRED_FILES[$role]}"; return 0; fi
    local tmp
    tmp="$(mktemp /run/svn-sync-cifs.XXXXXX)" || return 1
    chmod 600 "$tmp"
    if ! "$PYBIN" - "$CRED_TOML" "$role" > "$tmp" 2>/tmp/svn-sync-cred.err <<'PYEOF'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
# 历史共享兼容 [history] / [historical] 两种节名
names = [sys.argv[2]] + (["historical"] if sys.argv[2] == "history" else [])
section = next((data[n] for n in names if isinstance(data.get(n), dict)), None)
if section is None:
    sys.exit("凭据文件缺少 [%s] 节" % sys.argv[2])
user = str(section.get("username") or "").strip()
password = str(section.get("password") or "").strip()
if not user or not password or password == "REPLACE_ME":
    sys.exit("[%s] 的账号或密码未填写" % sys.argv[2])
print("username=%s" % user)
print("password=%s" % password)
PYEOF
    then
        echo "读取 [$role] 凭据失败: $(cat /tmp/svn-sync-cred.err)" >&2
        rm -f "$tmp" /tmp/svn-sync-cred.err
        return 1
    fi
    rm -f /tmp/svn-sync-cred.err
    CRED_FILES[$role]="$tmp"
    printf '%s' "$tmp"
}

failed=0
for entry in "${SHARES[@]}"; do
    read -r role server share <<<"$entry"
    point="$(mount_point_for "$server" "$share")"

    if mountpoint -q "$point"; then
        echo "已挂载，跳过 $point"
        continue
    fi
    mkdir -p "$point"

    cred="$(credentials_for "$role")" || { failed=1; continue; }

    # 先试 3.0，失败降 2.1（173 是 Samba 4.10.16，106/108 是 Windows）；
    # iocharset=utf8 对中文共享名和中文目录是必须的；ro 只读。
    mounted=0
    for vers in 3.0 2.1; do
        if mount -t cifs "//${server}/${share}" "$point" \
            -o "credentials=${cred},ro,iocharset=utf8,uid=0,gid=0,file_mode=0440,dir_mode=0550,vers=${vers}" \
            2>/tmp/svn-sync-mount.err
        then
            echo "已挂载 //${server}/${share} → $point (vers=${vers})"
            mounted=1
            break
        fi
    done
    if [ "$mounted" -eq 0 ]; then
        echo "挂载失败 //${server}/${share}: $(tail -1 /tmp/svn-sync-mount.err)" >&2
        failed=1
    fi
    rm -f /tmp/svn-sync-mount.err
done

exit "$failed"
