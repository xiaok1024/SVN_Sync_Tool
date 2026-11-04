"""通过 Gitee OpenAPI 创建仓库（若不存在）。

用法：
    python gitee_create_repo.py <access_token> [owner] [repo]

退出码：0=仓库可用（已存在或创建成功），1=失败。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://gitee.com/api/v5"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url, data=None, method=None):
    headers = {"User-Agent": "svn-sync-push", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    return _opener().open(req, timeout=40)


def repo_exists(owner, repo):
    try:
        with _req(f"{API}/repos/{owner}/{repo}") as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (401, 403):
            print(f"[WARN] 查询仓库返回 {e.code}，按不存在处理", flush=True)
            return None
        raise


def create_repo(token, repo, private=True, description=""):
    payload = urllib.parse.urlencode({
        "access_token": token,
        "name": repo,
        "private": "true" if private else "false",
        "auto_init": "false",
        "description": description,
    }).encode("utf-8")
    try:
        with _req(f"{API}/user/repos", data=payload, method="POST") as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        print(f"[FAIL] 创建仓库失败 HTTP {e.code}", flush=True)
        print(f"       响应: {body}", flush=True)
        return None
    except Exception as e:
        print(f"[FAIL] 创建仓库异常: {type(e).__name__}: {e}", flush=True)
        return None


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法: python gitee_create_repo.py <access_token> [owner] [repo]")
        return 1

    token = sys.argv[1].strip()
    owner = (sys.argv[2] if len(sys.argv) > 2 else "xiaok1024").strip()
    repo = (sys.argv[3] if len(sys.argv) > 3 else "SVN_Sync_Tool").strip()

    print(f"目标仓库: {owner}/{repo}", flush=True)

    existing = repo_exists(owner, repo)
    if existing:
        print(f"[OK] 仓库已存在：{existing.get('full_name')} "
              f"(private={existing.get('private')})", flush=True)
        print("EXISTS", flush=True)
        return 0

    print("[..] 仓库不存在，尝试创建（私有）...", flush=True)
    created = create_repo(
        token, repo, private=True, description="SVN 代码同步工具 (Windows)")
    if not created:
        return 1

    print(f"[OK] 创建成功：{created.get('full_name')} "
          f"(private={created.get('private')})", flush=True)
    print(f"     {created.get('html_url')}", flush=True)
    print("CREATED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
