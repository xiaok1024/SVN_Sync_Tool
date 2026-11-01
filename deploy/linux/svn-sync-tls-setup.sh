#!/usr/bin/env bash
# 为 SVN Sync Web 生成私有 CA 与按 IP 签发的服务器证书。
#
# 为什么用私有 CA 而不是裸自签证书：
#   同事把 ca.crt 装进系统信任一次，之后任何浏览器都不再警告；裸自签证书每人
#   每台机器都要点过警告，Chrome 对 IP 地址的自签证书还会拒绝记住例外。
#   服务器证书可独立续期，续期后同事不需要重新安装任何东西。
#
# 关键约束（Apple 与 Chrome 都会检查，不满足就直接判不可信）：
#   - 服务器证书有效期不超过 825 天
#   - 必须带 subjectAltName，且访问用的 IP 必须在里面（CN 早已不被采信）
#   - SHA-256、RSA ≥ 2048
#
# 用法：
#   svn-sync-tls-setup.sh                 首次生成 CA 与服务器证书（已存在则跳过）
#   svn-sync-tls-setup.sh --renew         仅重新签发服务器证书（CA 不变，同事无需重装）
#   SVN_SYNC_TLS_IP=192.168.30.178 SVN_SYNC_TLS_DNS=lzr-ubuntu 可覆盖默认值

set -euo pipefail

TLS_DIR="${SVN_SYNC_TLS_DIR:-/etc/svn-sync-tool/tls}"
IP_ADDR="${SVN_SYNC_TLS_IP:-192.168.30.178}"
DNS_NAME="${SVN_SYNC_TLS_DNS:-lzr-ubuntu}"
CA_DAYS=3650
SERVER_DAYS=825

renew=0
[ "${1:-}" = "--renew" ] && renew=1

umask 077
mkdir -p "$TLS_DIR"
cd "$TLS_DIR"

if [ ! -f ca.key ] || [ ! -f ca.crt ]; then
    echo "生成私有 CA（${CA_DAYS} 天）…"
    openssl genrsa -out ca.key 4096 2>/dev/null
    openssl req -x509 -new -key ca.key -sha256 -days "$CA_DAYS" \
        -subj "/CN=LZR SVN Sync Local CA/O=LZR" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -out ca.crt
    chmod 600 ca.key
    chmod 644 ca.crt
else
    echo "CA 已存在，复用 $TLS_DIR/ca.crt"
fi

if [ -f server.crt ] && [ "$renew" -eq 0 ]; then
    echo "服务器证书已存在；如需续期请加 --renew"
else
    echo "签发服务器证书（${SERVER_DAYS} 天）：IP=${IP_ADDR} DNS=${DNS_NAME}"
    openssl genrsa -out server.key 2048 2>/dev/null
    openssl req -new -key server.key -subj "/CN=${IP_ADDR}/O=LZR" -out server.csr
    ext_file="$(mktemp)"
    cat > "$ext_file" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:${IP_ADDR},DNS:${DNS_NAME},DNS:localhost,IP:127.0.0.1
EOF
    openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days "$SERVER_DAYS" -sha256 -extfile "$ext_file" -out server.crt 2>/dev/null
    rm -f "$ext_file" server.csr
    chmod 600 server.key
    chmod 644 server.crt
fi

echo
echo "证书目录: $TLS_DIR"
echo "CA 指纹 (SHA-256):"
openssl x509 -in ca.crt -noout -fingerprint -sha256 | sed 's/^/  /'
echo "服务器证书:"
openssl x509 -in server.crt -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/  /'
echo
echo "校验签名链:"
openssl verify -CAfile ca.crt server.crt | sed 's/^/  /'
