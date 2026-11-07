"""验证「已装进 AstrBot 实例」的那份插件能不能真的跑起来。

它 import 的是实例目录里的 main.py（不是工作区那份），并且直接调用
`/sts2 状态` 的处理函数——所以跑通就等于：AstrBot 加载的代码路径 + 真游戏接口都正常。

用法：
    $py tools/check_installed.py            # 只读，看状态
    $py tools/check_installed.py --act 3    # 再真打 3 个决策（会动游戏）
    $py tools/check_installed.py --zip dist/astrbot_plugin_sts2_player-v0.4.1.zip
                                            # 先把安装包解到临时目录，再按上面那样验一遍
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace


def _log(msg: str) -> None:
    print(msg, flush=True)


INSTANCE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances"
                r"\2f597663-e149-4d1f-8644-244dcd5d4e6c")
PLUGIN = INSTANCE / "data" / "plugins" / "astrbot_plugin_sts2_player"
CORE = INSTANCE / "core"
CONFIG = INSTANCE / "data" / "config" / "astrbot_plugin_sts2_player_config.json"

sys.path.insert(0, str(CORE))


def load_installed(plugin_dir: Path | None = None):
    target = (plugin_dir or PLUGIN) / "main.py"
    spec = importlib.util.spec_from_file_location("installed_sts2_main", target)
    assert spec and spec.loader, f"找不到 {target}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["installed_sts2_main"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeProvider:
    async def text_chat(self, prompt: str, system_prompt: str = "", **kw):
        return SimpleNamespace(completion_text="")


class FakeContext:
    def get_using_provider(self, umo: str | None = None):
        return FakeProvider()


class FakeEvent:
    def __init__(self, umo: str = "check:umo"):
        self.unified_msg_origin = umo
        self.sent: list[str] = []

    def plain_result(self, text: str):
        self.sent.append(text)
        return text

    def get_sender_id(self) -> str:
        return "check"


async def main() -> int:
    args = parse_args()
    tmp_dir: Path | None = None
    if args.zip:
        # 把安装包解到临时目录，模拟 AstrBot「从文件安装」之后的目录结构
        # （用工作区里的临时目录，不用系统 TEMP——沙箱可能拦）
        tmp_dir = Path(__file__).resolve().parent.parent / ".pack_tmp"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip) as z:
            z.extractall(tmp_dir)
        candidates = list(tmp_dir.glob("*/main.py"))
        if not candidates:
            _log(f"✗ 安装包里没有 main.py：{args.zip}")
            return 1
        plugin_dir = candidates[0].parent
        _log(f"安装包：{args.zip}")
    else:
        plugin_dir = PLUGIN

    _log(f"插件目录：{plugin_dir}")
    if not (plugin_dir / "main.py").exists():
        _log("✗ 实例里没有 main.py，插件没装上")
        return 1
    cfg = {}
    if CONFIG.exists():
        # AstrBot 自己写配置时带 BOM，读的时候要容忍
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    _log(f"配置：sts2_url={cfg.get('sts2_url')} block_priority={cfg.get('block_priority')}")

    mod = load_installed(plugin_dir)
    _log(f"已加载插件模块：{mod.__name__}")

    plugin = mod.Sts2PlayerPlugin(FakeContext(), cfg)
    event = FakeEvent()

    _log("—— 执行 /sts2 状态 ——")
    async for chunk in plugin.sts2_cmd(event, ""):
        _log(str(chunk))

    _log("—— 执行 /sts2 帮助 ——")
    async for chunk in plugin.sts2_cmd(event, "帮助"):
        _log(str(chunk))

    health = await plugin.client.health()
    _log(f"后端：{await plugin.client.detect()} 健康：{health}")

    if args.act > 0:
        _log(f"—— 真打 {args.act} 个决策 ——")
        async for chunk in plugin._auto(event, args.act):
            _log(str(chunk))

    await plugin.terminate()
    if tmp_dir is not None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _log("✅ 实例内的插件可以正常工作" if args.zip is None else "✅ 安装包解开后可以正常工作")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--act", type=int, default=0, help="真打几个决策（默认 0，只读）")
    p.add_argument("--zip", default=None, help="先解开这个安装包再验（而不是验实例里那份）")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
