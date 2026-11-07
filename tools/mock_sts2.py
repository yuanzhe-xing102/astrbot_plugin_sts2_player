# -*- coding: utf-8 -*-
"""STS2_MCP 的本地模拟器：假装自己是《杀戮尖塔 2》+ mod 的 REST 接口。

用途：没有 Windows 游戏 / 没装 mod 的时候，也能联调插件、跑 e2e 测试。
接口形状与 STS2MCP v0.3.4 的 docs/raw-full.md 保持一致（动作名、字段名、错误用
{"status":"error","error":...} 表达）。

运行：
    python tools/mock_sts2.py --port 8811
然后插件配置 sts2_url = http://127.0.0.1:8811
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FLOOR_PLAN: dict[int, list[str]] = {
    1: ["Monster"],
    2: ["Monster", "Event"],
    3: ["Treasure", "Shop"],
    4: ["Elite", "Event"],
    5: ["RestSite", "Monster"],
    6: ["Boss"],
}
MAX_FLOOR = 6

# 事件池：覆盖 event / card_select / relic_select / hand_select / crystal_sphere /
# fake_merchant 这几种界面，让规则层真的被走过一遍，而不是靠猜。
EVENT_IDS = ["BIG_FISH", "SHINING_LIGHT", "CRYSTAL_SPHERE", "GOLDEN_IDOL", "FAKE_MERCHANT"]


def _card(cid: str, name: str, ctype: str, cost: str, desc: str, **kw: Any) -> dict:
    return {
        "id": cid,
        "name": name,
        "type": ctype,
        "cost": cost,
        "description": desc,
        "target_type": kw.pop("target_type", "None"),
        "upgraded": kw.pop("upgraded", False),
        **kw,
    }


def apply_upgrade(card: dict) -> None:
    """升级一张牌的数值：真实游戏里升级通常 +2~4 点伤害/格挡，这里按 +30% 算。"""
    desc = str(card.get("description") or "")
    if "dmg" in card:
        card["dmg"] = int(card["dmg"]) + max(2, int(int(card["dmg"]) * 0.3))
        dmg = card["dmg"]
        desc = re.sub(r"(Deal\s+)\d+", rf"\g<1>{dmg}", desc)
        desc = re.sub(r"造成\s*\d+", f"造成{dmg}", desc)
    if "block" in card:
        card["block"] = int(card["block"]) + max(2, int(int(card["block"]) * 0.3))
        blk = card["block"]
        desc = re.sub(r"(Gain\s+)\d+(\s+Block)", rf"\g<1>{blk}\g<2>", desc)
    card["description"] = desc
    card["upgraded"] = True


def build_deck() -> list[dict]:
    deck = []
    for _ in range(5):
        deck.append(_card("STRIKE_R", "Strike", "Attack", "1", "Deal 6 damage.",
                          target_type="AnyEnemy", dmg=6))
    for _ in range(4):
        deck.append(_card("DEFEND_R", "Defend", "Skill", "1", "Gain 5 Block.", block=5))
    deck.append(_card("BASH", "Bash", "Attack", "2", "Deal 8 damage. Apply 2 Vulnerable.",
                      target_type="AnyEnemy", dmg=8, vuln=2))
    deck.append(_card("INFLAME", "Inflame", "Power", "1", "Gain 2 Strength.", strength=2))
    return deck


CARD_POOL = [
    _card("UPPERCUT", "Uppercut", "Attack", "2", "Deal 13 damage.", target_type="AnyEnemy",
          dmg=13, rarity="Uncommon"),
    _card("SHRUG_IT_OFF", "Shrug It Off", "Skill", "1", "Gain 8 Block. Draw 1 card.",
          block=8, draw=1, rarity="Common"),
    _card("OFFERING", "Offering", "Skill", "0", "Lose 6 HP. Gain 2 Energy. Draw 3 cards.",
          energy=2, draw=3, lose_hp=6, rarity="Rare"),
    _card("ANGER", "Anger", "Attack", "0", "Deal 6 damage.", target_type="AnyEnemy",
          dmg=6, rarity="Common"),
    _card("METALLICIZE", "Metallicize", "Power", "1", "At the end of your turn, gain 3 Block.",
          rarity="Uncommon"),
]


class Game:
    """一局的完整状态机。所有方法都在锁内调用。"""

    def __init__(self, seed: int = 7):
        self.lock = threading.RLock()
        self.rng = random.Random(seed)
        self.reset()

    # ---------------- 初始化 ----------------
    def reset(self) -> None:
        self.state_type = "map"
        self.act_no = 1
        self.floor = 0
        self.player: dict[str, Any] = {
            "character": "The Ironclad",
            "hp": 80,
            "max_hp": 80,
            "block": 0,
            "gold": 99,
            "status": [],
            "relics": [{
                "id": "BURNING_BLOOD",
                "name": "Burning Blood",
                "description": "At the end of combat, heal 6 HP.",
                "counter": None,
                "keywords": [],
            }],
            "potions": [],
            "max_potion_slots": 3,
        }
        self.deck: list[dict] = build_deck()
        self.hand: list[dict] = []
        self.draw_pile: list[dict] = []
        self.discard_pile: list[dict] = []
        self.exhaust_pile: list[dict] = []
        self.enemies: list[dict] = []
        self.energy = 0
        self.max_energy = 3
        self.round = 0
        self.rewards: dict | None = None
        self.card_reward: dict | None = None
        self.rest: dict | None = None
        self.shop: dict | None = None
        self.treasure: dict | None = None
        self.event: dict | None = None
        self.event_id = ""
        self.card_select: dict | None = None
        self.relic_select: dict | None = None
        self.hand_select: dict | None = None
        self.crystal: dict | None = None
        self.map_data: dict | None = None
        self.battle_round = 0
        self.game_over: dict | None = None
        self.message = "新的一局开始了。"
        self.last_error = ""
        self.enter_map()

    # ---------------- 地图 ----------------
    def enter_map(self) -> None:
        self.state_type = "map"
        self.floor += 1
        if self.floor > MAX_FLOOR:
            self.end_run(True)
            return
        types = FLOOR_PLAN.get(self.floor, ["Monster"])
        options = []
        for i, t in enumerate(types):
            options.append({
                "index": i,
                "col": 1 + i,
                "row": self.floor,
                "type": t,
                "leads_to": [{"col": 1 + i, "row": self.floor + 1,
                              "type": FLOOR_PLAN.get(self.floor + 1, ["Monster"])[0]}],
            })
        self.map_data = {
            "current_position": {"col": 1, "row": self.floor - 1, "type": "Start" if self.floor == 1 else "Monster"},
            "visited": [],
            "next_options": options,
            "nodes": [],
            "boss": {"col": 2, "row": 6, "id": "ENCOUNTER.VANTOM_BOSS", "name": "Vantom"},
            "bosses": [],
        }

    def end_run(self, won: bool) -> None:
        self.state_type = "game_over"
        self.game_over = {
            "message": "你击败了 Vantom，这一局赢了。" if won else "你死了。",
            "options": ["main_menu"],
        }

    # ---------------- 战斗 ----------------
    def start_combat(self, kind: str) -> None:
        self.state_type = kind
        self.round = 1
        self.battle_round = 1
        self.energy = self.max_energy
        self.player["block"] = 0
        self.hand = []
        self.draw_pile = list(self.deck)
        self.rng.shuffle(self.draw_pile)
        self.discard_pile = []
        self.exhaust_pile = []
        if kind == "boss":
            self.enemies = [self._enemy("VANTOM_0", "Vantom", 85, 15, pattern=[10, 12, 18])]
        elif kind == "elite":
            self.enemies = [
                self._enemy("GREMLIN_NOB_0", "Gremlin Nob", 85, 12, pattern=[10, 10, 18]),
            ]
        else:
            # 普通战斗按真实第一幕的强度给：单只小怪，11 点上下伤害
            pick = self.rng.choice([
                ("JAW_WORM_0", "Jaw Worm", 44, 11),
                ("CULTIST_0", "Cultist", 48, 9),
                ("LOUSE_0", "Red Louse", 40, 10),
            ])
            self.enemies = [self._enemy(*pick)]
        self.draw(5)

    @staticmethod
    def _enemy(entity_id: str, name: str, hp: int, dmg: int,
               pattern: list[int] | None = None) -> dict:
        foe = {
            "entity_id": entity_id,
            "combat_id": abs(hash(entity_id)) % 1000,
            "name": name,
            "hp": hp,
            "max_hp": hp,
            "block": 0,
            "status": [],
            "intents": [],
            # 攻击模式：模拟真实敌人的「小招→大招」节奏，格挡才有价值
            "pattern": list(pattern or [dmg]),
            "pattern_idx": 0,
        }
        Game._set_intent(foe)
        return foe

    @staticmethod
    def _set_intent(foe: dict) -> None:
        dmg = foe["pattern"][foe["pattern_idx"] % len(foe["pattern"])]
        heavy = dmg >= 20
        foe["intents"] = [{
            "type": "Attack",
            "label": str(dmg),
            "title": "Heavy Attack" if heavy else "Attack",
            "description": f"Deals {dmg} damage.",
        }]

    def draw(self, n: int) -> None:
        for _ in range(n):
            if not self.draw_pile:
                if not self.discard_pile:
                    return
                self.draw_pile = self.discard_pile
                self.discard_pile = []
                self.rng.shuffle(self.draw_pile)
            self.hand.append(self.draw_pile.pop())

    def alive_enemies(self) -> list[dict]:
        return [e for e in self.enemies if e["hp"] > 0]

    def can_play(self, card: dict) -> tuple[bool, str | None]:
        if self.state_type not in ("monster", "elite", "boss"):
            return False, "NotInCombat"
        if card["cost"] == "X":
            return True, None
        try:
            cost = int(card["cost"])
        except ValueError:
            return True, None
        if cost > self.energy:
            return False, "NotEnoughEnergy"
        return True, None

    def hand_snapshot(self) -> list[dict]:
        out = []
        for i, c in enumerate(self.hand):
            ok, reason = self.can_play(c)
            out.append({
                "index": i,
                "id": c["id"],
                "name": c["name"],
                "type": c["type"],
                "cost": c["cost"],
                "star_cost": None,
                "description": c["description"],
                "target_type": c["target_type"],
                "can_play": ok,
                "unplayable_reason": reason,
                "is_upgraded": c.get("upgraded", False),
                "keywords": [],
            })
        return out

    def play_card(self, card_index: int, target: str | None) -> str:
        if self.state_type not in ("monster", "elite", "boss"):
            raise GameError("Not in combat.")
        if not (0 <= int(card_index) < len(self.hand)):
            raise GameError(f"Card index {card_index} out of range.")
        card = self.hand[int(card_index)]
        ok, reason = self.can_play(card)
        if not ok:
            raise GameError(reason or "Unplayable")
        enemy = None
        if card["target_type"] == "AnyEnemy":
            if not target:
                raise GameError("Card requires a target. Provide 'target' with an entity_id.")
            enemy = next((e for e in self.enemies if e.get("entity_id") == target and e["hp"] > 0), None)
            if enemy is None:
                raise GameError(f"No living enemy with entity_id {target}.")

        # 自伤代价（Offering 之类）：先付血再结算收益，血量归零直接死
        if card.get("lose_hp"):
            self.player["hp"] = max(0, self.player["hp"] - int(card["lose_hp"]))
            if self.player["hp"] <= 0:
                self.hand.pop(int(card_index))
                self.end_run(False)
                return "你被自己的牌抽干了血。"

        if card["type"] == "Power":
            self.player.setdefault("status", []).append({
                "id": "STRENGTH", "name": "Strength", "amount": card.get("strength", 2),
                "type": "Buff", "description": "Increases attack damage.", "keywords": [],
            })
        if card.get("dmg"):
            dmg = card["dmg"]
            strength = sum(s.get("amount", 0) for s in self.player.get("status", [])
                           if s.get("id") == "STRENGTH")
            dmg += strength
            if enemy is not None:
                # 易伤：受到的攻击伤害 +50%，这是「先挂易伤再打」的收益来源
                if any(s.get("id") == "VULNERABLE" for s in enemy.get("status", [])):
                    dmg = int(dmg * 1.5)
                absorbed = min(enemy["block"], dmg)
                enemy["block"] -= absorbed
                enemy["hp"] = max(0, enemy["hp"] - (dmg - absorbed))
        if card.get("vuln") and enemy is not None and enemy["hp"] > 0:
            stack = next((s for s in enemy.setdefault("status", [])
                          if s.get("id") == "VULNERABLE"), None)
            if stack is None:
                enemy["status"].append({
                    "id": "VULNERABLE", "name": "Vulnerable", "amount": int(card["vuln"]),
                    "type": "Debuff", "description": "Takes 50% more damage from attacks.",
                    "keywords": [],
                })
            else:
                stack["amount"] = int(stack.get("amount", 0)) + int(card["vuln"])
        if card.get("block"):
            self.player["block"] += card["block"]
        if card.get("draw"):
            self.draw(card["draw"])
        if card.get("energy"):
            self.energy += card["energy"]
        if card["cost"] != "X":
            self.energy -= int(card["cost"])

        self.hand.pop(int(card_index))
        self.discard_pile.append(card)
        self.message = f"Playing '{card['name']}'"
        if target and enemy is not None and card.get("dmg"):
            self.message += f" targeting {enemy['name']}"
        if not self.alive_enemies():
            self.win_combat()
        return self.message

    def end_turn(self) -> str:
        if self.state_type not in ("monster", "elite", "boss"):
            raise GameError("Not in combat.")
        incoming = 0
        for e in self.alive_enemies():
            for it in e["intents"]:
                if str(it.get("type", "")).lower() == "attack":
                    incoming += int(it.get("label") or 0)
        block = int(self.player["block"] or 0)
        taken = max(0, incoming - block)
        self.player["hp"] = max(0, self.player["hp"] - taken)
        if self.player["hp"] <= 0:
            self.end_run(False)
            return "你死了。"
        self.round += 1
        self.energy = self.max_energy
        self.player["block"] = 0
        for e in self.alive_enemies():
            e["pattern_idx"] = e.get("pattern_idx", 0) + 1
            self._set_intent(e)
            # 易伤按回合数衰减，掉光就没了
            for s in list(e.get("status") or []):
                if s.get("id") == "VULNERABLE":
                    s["amount"] = int(s.get("amount", 0)) - 1
                    if s["amount"] <= 0:
                        e["status"].remove(s)
        self.hand = []
        self.draw(5)
        self.message = f"敌人攻击造成了 {taken} 点伤害（护甲挡下 {incoming - taken}）。"
        return self.message

    def win_combat(self) -> None:
        self.state_type = "rewards"
        heal = 6
        self.player["hp"] = min(self.player["max_hp"], self.player["hp"] + heal)
        items = [
            {"index": 0, "type": "gold", "description": "Obtain 25 gold.", "gold_amount": 25},
            {"index": 1, "type": "card", "description": "Add a card to your deck."},
        ]
        # 真实游戏里药水掉率不低，模拟器按 50% 给一瓶
        if self.rng.random() < 0.5 and len(self.player["potions"]) < self.player["max_potion_slots"]:
            items.insert(1, {
                "index": 1,
                "type": "potion",
                "description": "Obtain a Fire Potion.",
                "potion_id": "FIRE_POTION",
                "potion_name": "Fire Potion",
            })
            for n, it in enumerate(items):
                it["index"] = n
        self.rewards = {"items": items, "can_proceed": True}
        self.enemies = []
        self.hand = []
        self.message = "战斗胜利。"

    # ---------------- 动作分发 ----------------
    def act(self, action: str, params: dict) -> str:
        with self.lock:
            handler = getattr(self, f"do_{action}", None)
            if handler is None:
                raise GameError(f"Unknown action '{action}'.")
            return handler(**{k: v for k, v in params.items()
                              if k in handler.__code__.co_varnames[: handler.__code__.co_argcount]})

    def do_play_card(self, card_index: int, target: str | None = None) -> str:
        return self.play_card(card_index, target)

    def do_end_turn(self) -> str:
        return self.end_turn()

    def do_use_potion(self, slot: int, target: str | None = None) -> str:
        for p in self.player["potions"]:
            if p["slot"] != slot:
                continue
            if p.get("id") == "FIRE_POTION":
                enemies = self.alive_enemies()
                if not enemies:
                    raise GameError("No target for the potion.")
                foe = next((e for e in enemies if e["entity_id"] == target), enemies[0])
                foe["hp"] = max(0, foe["hp"] - 20)
                self.player["potions"].remove(p)
                if not self.alive_enemies():
                    self.win_combat()
                    return "药水击杀了敌人。"
                return f"用 {p['name']} 打了 {foe['name']} 20 点。"
            self.player["potions"].remove(p)
            self.player["hp"] = min(self.player["max_hp"], self.player["hp"] + 20)
            return f"Used {p['name']}."
        raise GameError("Potion slot is empty.")

    def do_claim_reward(self, index: int) -> str:
        if not self.rewards:
            raise GameError("Not in rewards.")
        item = next((i for i in self.rewards["items"] if i["index"] == int(index)), None)
        if item is None:
            raise GameError(f"No reward at index {index}.")
        self.rewards["items"].remove(item)
        for n, it in enumerate(self.rewards["items"]):
            it["index"] = n
        if item["type"] == "gold":
            self.player["gold"] += item.get("gold_amount", 0)
            self.message = "获得了金币。"
        elif item["type"] == "card":
            cards = [dict(c, index=i) for i, c in enumerate(self.rng.sample(CARD_POOL, 3))]
            self.card_reward = {"cards": cards, "can_skip": True}
            self.state_type = "card_reward"
            self.message = "选择一张牌。"
        elif item["type"] == "potion":
            self.player["potions"].append({
                "id": item.get("potion_id") or "FIRE_POTION",
                "name": item.get("potion_name") or "Fire Potion",
                "description": "Deal 20 damage to target enemy.",
                "slot": len(self.player["potions"]),
                "can_use_in_combat": True,
                "target_type": "AnyEnemy",
                "keywords": [],
            })
            self.message = "获得了药水。"
        return self.message

    def do_select_card_reward(self, card_index: int) -> str:
        if not self.card_reward:
            raise GameError("Not choosing a card.")
        card = next((c for c in self.card_reward["cards"] if c["index"] == int(card_index)), None)
        if card is None:
            raise GameError(f"No card at index {card_index}.")
        # 奖励界面只给名字/描述，真正的数值要从卡池里取回来，否则打出去不生效
        mech = next((c for c in CARD_POOL if c["id"] == card.get("id")), None)
        new_card = dict(mech) if mech is not None else {k: v for k, v in card.items() if k != "index"}
        if mech is not None and card.get("is_upgraded"):
            new_card["upgraded"] = True
        self.deck.append(new_card)
        self.card_reward = None
        self.state_type = "rewards"
        self.message = f"把 {card['name']} 加入了卡组。"
        return self.message

    def do_skip_card_reward(self) -> str:
        self.card_reward = None
        self.state_type = "rewards"
        self.message = "跳过了选牌。"
        return self.message

    def do_proceed(self) -> str:
        if self.state_type in ("rewards", "shop", "rest_site", "treasure", "fake_merchant"):
            self.rewards = None
            self.shop = None
            self.rest = None
            self.treasure = None
            self.enter_map()
            return "进入了下一层。"
        raise GameError("Cannot proceed from this screen.")

    def do_choose_map_node(self, index: int) -> str:
        if self.state_type != "map" or not self.map_data:
            raise GameError("Not on the map.")
        opt = next((o for o in self.map_data["next_options"] if o["index"] == int(index)), None)
        if opt is None:
            raise GameError(f"No map node at index {index}.")
        kind = opt["type"]
        if kind in ("Monster", "Elite", "Boss"):
            self.start_combat({"Monster": "monster", "Elite": "elite", "Boss": "boss"}[kind])
            return f"进入战斗：{kind}"
        if kind == "RestSite":
            self.state_type = "rest_site"
            self.rest = {
                "options": [
                    {"index": 0, "id": "rest", "name": "Rest", "description": "Heal 30% of max HP.", "is_enabled": True},
                    {"index": 1, "id": "smith", "name": "Smith", "description": "Upgrade a card.", "is_enabled": True},
                ],
                "can_proceed": False,
            }
            return "来到了营火。"
        if kind == "Shop":
            self.state_type = "shop"
            items = []
            for i, c in enumerate(self.rng.sample(CARD_POOL, 3)):
                items.append({"index": i, "category": "card", "price": 60 + i * 10,
                              "is_stocked": True, "can_afford": self.player["gold"] >= 60 + i * 10,
                              "on_sale": False, "card_id": c["id"], "card_name": c["name"],
                              "card_type": c["type"], "card_cost": c["cost"], "card_star_cost": None,
                              "card_rarity": c.get("rarity", "Common"),
                              "card_description": c["description"], "keywords": []})
            items.append({"index": 3, "category": "card_removal", "price": 75,
                          "is_stocked": True, "can_afford": self.player["gold"] >= 75, "on_sale": False})
            # 真实商店一定卖药水，这里固定一瓶
            items.append({"index": 4, "category": "potion", "price": 50,
                          "is_stocked": True, "can_afford": self.player["gold"] >= 50,
                          "on_sale": False, "potion_id": "FIRE_POTION",
                          "potion_name": "Fire Potion",
                          "description": "Deal 20 damage to target enemy."})
            self.shop = {"items": items, "can_proceed": True}
            return "来到了商店。"
        if kind == "Treasure":
            self.state_type = "treasure"
            self.treasure = {
                "relics": [{"index": 0, "id": "LANTERN", "name": "Lantern",
                            "description": "Gain 1 Energy on the first turn of each combat.",
                            "rarity": "Uncommon", "keywords": []}],
                "can_proceed": False,
            }
            return "打开了宝箱。"
        if kind == "Event":
            self.enter_event()
            return "触发了一个事件。"
        raise GameError(f"Unsupported node type {kind}.")

    def do_choose_rest_option(self, index: int) -> str:
        if not self.rest:
            raise GameError("Not at a rest site.")
        opt = next((o for o in self.rest["options"] if o["index"] == int(index)), None)
        if opt is None or not opt.get("is_enabled", True):
            raise GameError("That option is not available.")
        if opt["id"] == "rest":
            healed = int(self.player["max_hp"] * 0.3)
            self.player["hp"] = min(self.player["max_hp"], self.player["hp"] + healed)
            self.enter_map()
            return f"休息，回复了 {healed} 点生命。"
        self._open_card_select("选择一张牌升级", mode="upgrade")
        return "开始升级一张牌。"

    def do_claim_treasure_relic(self, index: int) -> str:
        if not self.treasure:
            raise GameError("No treasure open.")
        relic = next((r for r in self.treasure["relics"] if r["index"] == int(index)), None)
        if relic is None:
            raise GameError(f"No relic at index {index}.")
        self.treasure["relics"].remove(relic)
        self.player["relics"].append({k: v for k, v in relic.items() if k != "index"})
        self.treasure["can_proceed"] = True
        self.message = f"获得遗物：{relic['name']}"
        return self.message

    def do_shop_purchase(self, index: int) -> str:
        if not self.shop:
            raise GameError("Not in a shop.")
        item = next((i for i in self.shop["items"] if i["index"] == int(index)), None)
        if item is None:
            raise GameError(f"No shop item at index {index}.")
        if not item.get("is_stocked", True):
            raise GameError("Sold out.")
        if self.player["gold"] < item["price"]:
            raise GameError("Not enough gold.")
        self.player["gold"] -= item["price"]
        item["is_stocked"] = False
        cat = item.get("category")
        if cat == "card":  # 真实游戏里买的牌会进卡组
            self.deck.append(_card(item["card_id"], item["card_name"], item["card_type"],
                                   item["card_cost"], item["card_description"],
                                   rarity=item.get("card_rarity", "Common")))
            self.message = f"买入 {item['card_name']}并加入卡组。"
            return self.message
        if cat == "potion":
            potions = self.player["potions"]
            if len(potions) < self.player["max_potion_slots"]:
                potions.append({
                    "id": item["potion_id"],
                    "name": item["potion_name"],
                    "description": item["description"],
                    "slot": max((p["slot"] for p in potions), default=-1) + 1,
                    "can_use_in_combat": True,
                    "target_type": "AnyEnemy",
                })
            self.message = f"买入 {item['potion_name']}。"
            return self.message
        if cat == "card_removal":
            if len(self.deck) <= 5:
                raise GameError("Deck is already thin.")
            self._open_card_select("选择一张牌移除", mode="remove")
            self.message = "买了删牌服务，选一张牌删掉。"
            return self.message
        self.message = "购买成功。"
        return self.message

    # ---------------- 事件与各类选择界面 ----------------
    EVENT_META = {
        "BIG_FISH": ("Big Fish", "河边有一条大鱼，它似乎想和你做笔交易。"),
        "SHINING_LIGHT": ("Shining Light", "一束光从岩缝里透出来，后面有什么在闪光。"),
        "CRYSTAL_SPHERE": ("Crystal Sphere", "地上滚着一颗水晶球，里面隐约有影子。"),
        "GOLDEN_IDOL": ("Golden Idol", "祭坛上躺着一座金像。"),
        "FAKE_MERCHANT": ("Fake Merchant", "一个可疑的商人挡在路中间。"),
    }

    def enter_event(self) -> None:
        eid = self.rng.choice(EVENT_IDS)
        self.event_id = eid
        name, body = self.EVENT_META[eid]
        self.state_type = "event"
        self.event = {
            "event_id": eid,
            "event_name": name,
            "is_ancient": False,
            "in_dialogue": True,
            "body": body,
            "options": [],
        }
        self.message = f"事件：{name}"

    def _event_options(self) -> list[dict]:
        eid = self.event_id

        def opt(i: int, title: str, desc: str, proceed: bool = False) -> dict:
            return {"index": i, "title": title, "description": desc,
                    "is_locked": False, "is_proceed": proceed, "was_chosen": False}

        if eid == "BIG_FISH":
            return [opt(0, "Banana", "Heal 10% of your max HP."),
                    opt(1, "Donut", "Gain 5 max HP."),
                    opt(2, "Box", "Obtain a relic.")]
        if eid == "SHINING_LIGHT":
            return [opt(0, "Enter the light", "Upgrade a card."),
                    opt(1, "Leave", "Nothing happens.", proceed=True)]
        if eid == "CRYSTAL_SPHERE":
            return [opt(0, "Divine", "Look into the sphere."),
                    opt(1, "Leave", "Nothing happens.", proceed=True)]
        if eid == "GOLDEN_IDOL":
            return [opt(0, "Pray", "Obtain a relic."),
                    opt(1, "Leave", "Nothing happens.", proceed=True)]
        return [opt(0, "Look inside", "Browse the merchant's wares."),
                opt(1, "Leave", "Nothing happens.", proceed=True)]

    def do_advance_dialogue(self) -> str:
        if not self.event:
            raise GameError("Not in an event.")
        if not self.event.get("in_dialogue"):
            raise GameError("No dialogue to advance.")
        self.event["in_dialogue"] = False
        self.event["options"] = self._event_options()
        return self.event["body"]

    def do_choose_event_option(self, index: int) -> str:
        if not self.event:
            raise GameError("Not in an event.")
        if self.event.get("in_dialogue"):
            raise GameError("The dialogue is still open.")
        opt = next((o for o in self.event["options"] if o["index"] == int(index)), None)
        if opt is None:
            raise GameError(f"No event option at index {index}.")
        eid, idx = self.event_id, int(index)
        player = self.player
        if eid == "BIG_FISH":
            if idx == 0:
                player["hp"] = min(player["max_hp"], player["hp"] + int(player["max_hp"] * 0.1))
                msg = "你吃了香蕉，回复了生命。"
            elif idx == 1:
                player["max_hp"] += 5
                player["hp"] += 5
                msg = "你吃了甜甜圈，最大生命 +5。"
            else:
                player["relics"].append({"id": "BLOOD_VIAL", "name": "Blood Vial",
                                         "description": "Heal 2 HP at the start of each combat.",
                                         "counter": None, "keywords": []})
                msg = "你从箱子里翻出了血瓶。"
            self.event = None
            self.enter_map()
            return msg
        if eid == "SHINING_LIGHT" and idx == 0:
            self._open_card_select("Upgrade a card.", upgrade=True)
            return "光里有一张牌浮在你面前。"
        if eid == "CRYSTAL_SPHERE" and idx == 0:
            self._open_crystal_sphere()
            return "水晶球里出现了网格。"
        if eid == "GOLDEN_IDOL" and idx == 0:
            self._open_relic_select()
            return "金像下面有件遗物。"
        if eid == "FAKE_MERCHANT" and idx == 0:
            self._open_fake_merchant()
            return "商人掀开了他的斗篷。"
        self.event = None
        self.enter_map()
        return "你选择了离开。"

    def _open_fake_merchant(self) -> None:
        self.state_type = "fake_merchant"
        self.shop = {
            "items": [
                {"index": 0, "category": "card", "cost": 40, "is_stocked": True, "can_afford": True,
                 "on_sale": False, "card_id": "ANGER", "card_name": "Anger", "card_type": "Attack",
                 "card_cost": "0", "card_rarity": "Common", "card_description": "Deal 6 damage."},
                {"index": 1, "category": "relic", "cost": 70, "is_stocked": True, "can_afford": True,
                 "on_sale": False, "name": "Cursed Key",
                 "description": "Gain 1 Energy each turn. Curses you when you open a chest."},
            ],
            "can_proceed": True,
        }

    def _open_card_select(self, prompt: str, upgrade: bool = True, mode: str | None = None) -> None:
        self.state_type = "card_select"
        mode = mode or ("upgrade" if upgrade else "choose")
        pool = self.deck if mode == "remove" else self.deck[:3]
        cards = []
        for i, c in enumerate(pool):
            cards.append({"index": i, "id": c.get("id"), "name": c["name"], "type": c["type"],
                          "cost": c["cost"], "description": c["description"],
                          "rarity": c.get("rarity", "Common"),
                          "is_upgraded": bool(c.get("upgraded"))})
        self.card_select = {
            "kind": {"upgrade": "deck_upgrade_select", "remove": "deck_card_select"}.get(mode, "choose_card_select"),
            "prompt": prompt, "cards": cards, "can_confirm": False,
            "can_cancel": True, "upgrade": upgrade, "mode": mode, "selected": [],
        }

    def do_select_card(self, index: int) -> str:
        if not self.card_select:
            raise GameError("No card selection open.")
        card = next((c for c in self.card_select["cards"] if c["index"] == int(index)), None)
        if card is None:
            raise GameError(f"No card at index {index}.")
        self.card_select["selected"] = [int(index)]
        self.card_select["can_confirm"] = True
        return f"选中了 {card['name']}。"

    def do_confirm_selection(self) -> str:
        if not self.card_select:
            raise GameError("No card selection open.")
        sel = self.card_select.get("selected") or []
        if not sel:
            raise GameError("Nothing selected.")
        idx = int(sel[0])
        card = next((c for c in self.card_select["cards"] if c["index"] == idx), None)
        if card and self.card_select.get("mode") == "remove":
            self.deck = [c for c in self.deck if c.get("name") != card["name"]]
            self.card_select = None
            self.enter_map()
            return f"从卡组里删掉了 {card['name']}。"
        if card and self.card_select.get("upgrade"):
            for c in self.deck:
                if c.get("name") == card["name"] and not c.get("upgraded"):
                    c["upgraded"] = True
                    apply_upgrade(c)
                    break
            self.card_select = None
            self.enter_map()
            return f"升级了 {card['name']}。"
        self.card_select = None
        self.enter_map()
        return "选择完成。"

    def do_cancel_selection(self) -> str:
        if not self.card_select:
            raise GameError("No card selection open.")
        self.card_select = None
        self.enter_map()
        return "取消了选择。"

    def _open_relic_select(self) -> None:
        self.state_type = "relic_select"
        self.relic_select = {
            "relics": [
                {"index": 0, "id": "BAG_OF_PREPARATION", "name": "Bag of Preparation",
                 "description": "Draw 2 additional cards at the start of each combat.",
                 "rarity": "Common", "keywords": []},
                {"index": 1, "id": "VENERABLE_TEA_SET", "name": "Venerable Tea Set",
                 "description": "Gain 2 Energy on the first turn of each combat.",
                 "rarity": "Uncommon", "keywords": []},
            ],
            "can_skip": True,
        }

    def do_select_relic(self, index: int) -> str:
        if not self.relic_select:
            raise GameError("No relic selection open.")
        relic = next((r for r in self.relic_select["relics"] if r["index"] == int(index)), None)
        if relic is None:
            raise GameError(f"No relic at index {index}.")
        self.player["relics"].append({k: v for k, v in relic.items() if k != "index"})
        self.relic_select = None
        self.enter_map()
        return f"获得遗物：{relic['name']}"

    def do_skip_relic_selection(self) -> str:
        if not self.relic_select:
            raise GameError("No relic selection open.")
        self.relic_select = None
        self.enter_map()
        return "跳过了遗物。"

    def _open_crystal_sphere(self) -> None:
        self.state_type = "crystal_sphere"
        cells = [{"x": x, "y": y, "revealed": False, "has_relic": x == 2 and y == 2}
                 for x in range(3) for y in range(3)]
        self.crystal = {"grid_w": 3, "grid_h": 3, "cells": cells,
                        "tool": "big", "can_proceed": False}

    def do_crystal_sphere_set_tool(self, tool: str = "big") -> str:
        if not self.crystal:
            raise GameError("The crystal sphere is not open.")
        if str(tool) not in ("big", "small"):
            raise GameError("Tool must be 'big' or 'small'.")
        self.crystal["tool"] = str(tool)
        return f"换成了 {tool} 工具。"

    def do_crystal_sphere_click_cell(self, x: int, y: int) -> str:
        if not self.crystal:
            raise GameError("The crystal sphere is not open.")
        cell = next((c for c in self.crystal["cells"] if c["x"] == int(x) and c["y"] == int(y)), None)
        if cell is None:
            raise GameError(f"No cell at ({x}, {y}).")
        cell["revealed"] = True
        if cell["has_relic"]:
            self.player["relics"].append({"id": "ODD_MUSHROOM", "name": "Odd Mushroom",
                                          "description": "Whenever you gain Weak, gain 1 more.",
                                          "counter": None, "keywords": []})
        if all(c["revealed"] for c in self.crystal["cells"]):
            self.crystal["can_proceed"] = True
        return f"点亮了 ({x}, {y})。"

    def do_crystal_sphere_proceed(self) -> str:
        if not self.crystal:
            raise GameError("The crystal sphere is not open.")
        self.crystal = None
        self.enter_map()
        return "离开了水晶球。"

    # ---------------- 快照 ----------------
    def snapshot(self) -> dict:
        with self.lock:
            st: dict[str, Any] = {
                "state_type": self.state_type,
                "run": {"act": self.act_no, "floor": self.floor, "ascension": 0},
                "player": dict(self.player),
            }
            # 整副牌：构筑层（选牌/升级/删牌）要用
            st["player"]["deck"] = [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "cost": c.get("cost"),
                    "description": c.get("description"),
                    "rarity": c.get("rarity", "Common"),
                    "is_upgraded": bool(c.get("upgraded")),
                }
                for c in self.deck
            ]
            player = st["player"]
            if self.state_type in ("monster", "elite", "boss", "hand_select"):
                player["energy"] = self.energy
                player["max_energy"] = self.max_energy
                player["hand"] = self.hand_snapshot()
                player["draw_pile_count"] = len(self.draw_pile)
                player["discard_pile_count"] = len(self.discard_pile)
                player["exhaust_pile_count"] = len(self.exhaust_pile)
                player["draw_pile"] = [
                    {"name": c["name"], "cost": c["cost"], "star_cost": None, "description": c["description"]}
                    for c in self.draw_pile
                ]
                st["battle"] = {
                    "round": self.round,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [dict(e) for e in self.enemies],
                }
            if self.state_type == "map":
                st["map"] = self.map_data
            if self.state_type == "rewards":
                st["rewards"] = self.rewards
            if self.state_type == "card_reward":
                st["card_reward"] = self.card_reward
            if self.state_type == "rest_site":
                st["rest_site"] = self.rest
            if self.state_type == "shop":
                st["shop"] = self.shop
            if self.state_type == "treasure":
                st["treasure"] = self.treasure
            if self.state_type == "event":
                st["event"] = self.event
            if self.state_type == "card_select":
                st["card_select"] = self.card_select
            if self.state_type == "relic_select":
                st["relic_select"] = self.relic_select
            if self.state_type == "hand_select":
                st["hand_select"] = self.hand_select
            if self.state_type == "crystal_sphere" and self.crystal:
                csp = dict(self.crystal)
                csp["clickable_cells"] = [
                    {"x": c["x"], "y": c["y"]} for c in self.crystal["cells"] if not c["revealed"]
                ]
                st["crystal_sphere"] = csp
            if self.state_type == "fake_merchant":
                st["shop"] = self.shop
                st["fake_merchant"] = self.shop
            if self.state_type == "game_over":
                st["game_over"] = self.game_over
            return st


class GameError(Exception):
    pass


GAME = Game()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静点
        pass

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(200, {"message": "Hello from mock STS2 MCP v0.3.4", "status": "ok"})
            return
        if self.path.startswith("/api/v1/singleplayer"):
            self._send(200, GAME.snapshot())
            return
        if self.path == "/debug":
            self._send(200, GAME.snapshot())
            return
        if self.path == "/reset":
            with GAME.lock:
                GAME.reset()
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"status": "error", "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/api/v1/singleplayer"):
            self._send(404, {"status": "error", "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"status": "error", "error": "bad json"})
            return
        action = body.pop("action", None)
        if not action:
            self._send(400, {"status": "error", "error": "missing action"})
            return
        try:
            msg = GAME.act(action, body)
        except GameError as e:
            self._send(200, {"status": "error", "error": str(e)})
            return
        except TypeError as e:
            self._send(200, {"status": "error", "error": f"bad params: {e}"})
            return
        self._send(200, {"status": "ok", "message": msg})


def create_server(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8811)
    args = ap.parse_args()
    server = create_server(args.port)
    print(f"mock STS2 MCP listening on http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()


