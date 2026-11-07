"""把插件打包成 AstrBot 能"从文件安装"的 zip，并做一次官方校验。

用法：
    python tools/pack.py                     # dist/astrbot_plugin_sts2_player-v0.4.1.zip
    python tools/pack.py --repo https://github.com/you/astrbot_plugin_sts2_player
    python tools/pack.py --version v0.4.2 --repo ...   # 打包时临时改版本号/仓库地址

校验用的是 AstrBot 自己的 _PluginUpdater.validate_plugin_archive（读实例里的 core），
所以"能打包"等于"能在 WebUI 里装"。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT
DIST = ROOT / "dist"
INSTANCE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances\2f597663-e149-4d1f-8644-244dcd5d4e6c")
CORE = INSTANCE / "core"

# 装进插件目录的东西（不含 __pycache__、测试、工具）
PAYLOAD = ("main.py", "metadata.yaml", "_conf_schema.json", "logo.png", "README.md")


def _read_version(meta_text: str) -> str:
    m = re.search(r"^version:\s*(\S+)", meta_text, re.M)
    return m.group(1) if m else "unknown"


def _patch_meta(meta_text: str, *, repo: str | None, version: str | None) -> str:
    if version:
        meta_text = re.sub(r"^version:.*$", f"version: {version}", meta_text, count=1, flags=re.M)
    if repo:
        line = f'repo: "{repo}"'
        if re.search(r"^repo:", meta_text, re.M):
            meta_text = re.sub(r"^repo:.*$", line, meta_text, count=1, flags=re.M)
        else:
            meta_text = meta_text.rstrip("\n") + "\n" + line + "\n"
    return meta_text


def build(*, repo: str | None, version: str | None) -> Path:
    missing = [n for n in PAYLOAD if not (PLUGIN_DIR / n).is_file()]
    if missing:
        raise SystemExit(f"插件目录缺文件：{', '.join(missing)}")

    meta_text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    meta_text = _patch_meta(meta_text, repo=repo, version=version)
    out_version = _read_version(meta_text)

    DIST.mkdir(exist_ok=True)
    out = DIST / f"astrbot_plugin_sts2_player-{out_version}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PAYLOAD:
            data = meta_text.encode("utf-8") if name == "metadata.yaml" else (PLUGIN_DIR / name).read_bytes()
            z.writestr(f"astrbot_plugin_sts2_player/{name}", data)
    return out


def validate(zip_path: Path) -> str:
    sys.path.insert(0, str(CORE))
    from astrbot.core.star.updater import _PluginUpdater  # noqa: PLC0415

    entry = _PluginUpdater.validate_plugin_archive(str(zip_path))
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 AstrBot 插件并校验")
    ap.add_argument("--repo", default=None, help="写进 metadata 的仓库地址（启用更新检查）")
    ap.add_argument("--version", default=None, help="打包时临时替换版本号，如 v0.4.2")
    args = ap.parse_args()

    out = build(repo=args.repo, version=args.version)
    entry = validate(out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()

    print(f"✅ 打包完成：{out}")
    print(f"   大小：{out.stat().st_size / 1024:.1f} KB")
    print(f"   sha256：{digest}")
    print(f"   校验通过：metadata 位于 {entry}")
    print("   安装：WebUI → 插件 → 从文件安装 → 选中这个 zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
