"""专门验证「营火锻造」这条路：选 SMITH → 选牌界面 → 确认 → 牌真的升级了。

真机上「升级」和「删牌」共用 CARD_SELECTION，光看代码容易想当然，
所以这里直接在真游戏里走一遍，把原始 payload 和规则层决策都打出来。

用法（游戏开着、接口在 8080）：
    python tools/probe_smith.py --steps 400
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances\2f597663-e149-4d1f-8644-244dcd5d4e6c\core")


def load_plugin():
    if str(CORE) not in sys.path:
        sys.path.insert(0, str(CORE))
    spec = importlib.util.spec_from_file_location("sts2_plugin_main", ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sts2_plugin_main"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:8080")
    p.add_argument("--steps", type=int, default=400)
    p.add_argument("--api-interval", type=float, default=0.5)
    p.add_argument("--force-smith", action="store_true", help="不管血量，到了营火一律选锻造")
    args = p.parse_args()

    mod = load_plugin()
    client = mod.Sts2Client(args.api)
    bot = mod.RuleBot(potion_hp_ratio=0.32, block_priority=0.6, card_min=18.0)

    try:
        for i in range(args.steps):
            st = await client.state()
            stype = st.get("state_type")
            if stype == "rest_site" and not (st.get("rest_site") or {}).get("options"):
                # 已经在这歇过了，只剩「继续」——先走出去
                print(f"[{i}] 营火已用过，继续前进")
                await client.act("proceed")
                await asyncio.sleep(1.0)
                continue
            if stype == "rest_site":
                ratio = float(st["player"]["hp"]) / max(1.0, float(st["player"]["max_hp"]))
                print(f"[{i}] 到达营火：{st['player']['hp']}/{st['player']['max_hp']} ({ratio:.0%})")
                for o in st["rest_site"]["options"]:
                    print(f"    选项 {o['index']} id={o['id']} 名字={o['name']} 可用={o['is_enabled']}")
                raw = await client.state()  # 触发 last_raw
                with open("rest_raw2.json", "w", encoding="utf-8") as fh:
                    json.dump(getattr(client, "last_raw", None), fh, ensure_ascii=False, indent=2)
                act = bot.next_action(st)
                if args.force_smith:
                    act = {"action": "choose_rest_option", "index": 1}
                print(f"    规则层决定：{act}")
                if not act or act["action"] != "choose_rest_option":
                    return 1
                res = await client.act(act["action"], **{k: v for k, v in act.items() if k != "action"})
                print(f"    → {res.get('status')} {res.get('message') or res.get('error')}")
                await asyncio.sleep(1.0)

                # 接下来必须是选牌界面：走规则层的 _card_select，再确认
                for _ in range(6):
                    st2 = await client.state()
                    print(f"    之后界面：{st2.get('state_type')}")
                    if st2.get("state_type") != "card_select":
                        break
                    sel = st2.get("card_select") or {}
                    print(f"      kind={sel.get('kind')} prompt={sel.get('prompt')} "
                          f"min={sel.get('min_select')} max={sel.get('max_select')} "
                          f"selected={sel.get('selected_count')}")
                    for c in sel.get("cards") or []:
                        print(f"      [{c['index']}] {c['name']} 已选={c.get('selected')}")
                    act2 = bot.next_action(st2)
                    print(f"      规则层决定：{act2}")
                    if not act2:
                        break
                    res2 = await client.act(act2["action"], **{k: v for k, v in act2.items() if k != "action"})
                    print(f"      → {res2.get('status')} {res2.get('message') or res2.get('error')}")
                    await asyncio.sleep(0.8)
                    if act2["action"] == "confirm_selection":
                        break

                st3 = await client.state()
                deck = st3.get("player", {}).get("deck") or []
                up = [c["name"] for c in deck if c.get("is_upgraded")]
                print(f"    升级后的牌：{up or '（没有，说明锻造没成功）'}")
                return 0

            if stype in ("game_over", "menu", "overlay"):
                act = bot.next_action(st)
                if not act:
                    print(f"[{i}] 停在 {stype}，规则层没有动作")
                    return 1
                await client.act(act["action"], **{k: v for k, v in act.items() if k != "action"})
                await asyncio.sleep(1.0)
                continue

            act = bot.next_action(st)
            if not act:
                await asyncio.sleep(0.8)
                continue
            await client.act(act["action"], **{k: v for k, v in act.items() if k != "action"})
            await asyncio.sleep(args.api_interval)

        print("步数用完还没碰到营火，重跑一次（每次随机地图）")
        return 2
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
