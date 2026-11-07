# -*- coding: utf-8 -*-
"""真机联调：用插件里的 RuleBot 直接驱动正在运行的《杀戮尖塔 2》。

和 bench.py 的区别：bench 驱动内存里的 mock，这个走 HTTP 打真的游戏
（需要游戏已启动、mods/ 里有 STS2_MCP.dll、Settings → Mods 已启用）。

运行：
    python tools/live_play.py                 # 默认打到 60 步或对局结束
    python tools/live_play.py --steps 200
    python tools/live_play.py --log live_run_1.txt
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

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


def sig_of(st: dict) -> str:
    keys = ("state_type", "player", "battle", "map", "rewards", "card_reward",
            "event", "rest_site", "shop", "treasure", "card_select", "relic_select",
            "crystal_sphere", "menu_screen")
    raw = repr([(k, st.get(k)) for k in keys])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


async def run(args) -> int:
    mod = load_plugin()
    client = mod.Sts2Client(args.url, timeout=args.timeout)
    bot = mod.RuleBot(
        potion_hp_ratio=args.potion,
        block_priority=args.bp,
        card_min=args.pick,
    )
    log_lines: list[str] = []

    def say(text: str) -> None:
        print(text)
        log_lines.append(text)

    steps = 0
    errors = 0
    last_sig, repeat = "", 0
    try:
        while steps < args.steps:
            st = await client.state()
            kind = st.get("state_type")

            if kind == "game_over":
                over = st.get("game_over") or {}
                say(f"【对局结束】{over.get('message') or over}")
                break
            if kind == "menu":
                say(f"【菜单】{st.get('menu_screen')}：{st.get('message')}（需要人工操作）")
                break
            if kind == "overlay":
                say(f"【覆盖层】{st.get('message')}（mod 的软锁保护，停止）")
                break

            battle = st.get("battle") or {}
            if battle.get("turn") == "enemy" or not battle.get("is_play_phase", True):
                # 敌人回合：等它自己走完，不消耗步数
                await asyncio.sleep(args.interval)
                continue

            cur = sig_of(st)
            if cur == last_sig:
                repeat += 1
                if repeat >= 5:
                    say(f"⚠ 状态连续 {repeat} 次没有变化，停止（可能卡在 {kind}）")
                    break
            else:
                repeat = 0
            last_sig = cur

            action = bot.next_action(st)
            if not action:
                say(f"⚠ 规则层不知道该怎么走（{kind}），停止")
                break

            name = action["action"]
            params = {k: v for k, v in action.items() if k != "action"}
            res = await client.act(name, **params)
            steps += 1

            status = res.get("status")
            tail = res.get("message") or res.get("error") or ""
            if status == "error":
                errors += 1
                say(f"{steps:>3}. {kind:<11} ✗ {name} {params} → {tail}")
                if errors >= args.max_errors:
                    say(f"⚠ 连续/累计 {errors} 次错误，停止")
                    break
                await asyncio.sleep(args.interval)
                continue
            errors = 0
            say(f"{steps:>3}. {kind:<11} → {name} {params or ''}  {str(tail)[:60]}")

            if args.verbose:
                for line in mod.render_state(st).splitlines():
                    say("      " + line)
            await asyncio.sleep(args.interval)

        st = await client.state()
        p = st.get("player") or {}
        say(f"【收尾】{st.get('state_type')} 第{st.get('run', {}).get('floor')}层 "
            f"HP {p.get('hp')}/{p.get('max_hp')} 金币 {p.get('gold')} 共 {steps} 步")
    except mod.Sts2Error as exc:
        say(f"✗ {exc}")
        return 2
    finally:
        await client.close()
        if args.log:
            Path(args.log).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            print(f"（日志写入 {args.log}）")

    # import astrbot 会在 cwd 造一个 data/，清掉
    stray = Path.cwd() / "data"
    if not args.keep_data and stray.exists():
        shutil.rmtree(stray, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:15526")
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--interval", type=float, default=0.45, help="每步等待秒数")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--max-errors", type=int, default=6)
    ap.add_argument("--bp", type=float, default=0.15)
    ap.add_argument("--potion", type=float, default=0.32)
    ap.add_argument("--pick", type=float, default=18.0)
    ap.add_argument("--log", default="")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--keep-data", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
