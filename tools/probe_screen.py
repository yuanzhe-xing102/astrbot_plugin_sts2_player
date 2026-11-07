"""跑到指定界面就停下，把「原始 payload」和「映射后的 legacy state」都打印出来。

用来定位真机上某个界面（营火、商店、事件…）为什么决策不对 —— 之前的 bug 都是
「映射层把字段吃掉了」导致的，光看映射后的状态看不出来。

用法：
    python tools/probe_screen.py --screen REST --max-steps 120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTANCE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances\2f597663-e149-4d1f-8644-244dcd5d4e6c")
CORE = INSTANCE / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", default="REST", help="想停在哪个原生 screen（REST / SHOP / EVENT …）")
    ap.add_argument("--max-steps", type=int, default=120)
    ap.add_argument("--interval", type=float, default=0.6)
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    asyncio.run(_run(args))


async def _run(args) -> None:
    sys.path.insert(0, str(ROOT))
    import main as mod  # 仓库根目录就是插件目录，main.py 在根上

    client = mod.Sts2Client(args.api)
    bot = mod.RuleBot(potion_hp_ratio=0.32, block_priority=0.6, card_min=18)
    try:
        for i in range(1, args.max_steps + 1):
            raw = await client._json("GET", "/state")
            data = raw[1].get("data") or {}
            screen = str(data.get("screen") or "")
            if screen == args.screen:
                print(f"=== 命中 {args.screen}（第 {i} 步）===")
                print("--- 原生 rest/相关子结构 ---")
                for key in ("rest", "reward", "shop", "event", "selection", "chest", "combat"):
                    if data.get(key):
                        print(f"[{key}]")
                        print(json.dumps(data[key], ensure_ascii=False, indent=2)[:4000])
                print("--- 映射后的 legacy ---")
                st = mod.agent_to_legacy(data)
                print(json.dumps({k: st.get(k) for k in ("state_type", "rest_site")},
                                 ensure_ascii=False, indent=2)[:2000])
                print("--- 规则层决策 ---")
                print(bot.next_action(st))
                return
            st = mod.agent_to_legacy(data)
            act = bot.next_action(st)
            # 想尽快撞见目标界面时，地图上优先走同名节点（比如 RestSite）
            if act and act.get("action") == "choose_map_node":
                want = {"REST": ("Rest", "RestSite"), "SHOP": ("Shop",)}.get(args.screen, ())
                if want:
                    for node in (st.get("map") or {}).get("next_options") or []:
                        if any(w.lower() in str(node.get("type") or "").lower() for w in want):
                            act = {"action": "choose_map_node", "index": node.get("index")}
                            break
            if not act:
                print(f"第 {i} 步停在 {screen}，规则层没有动作")
                return
            name = act.pop("action")
            res = await client.act(name, **act)
            print(f"[{i}] {screen} · {name} {act} → {res.get('status')} {res.get('message') or res.get('error')}")
            await asyncio.sleep(args.interval)
        print("跑完了还没遇到目标界面")
    finally:
        await client.close()


if __name__ == "__main__":
    main()
