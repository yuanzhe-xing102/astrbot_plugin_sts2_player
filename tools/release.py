"""把 dist/ 里的 zip 传到 GitHub Release。

用法（token 走环境变量，别写进文件）：
    $env:GITHUB_TOKEN = "ghp_xxx"
    python tools/release.py --notes "修了多选选牌卡死"

会做三件事：
1. 从 metadata.yaml 读 version，找 dist/astrbot_plugin_sts2_player-<version>.zip
2. 建（或复用）tag 为 <version> 的 release
3. 把 zip 作为 asset 传上去，打印下载地址

网络说明：本机 Windows 的 schannel 是坏的，所以这里用 urllib + 不校验证书；
GitHub 的 release 上传域名偶发断连，失败会重试 3 次。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
CTX = ssl._create_unverified_context()


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GIT_TOKEN") or ""
    if not tok:
        sys.exit("请先设置环境变量 GITHUB_TOKEN（或 GIT_TOKEN）")
    return tok


def _meta() -> dict:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def _slug(repo: str) -> str:
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", repo)
    if not m:
        sys.exit(f"repo 不是 GitHub 地址：{repo}")
    return m.group(1)


def _req(method: str, url: str, token: str, data: bytes | None = None,
         ctype: str = "application/json") -> dict:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "astrbot-plugin-sts2-release")
    with urllib.request.urlopen(req, timeout=120, context=CTX) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", default="", help="release 说明（中文即可）")
    ap.add_argument("--tag", default="", help="默认用 metadata 里的 version")
    args = ap.parse_args()

    token = _token()
    meta = _meta()
    tag = args.tag or meta.get("version") or ""
    if not tag:
        sys.exit("metadata.yaml 里没有 version")
    slug = _slug(meta.get("repo") or "")
    zip_path = DIST / f"astrbot_plugin_sts2_player-{tag}.zip"
    if not zip_path.exists():
        sys.exit(f"找不到 {zip_path}，先跑 python tools/pack.py")

    body = args.notes.strip() or f"{meta.get('display_name', '')} {tag}"
    try:
        rel = _req("POST", f"{API}/repos/{slug}/releases", token, json.dumps({
            "tag_name": tag,
            "name": tag,
            "body": body,
        }).encode())
        print(f"新建 release：{rel.get('html_url')}")
    except urllib.error.HTTPError as e:
        if e.code != 422:  # 422 = 同 tag 已存在，复用
            raise
        rel = _req("GET", f"{API}/repos/{slug}/releases/tags/{tag}", token)
        print(f"复用已有 release：{rel.get('html_url')}")

    rel_id = rel["id"]
    data = zip_path.read_bytes()
    url = f"{UPLOADS}/repos/{slug}/releases/{rel_id}/assets?name={zip_path.name}"
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            asset = _req("POST", url, token, data, ctype="application/zip")
            print(f"上传成功：{asset.get('browser_download_url')}")
            print(f"大小：{asset.get('size')} 字节")
            return
        except Exception as e:  # noqa: BLE001 网络抖动就重试
            last = e
            print(f"第 {attempt} 次上传失败：{e}")
            time.sleep(3 * attempt)
    sys.exit(f"上传失败：{last}")


if __name__ == "__main__":
    main()
