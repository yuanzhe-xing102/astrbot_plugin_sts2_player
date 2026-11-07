# -*- coding: utf-8 -*-
"""让「插件本体」直接打真游戏（STS2-Agent 后端）。

和 live_agent.py 的区别：那个是独立的极简脚本，这个走的是真正的插件代码路径
（Sts2Client 后端识别 → agent_to_legacy 状态压缩 → RuleBot → /sts2 的 _auto 循环），
所以它验证的是「用户装好插件、发一条 /sts2 自动」到底能不能打。

用法（AstrBot 实例的 venv，且 PYTHONPATH 指到 core）：
    python tools/live_plugin.py --steps 30 --verbose
    python tools/live_plugin.py --steps 200 --interval 0.8 --use-llm
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT
CORE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances"
            r"\2f597663-e149-4d1f-8644-244dcd5d4e6c\core")
sys.path.insert(0, str(CORE))


def load_plugin():
    spec = importlib.util.spec_from_file_location("sts2_plugin_main", PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sts2_plugin_main"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeProvider:
    def __init__(self, text: str = ""):
        self.text = text
        self.calls = 0

    async def text_chat(self, prompt: str, system_prompt: str = "", **kw):
        self.calls += 1
        return SimpleNamespace(completion_text=self.text)


class FakeContext:
    def __init__(self, provider: FakeProvider):
        self.provider = provider

    def get_using_provider(self, umo: str | None = None):
        return self.provider


class FakeEvent:
    def __init__(self, umo: str = "live:umo"):
        self.unified_msg_origin = umo
        self.sent: list[str] = []

    def plain_result(self, text: str):
        self.sent.append(text)
        return text

    def get_sender_id(self) -> str:
        return "10001"


async def run(args) -> int:
    mod = load_plugin()
    provider = FakeProvider()
    cfg = {
        "sts2_url": args.api,
        "sts2_timeout_sec": 40,
        "operator_ids": [],
        "max_steps": max(args.steps, 40),
        "step_interval_sec": args.interval,
        "report_every": args.report_every,
        "use_llm": args.use_llm,
        "llm_combat": False,
        "potion_hp_ratio": args.potion,
        "block_priority": args.bp,
        "card_min": args.pick,
    }
    plugin = mod.Sts2PlayerPlugin(FakeContext(provider), cfg)
    event = FakeEvent()

    mode = await plugin.client.detect()
    ok = await plugin.client.health()
    print(f"后端：{mode}　健康：{ok}")
    if not ok:
        print("游戏接口没起来，先启动游戏（mod 会在游戏里自动开接口）。")
        return 2

    st = await plugin.client.state()
    print(f"起始界面：{st.get('state_type')} / screen={st.get('screen')}")

    if args.start_run:
        for _ in range(20):
            st = await plugin.client.state()
            if st.get("state_type") not in ("menu", "game_over", "overlay"):
                break
            scr = str(st.get("screen") or "")
            avail = st.get("available_actions") or []
            act = None
            if scr == "GAME_OVER":
                act = ("return_to_main_menu", {}) if "return_to_main_menu" in avail else (
                    "continue_game_over", {})
            elif scr == "TIMELINE":
                act = ("confirm_timeline_overlay", {})
            elif scr == "MAIN_MENU":
                if "continue_run" in avail:
                    act = ("continue_run", {})
                elif "open_character_select" in avail:
                    act = ("open_character_select", {})
            elif scr == "CHARACTER_SELECT":
                if "embark" in avail:
                    act = ("embark", {})
                elif "select_character" in avail:
                    act = ("select_character", {"option_index": 0})
            if act is None:
                break
            res = await plugin.client.act(act[0], **act[1])
            print(f"  {act[0]} → {res.get('status')} {res.get('message') or res.get('error') or ''}")
            await asyncio.sleep(1.2)

    steps = 0
    if args.verbose:
        orig_act = plugin.client.act

        async def traced(action, **params):
            res = await orig_act(action, **params)
            print(f"    · {action} {params} → {res.get('status')} "
                  f"{res.get('message') or res.get('error') or ''}")
            return res

        plugin.client.act = traced  # type: ignore[method-assign]

    if args.dump_rest:
        # 抓一次真机营火的原始 payload，用来核对适配层字段（只写第一次）
        orig_state = plugin.client.state
        dumped = {"done": False}

        async def state_and_dump(fmt: str = "json"):
            st = await orig_state(fmt)
            if not dumped["done"] and st.get("state_type") == "rest_site":
                raw = getattr(plugin.client, "last_raw", None)
                if raw:
                    path = Path("rest_raw.json")
                    path.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(f"    · 已把真机营火原始状态写到 {path}")
                    dumped["done"] = True
            return st

        plugin.client.state = state_and_dump  # type: ignore[method-assign]

    async for msg in plugin._auto(event, args.steps):
        steps += 1
        print(f"\n[{steps}] {msg}")

    await plugin.client.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:8080")
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--interval", type=float, default=0.6)
    p.add_argument("--report-every", type=int, default=5)
    p.add_argument("--potion", type=float, default=0.32)
    p.add_argument("--bp", type=float, default=0.15)
    p.add_argument("--pick", type=float, default=18.0)
    p.add_argument("--use-llm", action="store_true")
    p.add_argument("--start-run", action="store_true", help="在主菜单时自动开一局")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dump-rest", action="store_true", help="遇到营火时把原生状态写到 rest_raw.json")
    args = p.parse_args()

    try:
        return asyncio.run(run(args))
    finally:
        stray = Path.cwd() / "data"
        if stray.exists():
            shutil.rmtree(stray, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
