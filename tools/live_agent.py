#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机驱动脚本：直接对着 CharTyr/STS2-Agent 的本地 HTTP API 打牌。

用途是验证「状态级接入」在真游戏里能不能跑通，不依赖 AstrBot。
决策用的是简化规则（能打就打、打不动就结束回合、奖励/选路/事件选第一个能选的），
真正的策略在 AstrBot 插件里（RuleBot）。

用法：
    python tools/live_agent.py --steps 30 --verbose
    python tools/live_agent.py --api http://127.0.0.1:8080 --continue-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:8080"


def _req(url: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": {"code": f"http_{exc.code}", "message": body[:400]}}


class Agent:
    def __init__(self, api: str) -> None:
        self.api = api.rstrip("/")

    def health(self) -> dict:
        return _req(self.api + "/health")

    def state(self) -> dict:
        resp = _req(self.api + "/state")
        if not resp.get("ok"):
            raise RuntimeError(f"state failed: {resp}")
        return resp["data"]

    def act(self, action: str, **params) -> dict:
        payload = {"action": action}
        payload.update({k: v for k, v in params.items() if v is not None})
        return _req(self.api + "/action", payload)


# --------------------------------------------------------------------------- #
# 简化规则
# --------------------------------------------------------------------------- #

_NUM = re.compile(r"-?\d+")


def _first_int(text: str) -> int:
    m = _NUM.search(text or "")
    return int(m.group()) if m else 0


def _card_damage(card: dict) -> int:
    """从卡牌文本里估一个伤害值（含 X 段攻击）。"""
    text = (card.get("resolved_rules_text") or card.get("rules_text") or "").lower()
    values = {str(dv.get("name", "")).lower(): dv.get("current_value") for dv in card.get("dynamic_values") or []}
    dmg = 0
    for key in ("damage", "damage_per_hit"):
        if key in values and isinstance(values[key], (int, float)):
            dmg = max(dmg, int(values[key]))
    if not dmg:
        m = re.search(r"deal\s+(\d+)\s+damage", text)
        if m:
            dmg = int(m.group(1))
        m2 = re.search(r"(\d+)\s+damage", text)
        if not dmg and m2:
            dmg = int(m2.group(1))
    hits = 1
    m3 = re.search(r"damage\s+(\d+)\s+times", text) or re.search(r"(\d+)\s+times", text)
    if m3:
        hits = int(m3.group(1))
    if "x" in str(card.get("energy_cost", "")):
        hits *= 1
    return dmg * hits


def _card_block(card: dict) -> int:
    text = (card.get("resolved_rules_text") or card.get("rules_text") or "").lower()
    values = {str(dv.get("name", "")).lower(): dv.get("current_value") for dv in card.get("dynamic_values") or []}
    if "block" in values and isinstance(values["block"], (int, float)):
        return int(values["block"])
    m = re.search(r"(\d+)\s+block", text)
    return int(m.group(1)) if m else 0


def _incoming(enemies: list[dict]) -> int:
    total = 0
    for foe in enemies:
        if not foe.get("is_alive", True):
            continue
        for intent in foe.get("intents") or []:
            if intent.get("intent_type") != "Attack":
                continue
            dmg = intent.get("total_damage") or intent.get("damage")
            if dmg is None:
                dmg = _first_int(intent.get("label", ""))
            total += int(dmg or 0)
    return total


def _pick_card(state: dict) -> tuple[int, int | None] | None:
    combat = state.get("combat") or {}
    hand = [c for c in combat.get("hand") or [] if c.get("playable")]
    if not hand:
        return None
    me = combat.get("player") or {}
    hp = int(me.get("current_hp") or 0)
    block = int(me.get("block") or 0)
    energy = int(me.get("energy") or 0)
    need = max(0, _incoming(combat.get("enemies") or []) - block)

    best: tuple[float, int, int | None] | None = None
    for card in hand:
        cost = card.get("energy_cost")
        cost = int(cost) if isinstance(cost, (int, float)) else 1
        if card.get("costs_x"):
            cost = max(1, energy)
        if cost > energy:
            continue
        ctype = (card.get("card_type") or "").lower()
        dmg = _card_damage(card)
        blk = _card_block(card)
        score = 0.0
        if dmg:
            score = dmg / max(1, cost) * 10.0
            alive = [e for e in combat.get("enemies") or [] if e.get("is_alive", True)]
            for foe in alive:
                if int(foe.get("current_hp") or 0) <= dmg:
                    score = 500.0
                    break
        elif blk:
            score = min(blk, need) / max(1, cost) * 10.0
            if blk >= need:
                score += 8.0
            if need == 0:
                score *= 0.3
        elif "power" in ctype or "能力" in ctype:
            score = 9.0
        else:
            score = 3.0
        if score > (best[0] if best else 0.0):
            best = (score, int(card["index"]), _pick_target(card, combat, dmg))

    if best is None or best[0] < 1.0:
        return None
    return best[1], best[2]


def _pick_target(card: dict, combat: dict, dmg: int) -> int | None:
    if not card.get("requires_target"):
        return None
    space = card.get("target_index_space") or ""
    if "players" in space:
        return None
    alive = [e for e in combat.get("enemies") or [] if e.get("is_alive", True)]
    if not alive:
        return None
    valid = card.get("valid_target_indices")
    if isinstance(valid, list) and valid:
        alive = [e for e in alive if e.get("index") in valid] or alive
    killable = [e for e in alive if int(e.get("current_hp") or 0) <= dmg]
    pool = killable or alive
    pool.sort(key=lambda e: (int(e.get("current_hp") or 0), -_incoming([e])))
    return int(pool[0]["index"])


def decide(state: dict) -> tuple[str, dict] | None:
    screen = state.get("screen")
    actions = set(state.get("available_actions") or [])

    if screen == "COMBAT":
        readiness = (state.get("combat") or {}).get("action_readiness") or {}
        if readiness and not readiness.get("can_use_combat_actions", True):
            return None  # 等
        pick = _pick_card(state)
        if pick:
            card_index, target = pick
            return "play_card", {"card_index": card_index, "target_index": target}
        if "end_turn" in actions:
            return "end_turn", {}
        return None

    if screen == "MAP":
        nodes = (state.get("map") or {}).get("available_nodes") or []
        if nodes:
            def node_score(n: dict) -> float:
                kind = (n.get("node_type") or "").lower()
                return {"elite": 1.0, "monster": 2.0, "event": 3.0, "shop": 3.5, "treasure": 2.5, "rest": 4.0, "boss": 5.0}.get(kind, 2.0)
            nodes = sorted(nodes, key=node_score)
            return "choose_map_node", {"option_index": int(nodes[0]["index"])}
        return None

    if screen == "REWARD":
        reward = state.get("reward") or {}
        if reward.get("pending_card_choice"):
            cards = reward.get("card_options") or []
            if cards:
                best = max(cards, key=lambda c: _card_value(c))
                return "choose_reward_card", {"option_index": int(best["index"])}
            if "skip_reward_cards" in actions:
                return "skip_reward_cards", {}
            return None
        claimable = [r for r in reward.get("rewards") or [] if r.get("claimable")]
        if claimable:
            return "claim_reward", {"option_index": int(claimable[0]["index"])}
        if "collect_rewards_and_proceed" in actions:
            return "collect_rewards_and_proceed", {}
        if "proceed" in actions:
            return "proceed", {}
        return None

    if screen == "EVENT":
        options = [o for o in (state.get("event") or {}).get("options") or [] if not o.get("is_locked")]
        if options:
            return "choose_event_option", {"option_index": int(options[0]["index"])}
        return None

    if screen in ("REST",):
        options = [o for o in (state.get("rest") or {}).get("options") or [] if o.get("is_enabled")]
        if options:
            return "choose_rest_option", {"option_index": int(options[0]["index"])}
        if "proceed" in actions:
            return "proceed", {}
        return None

    if screen == "CHEST":
        chest = state.get("chest") or {}
        if not chest.get("is_opened") and "open_chest" in actions:
            return "open_chest", {}
        options = chest.get("relic_options") or []
        if options and not chest.get("has_relic_been_claimed"):
            return "choose_treasure_relic", {"option_index": int(options[0]["index"])}
        if "proceed" in actions:
            return "proceed", {}
        return None

    if screen == "SHOP":
        shop = state.get("shop") or {}
        if not shop.get("is_open") and "open_shop_inventory" in actions:
            return "open_shop_inventory", {}
        if "close_shop_inventory" in actions:
            return "close_shop_inventory", {}
        if "proceed" in actions:
            return "proceed", {}
        return None

    if screen == "CARD_SELECTION":
        cards = (state.get("selection") or {}).get("cards") or []
        if cards:
            return "select_deck_card", {"option_index": int(cards[0]["index"])}
        return None

    if screen == "GAME_OVER":
        over = state.get("game_over") or {}
        if over.get("can_continue"):
            return "continue_game_over", {}
        if over.get("can_return_to_main_menu"):
            return "return_to_main_menu", {}
        return None

    if screen == "MAIN_MENU":
        if "continue_run" in actions:
            return "continue_run", {}
        return None

    if "proceed" in actions:
        return "proceed", {}
    return None


def _card_value(card: dict) -> float:
    rarity = (card.get("rarity") or "").lower()
    score = {"rare": 30.0, "uncommon": 20.0, "common": 12.0}.get(rarity, 12.0)
    ctype = (card.get("card_type") or "").lower()
    if "power" in ctype:
        score += 6.0
    if "attack" in ctype:
        score += 4.0
    if "curse" in ctype or "status" in ctype:
        score -= 40.0
    score += min(8.0, _card_damage(card) / 6.0)
    return score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--interval", type=float, default=0.6)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--continue-run", action="store_true", help="主菜单时点「继续游戏」")
    args = ap.parse_args()

    agent = Agent(args.api)
    health = agent.health()
    if not health.get("ok"):
        print("API 不可用：", health)
        return 2
    info = health["data"]
    print(f"STS2 AI Agent {info.get('mod_version')} / 游戏 {info.get('game_version')} / port {info.get('api_port')}")

    if args.continue_run:
        print("继续存档：", json.dumps(agent.act("continue_run"), ensure_ascii=False)[:300])
        time.sleep(2)

    idle = 0
    for step in range(args.steps):
        try:
            state = agent.state()
        except Exception as exc:  # noqa: BLE001
            print(f"读状态失败：{exc}")
            return 2
        screen = state.get("screen")
        turn = state.get("turn")
        plan = decide(state)
        if plan is None:
            idle += 1
            if args.verbose:
                print(f"[{step}] {screen} turn={turn} -> 等待 ({idle})")
            # 战斗里动画/敌人回合会让战斗动作暂时不可用，要耐心等；
            # 其它界面连续几次都没动作，基本就是卡住了。
            patience = 90 if screen == "COMBAT" else 8
            if idle >= patience:
                print(f"{screen} 连续 {idle} 次无事可做，停下")
                return 0
            time.sleep(max(args.interval, 1.0) if screen == "COMBAT" else 2.0)
            continue
        idle = 0
        action, params = plan
        before = f"{screen} turn={turn}"
        resp = agent.act(action, **params)
        ok = resp.get("ok")
        payload = resp.get("data") or {}
        err = (resp.get("error") or {}).get("message", "")
        print(f"[{step}] {before} -> {action} {params} :: {'ok' if ok else 'FAIL'} {payload.get('status','')} {err[:120]}")
        if not ok and err:
            time.sleep(1.0)
        time.sleep(args.interval)

    final = agent.state()
    print(f"结束：screen={final.get('screen')} floor={(final.get('run') or {}).get('floor')} "
          f"hp={(final.get('run') or {}).get('current_hp')}/{(final.get('run') or {}).get('max_hp')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
