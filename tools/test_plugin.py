# -*- coding: utf-8 -*-
"""不依赖真游戏、不依赖 AstrBot 启动器的端到端测试。

做的事：
  1. 起一个 STS2_MCP 模拟服务器（tools/mock_sts2.py）；
  2. 真正 import 插件 main.py（用本机 AstrBot 4.29.4 的 astrbot 包）；
  3. 用假 Context / 假 Provider / 假 event 驱动 /sts2 自动；
  4. 断言：能一路打过战斗、领奖励、上楼层，且没有连续报错。

运行（用 AstrBot 实例的 venv，且 PYTHONPATH 指向它的 core）：
    python tools/test_plugin.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT
CORE = Path(r"C:\Users\Administrator\.astrbot_launcher\instances"
            r"\2f597663-e149-4d1f-8644-244dcd5d4e6c\core")
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT / "tools"))

import mock_sts2  # noqa: E402


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
    def __init__(self, umo: str = "test:umo"):
        self.unified_msg_origin = umo
        self.sent: list[str] = []

    def plain_result(self, text: str):
        self.sent.append(text)
        return text

    def get_sender_id(self) -> str:
        return "10001"


async def main() -> int:
    # 导入 astrbot 时它会在当前目录 bootstrap 一个 data/（默认模板），测完清掉
    stray = Path.cwd() / "data"
    had_data = stray.exists()
    server = mock_sts2.create_server(0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    mod = load_plugin()
    print("插件导入成功：", mod.__name__)

    # ---- 纯函数检查 ----
    st = mock_sts2.GAME.snapshot()
    text = mod.render_state(st)
    assert "地图" in text, text
    assert mod.render_state({"state_type": "unknown"})
    acts = mod.parse_llm_json('好的，我这样打：```json\n{"think":"x","actions":[{"action":"end_turn"}]}\n```')
    assert acts and acts[0]["action"] == "end_turn", acts
    assert mod.parse_llm_json("") == []

    provider = FakeProvider('{"actions": []}')   # 模型永远给空 → 回退规则层
    ctx = FakeContext(provider)
    cfg = {
        "sts2_url": base,
        "step_interval_sec": 0,
        "report_every": 1000,
        "max_steps": 200,
        "use_llm": True,
        "llm_combat": False,
        "operator_ids": [],
    }
    plugin = mod.Sts2PlayerPlugin(ctx, cfg)

    event = FakeEvent()
    steps = 0
    finished = False
    async for _ in plugin._auto(event, 200):
        steps += 1
        if plugin._running == set():
            finished = True
    print(f"自动推进结束：生成 {steps} 次消息，finished={finished}")

    final = await plugin.client.state()
    print("\n===== 最终状态 =====")
    print(mod.render_state(final))

    report = "\n".join(str(s) for s in event.sent)
    assert "连续失败" not in report, "出现连续失败：\n" + report
    assert "规则层不知道" not in report, "规则层卡住：\n" + report
    assert provider.calls > 0, "LLM 从未被调用（use_llm=True 时应至少问一次）"

    assert final["run"]["floor"] >= 1
    assert final["player"]["gold"] >= 99, final["player"]
    assert final["run"]["floor"] >= 2, f"一步都没推进：{final['run']}"

    # 规则机器人打完整幕：到第 6 层 Boss 并把它打掉
    assert final["run"]["floor"] >= 6, f"没走到 Boss：{final['run']}"
    assert final["state_type"] == "game_over", f"没打完 Boss：{final['state_type']}"
    # 中途领过牌，卡组应该变厚
    assert len(mock_sts2.GAME.deck) > 10, len(mock_sts2.GAME.deck)

    # 规则机器人的选牌不能拿诅咒
    assert all(c["type"] != "Curse" for c in mock_sts2.GAME.deck)

    # ---- 指令层：/sts2 状态能出快照 ----
    mock_sts2.GAME.reset()
    event2 = FakeEvent()
    async for _ in plugin.sts2_cmd(event2, ""):
        pass
    assert event2.sent and "地图" in event2.sent[-1], event2.sent[-1:]

    # ---- LLM 决策真的能驱动游戏（不只是被调用） ----
    provider.text = (
        '```json\n{"think":"先走第一层","actions":[{"action":"choose_map_node","index":0}]}\n```'
    )
    before = provider.calls
    event3 = FakeEvent()
    async for _ in plugin._auto(event3, 3):
        pass
    assert provider.calls > before, "use_llm 下没有走模型"
    st3 = await plugin.client.state()
    assert st3["state_type"] in ("monster", "elite", "boss"), f"模型给的动作没有生效：{st3['state_type']}"

    # ---- 卡组构筑层：选牌/升级/删牌都要看整副牌，而不是只看单卡 ----
    check_deck_sense(plugin.bot)
    # ---- 连招层：能挂易伤就先挂，后面的攻击牌全部 +50% ----
    check_vuln_order(plugin.bot)

    await plugin.client.close()
    server.shutdown()
    server.server_close()
    if not had_data and stray.exists():
        shutil.rmtree(stray, ignore_errors=True)
    print("\n✅ 全部断言通过")
    return 0


def card(name, ctype, cost, desc, rarity="Common"):
    return {"name": name, "type": ctype, "cost": cost, "description": desc,
            "rarity": rarity, "is_upgraded": False}


def deck_of(strikes: int, defends: int, extra: list[dict] | None = None) -> list[dict]:
    deck = [card("打击", "Attack", "1", "造成6点伤害。") for _ in range(strikes)]
    deck += [card("防御", "Skill", "1", "获得5点格挡。") for _ in range(defends)]
    deck += extra or []
    return deck


def indexed(cards: list[dict]) -> list[dict]:
    for i, c in enumerate(cards):
        c["index"] = i
    return cards


def check_deck_sense(bot) -> None:
    """构筑层单元检查：缺什么补什么、升级挑核心、删牌删基础牌、血少先睡。"""
    # 1) 一手基础攻击牌、没有格挡 → 应该在「攻击牌」和「格挡牌」之间选格挡
    st = {
        "state_type": "card_reward",
        "player": {"deck": deck_of(8, 0), "hp": 80, "max_hp": 80},
        "card_reward": {
            "can_skip": True,
            "cards": indexed([
                card("重击", "Attack", "2", "造成14点伤害。", "Uncommon"),
                card("铁斩波", "Skill", "1", "获得7点格挡。", "Common"),
            ]),
        },
    }
    act = bot.next_action(st)
    assert act == {"action": "select_card_reward", "card_index": 1}, f"缺格挡时没补格挡：{act}"
    print("✓ 缺格挡 → 补格挡")

    # 2) 反过来：一堆格挡、没有像样的攻击 → 补攻击
    st["player"]["deck"] = deck_of(0, 9)
    act = bot.next_action(st)
    assert act == {"action": "select_card_reward", "card_index": 0}, f"缺攻击时没补攻击：{act}"
    print("✓ 缺攻击 → 补攻击")

    # 3) 牌组已经很厚、牌又平庸 → 跳过（塔牌里薄才是强）
    st["player"]["deck"] = deck_of(8, 8, [card("顺劈斩", "Attack", "1", "造成8点伤害。")])
    st["card_reward"]["cards"] = indexed([
        card("顺劈斩", "Attack", "1", "造成8点伤害。"),
        card("铁斩波", "Skill", "1", "获得5点格挡。"),
        card("猛击", "Attack", "1", "造成7点伤害。"),
    ])
    act = bot.next_action(st)
    assert act == {"action": "skip_card_reward"}, f"牌组过厚还在收垃圾牌：{act}"
    print("✓ 牌组过厚 → 跳过平庸牌")

    # 4) 升级：优先痛击（上易伤的核心牌），而不是基础打击
    st_sel = {
        "state_type": "card_select",
        "player": {"deck": deck_of(3, 3), "hp": 80, "max_hp": 80},
        "card_select": {
            "kind": "deck_upgrade_select",
            "cards": [
                {"index": 0, **card("打击", "Attack", "1", "造成6点伤害。")},
                {"index": 1, **card("痛击", "Attack", "2", "造成8点伤害。施加2层易伤。")},
            ],
        },
    }
    act = bot.next_action(st_sel)
    assert act == {"action": "select_card", "index": 1}, f"升级选错了牌：{act}"
    print("✓ 升级优先核心牌（痛击）")

    # 5) 删牌：删基础打击，不删痛击
    st_sel["card_select"] = {
        "kind": "deck_card_select",
        "prompt": "选择一张牌移除",
        "cards": [
            {"index": 0, **card("痛击", "Attack", "2", "造成8点伤害。施加2层易伤。")},
            {"index": 1, **card("打击", "Attack", "1", "造成6点伤害。")},
        ],
    }
    act = bot.next_action(st_sel)
    assert act == {"action": "select_card", "index": 1}, f"删牌选错了牌：{act}"
    print("✓ 删牌优先基础牌（打击）")

    # 6) 营火：满血且还有牌可升 → 升级；残血 → 睡觉
    rest = {
        "state_type": "rest_site",
        "player": {"deck": deck_of(3, 3), "hp": 80, "max_hp": 80},
        "rest_site": {"options": [
            {"index": 0, "id": "rest", "is_enabled": True},
            {"index": 1, "id": "smith", "is_enabled": True},
        ]},
    }
    assert bot.next_action(rest) == {"action": "choose_rest_option", "index": 1}, "满血没去升级"
    rest["player"]["hp"] = 20
    assert bot.next_action(rest) == {"action": "choose_rest_option", "index": 0}, "残血还去升级"
    print("✓ 营火：满血升级、残血睡觉")

    # 7) 商店：牌组厚了就该买删牌服务，而不是再塞一张牌
    shop = {
        "state_type": "shop",
        "player": {"deck": deck_of(8, 8), "hp": 80, "max_hp": 80, "gold": 300,
                   "potions": [], "max_potion_slots": 3},
        "shop": {
            "is_open": True,
            "items": [{"index": 3, "category": "card_removal", "price": 75, "is_stocked": True}],
        },
    }
    act = bot.next_action(shop)
    assert act and act.get("category") == "card_removal", f"牌组厚了没买删牌：{act}"
    print("✓ 商店：牌组厚时买删牌服务")


def check_vuln_order(bot) -> None:
    """连招层：这一回合打不死对面时，先挂易伤再动手。"""
    def combat_state(hand, energy=3, enemy_hp=80, enemy_status=None, incoming=10):
        return {
            "state_type": "monster",
            "player": {"hp": 70, "max_hp": 80, "block": 0, "energy": energy,
                       "hand": indexed(hand), "potions": []},
            "battle": {
                "round": 1, "turn": "player", "is_play_phase": True,
                "enemies": [{
                    "entity_id": "0", "name": "小怪", "hp": enemy_hp, "max_hp": enemy_hp,
                    "block": 0, "status": enemy_status or [],
                    "intents": [{"type": "Attack", "label": str(incoming), "description": ""}],
                }],
            },
        }

    bash = card("痛击", "Attack", "2", "造成8点伤害。施加2层易伤。")
    strike = card("打击", "Attack", "1", "造成6点伤害。")

    # 1) 打不死对面 → 先痛击（挂易伤），而不是先打一张打击
    act = bot.next_action(combat_state([bash, strike, strike], energy=3, enemy_hp=80))
    assert act and act["action"] == "play_card" and act["card_index"] == 0, f"没有先挂易伤：{act}"
    print("✓ 连招：打不死时先挂易伤")

    # 2) 对面身上已经有易伤 → 别再挂，直接打伤害
    act = bot.next_action(combat_state([bash, strike, strike], energy=3, enemy_hp=80,
                                       enemy_status=[{"name": "Vulnerable"}]))
    assert act and act["action"] == "play_card" and act["card_index"] != 0, f"重复挂易伤：{act}"
    print("✓ 连招：已有易伤就不重复挂")

    # 3) 这一回合能打死 → 不要为了易伤拖节奏，直接斩杀
    act = bot.next_action(combat_state([bash, strike, strike], energy=3, enemy_hp=12))
    assert act and act["action"] == "play_card" and act["card_index"] != 0, f"能斩杀却先挂易伤：{act}"
    print("✓ 连招：能斩杀时优先斩杀")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
