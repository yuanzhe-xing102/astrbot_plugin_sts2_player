# -*- coding: utf-8 -*-
"""让「模型」通过插件本体来打游戏（真 LLM，不是假 provider）。

和 live_plugin.py 的区别：那个的 FakeProvider 永远返回空串，所以只有规则层在工作。
这个把 text_chat 真的接到 DeepSeek chat/completions，于是走的是插件里真正的
`_ask_llm` → `render_state` → `LLM_SYSTEM` → `parse_llm_json` 通路，
可以实测「模型介入操作」到底能推进到第几层、花多少钱。

用法：
    python tools/live_llm.py --steps 60                  # 只让模型管非战斗决策
    python tools/live_llm.py --steps 60 --llm-combat     # 战斗也交给模型
    python tools/live_llm.py --steps 40 --mock 15999     # 对着 mock_sts2 跑，不占真游戏

密钥默认从 AstrBot 实例的 core/data/cmd_config.json 里取（provider_sources 的 deepseek 那条），
也可以用环境变量 STS2_LLM_KEY / STS2_LLM_BASE / STS2_LLM_MODEL 覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import ssl
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT
CORE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances"
            r"\2f597663-e149-4d1f-8644-244dcd5d4e6c\core")
CORE_CFG = CORE / "data" / "cmd_config.json"
sys.path.insert(0, str(CORE))


def load_plugin():
    spec = importlib.util.spec_from_file_location("sts2_plugin_main", PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sts2_plugin_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def read_llm_config() -> tuple[str, str, str]:
    """(key, base, model)，环境变量优先。"""
    key = os.environ.get("STS2_LLM_KEY", "")
    base = os.environ.get("STS2_LLM_BASE", "https://api.deepseek.com/v1")
    model = os.environ.get("STS2_LLM_MODEL", "deepseek-chat")
    if key:
        return key, base, model
    if CORE_CFG.exists():
        cfg = json.loads(CORE_CFG.read_text(encoding="utf-8-sig"))
        for src in cfg.get("provider_sources") or []:
            keys = src.get("key") or []
            if src.get("provider") == "deepseek" and keys:
                base = src.get("api_base") or base
                break
        for p in cfg.get("provider") or []:
            if str(p.get("id", "")).startswith("deepseek/"):
                model = p.get("model") or model
                break
        else:
            keys = []
        if keys:
            return str(keys[0]), str(base), str(model)
    raise SystemExit("找不到 API key：设置 STS2_LLM_KEY 环境变量，或确认 AstrBot 里配了 deepseek。")


class DeepSeekProvider:
    """只实现插件用到的那一个方法：text_chat(prompt=, system_prompt=)。"""

    def __init__(self, key: str, base: str, model: str, timeout: float = 120.0,
                 max_tokens: int = 700):
        self.key = key
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.errors = 0
        self.last_text = ""
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    async def text_chat(self, prompt: str, system_prompt: str = "", **kw):
        import aiohttp

        self.calls += 1
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": int(kw.get("max_tokens") or self.max_tokens),
            "stream": False,
        }
        if kw.get("thinking_disabled", True):
            payload["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}
        text = ""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            conn = aiohttp.TCPConnector(ssl=self._ssl)
            async with aiohttp.ClientSession(timeout=timeout, connector=conn) as sess:
                async with sess.post(
                    f"{self.base}/chat/completions", json=payload, headers=headers
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        self.errors += 1
                        print(f"    ! LLM HTTP {resp.status}: {body[:200]}")
                        return SimpleNamespace(completion_text="")
                    data = json.loads(body)
            usage = data.get("usage") or {}
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            text = msg.get("content") or ""
            if not text.strip():
                # 有些模型把全部输出放进 reasoning_content，content 为空
                text = msg.get("reasoning_content") or ""
            self.last_text = text
        except Exception as e:  # noqa: BLE001
            self.errors += 1
            print(f"    ! LLM 调用失败：{type(e).__name__}: {str(e)[:160]}")
        return SimpleNamespace(completion_text=text)

    def report(self) -> str:
        total = self.prompt_tokens + self.completion_tokens
        return (f"模型调用 {self.calls} 次（失败 {self.errors}）｜"
                f"tokens: 输入 {self.prompt_tokens} / 输出 {self.completion_tokens} / 合计 {total}")


class FakeContext:
    def __init__(self, provider):
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


async def start_run_if_needed(plugin, verbose: bool = True) -> None:
    """在主菜单/游戏结束/时间线这类界面里，自动点到能打的状态。"""
    for _ in range(20):
        st = await plugin.client.state()
        if st.get("state_type") not in ("menu", "game_over", "overlay"):
            return
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
            act = ("embark", {}) if "embark" in avail else (
                "select_character", {"option_index": 0})
        if act is None:
            return
        res = await plugin.client.act(act[0], **act[1])
        if verbose:
            print(f"  {act[0]} → {res.get('status')} "
                  f"{res.get('message') or res.get('error') or ''}")
        await asyncio.sleep(1.0)


async def run(args) -> int:
    mod = load_plugin()
    key, base, model = read_llm_config()
    provider = DeepSeekProvider(key, base, model)
    cfg = {
        "sts2_url": args.api,
        "sts2_timeout_sec": 40,
        "operator_ids": [],
        "max_steps": max(args.steps, 40),
        "step_interval_sec": args.interval,
        "report_every": args.report_every,
        "use_llm": True,
        "llm_combat": bool(args.llm_combat),
        "llm_combat_plan": bool(args.plan),
        "potion_hp_ratio": args.potion,
        "block_priority": args.bp,
        "card_min": args.pick,
        "auto_restart": bool(args.auto_restart),
    }
    plugin = mod.Sts2PlayerPlugin(FakeContext(provider), cfg)
    event = FakeEvent()

    mode = await plugin.client.detect()
    ok = await plugin.client.health()
    print(f"后端：{mode}　健康：{ok}　模型：{model}（战斗交模型：{bool(args.llm_combat)}）")
    if not ok:
        print("游戏接口没起来。")
        return 2

    st = await plugin.client.state()
    print(f"起始界面：{st.get('state_type')} / screen={st.get('screen')}")
    if args.start_run:
        await start_run_if_needed(plugin)

    if args.verbose:
        orig_act = plugin.client.act

        async def traced(action, **params):
            res = await orig_act(action, **params)
            think = ""
            if provider.last_text:
                try:
                    obj = json.loads(provider.last_text[provider.last_text.find("{"):
                                                      provider.last_text.rfind("}") + 1])
                    think = str(obj.get("think") or "")
                except Exception:  # noqa: BLE001
                    think = ""
            print(f"    · {action} {params} → {res.get('status')} "
                  f"{res.get('message') or res.get('error') or ''}"
                  + (f"   [模型：{think[:60]}]" if think else ""))
            return res

        plugin.client.act = traced  # type: ignore[method-assign]

    t0 = time.time()
    floor_seen: list[int] = []
    try:
        async for msg in plugin._auto(event, args.steps):
            text = str(msg)
            print(f"\n[{int(time.time() - t0)}s] {text}")
            for line in text.splitlines():
                if "第1幕 第" in line:
                    digits = "".join(ch for ch in line.split("第1幕 第")[1][:4] if ch.isdigit())
                    if digits:
                        floor_seen.append(int(digits))
    finally:
        print("\n" + provider.report())
        try:
            final = await plugin.client.state()
            run_info = final.get("run") or {}
            print(f"结束：{final.get('state_type')} / 第{run_info.get('floor')}层 / "
                  f"{run_info.get('current_hp')}/{run_info.get('max_hp')}HP / {run_info.get('gold')}金")
        except Exception as e:  # noqa: BLE001
            print(f"读结束状态失败：{e}")
        if floor_seen:
            print(f"最高到达：第 {max(floor_seen)} 层")
        await plugin.client.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:8080")
    p.add_argument("--steps", type=int, default=40)
    p.add_argument("--interval", type=float, default=0.4)
    p.add_argument("--report-every", type=int, default=5)
    p.add_argument("--potion", type=float, default=0.32)
    p.add_argument("--bp", type=float, default=0.15)
    p.add_argument("--pick", type=float, default=18.0)
    p.add_argument("--llm-combat", action="store_true", help="战斗动作也交给模型决定")
    p.add_argument("--plan", action="store_true", help="回合计划模式：每回合只问一次，模型给整串出牌顺序")
    p.add_argument("--start-run", action="store_true")
    p.add_argument("--no-auto-restart", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    args.auto_restart = not args.no_auto_restart

    try:
        return asyncio.run(run(args))
    finally:
        stray = Path.cwd() / "data"
        if stray.exists():
            shutil.rmtree(stray, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
