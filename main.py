# -*- coding: utf-8 -*-
"""astrbot_plugin_sts2_player —— 让 AstrBot 里的 AI 去玩《杀戮尖塔 2》。

数据通路：

    AstrBot 进程                     游戏进程（Slay the Spire 2）
    ┌───────────────────────┐        ┌──────────────────────────────┐
    │ 本插件                │  HTTP  │ STS2_MCP mod                 │
    │  · 规则机器人         │ <────> │  localhost:15526             │
    │  · 可选的 LLM 决策    │  JSON  │  读引擎状态 / 调游戏 API     │
    └───────────────────────┘        └──────────────────────────────┘

为什么这么分层：
  · 一局《杀戮尖塔 2》有几百个决策点，全部丢给 LLM 会烧掉百万级 token
    （STS2MCP 作者实测整局 Claude Sonnet 4.6 约 8M token）。
  · 战斗里的“打哪张牌、打谁”是可以用启发式做到八成水平的，
    交给规则机器人：零 token、毫秒级、不会抽风。
  · 只有在“值得想一下”的界面（选牌、事件、商店、路线、Boss 前）才问模型。

指令：
  /sts2                    当前对局快照（压缩后的可读文本）
  /sts2 自动 [n]            自动推进 n 步（默认 10，含战斗）
  /sts2 停                  停下自动推进
  /sts2 执行 <action> [json] 直接调一个 API 动作
  /sts2 帮助                动作表

llm_tool：sts2_state / sts2_action —— 主 LLM 在聊天里也能直接看和操作。
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from typing import Any, AsyncGenerator

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_URL = "http://127.0.0.1:15526"

CARD_TYPE_CN = {
    "Attack": "攻击",
    "Skill": "技能",
    "Power": "能力",
    "Status": "状态",
    "Curse": "诅咒",
}

NODE_CN = {
    "Start": "起点",
    "Monster": "战斗",
    "Elite": "精英",
    "RestSite": "营火",
    "Shop": "商店",
    "Treasure": "宝箱",
    "Event": "事件",
    "Boss": "Boss",
    "Unknown": "未知",
}

SCREEN_CN = {
    "menu": "主菜单",
    "unknown": "未知界面",
    "overlay": "弹窗",
    "monster": "战斗",
    "elite": "精英战",
    "boss": "Boss 战",
    "hand_select": "选牌（战斗中）",
    "rewards": "战斗奖励",
    "card_reward": "选一张牌",
    "map": "地图",
    "event": "事件",
    "rest_site": "营火",
    "shop": "商店",
    "fake_merchant": "假商人",
    "treasure": "宝箱",
    "card_select": "选牌（牌堆）",
    "bundle_select": "选牌包",
    "relic_select": "选遗物",
    "crystal_sphere": "水晶球",
    "game_over": "游戏结束",
}

COMBAT_SCREENS = ("monster", "elite", "boss")

# 只在这些界面上问模型。菜单/浮层/结算这种「按钮在哪」的事情规则层一次就对，
# 交给模型反而会被它编出 menu_select 这种老后端才有的动作。
LLM_SCREENS = COMBAT_SCREENS + (
    "map",
    "rewards",
    "card_reward",
    "event",
    "shop",
    "fake_merchant",
    "rest_site",
    "treasure",
    "card_select",
    "bundle_select",
    "relic_select",
    "crystal_sphere",
)

# 每个界面只把这几个动作喂给模型，省 token 也少给幻觉留缝。
SCREEN_ACTIONS: dict[str, str] = {
    "monster": "play_card / end_turn / use_potion",
    "elite": "play_card / end_turn / use_potion",
    "boss": "play_card / end_turn / use_potion",
    "map": "choose_map_node",
    "rewards": "claim_reward / select_card_reward / skip_card_reward / resolve_rewards / proceed",
    "card_reward": "select_card_reward / skip_card_reward",
    "event": "choose_event_option / advance_dialogue",
    "shop": "open_shop / shop_purchase / close_shop / proceed",
    "fake_merchant": "open_shop / shop_purchase / close_shop / proceed",
    "rest_site": "choose_rest_option / proceed",
    "treasure": "open_chest / claim_treasure_relic / proceed",
    "card_select": "select_card / confirm_selection / cancel_selection",
    "bundle_select": "select_bundle / confirm_bundle_selection / cancel_bundle_selection",
    "relic_select": "select_relic / skip_relic_selection",
    "crystal_sphere": "crystal_sphere_set_tool / crystal_sphere_click_cell / crystal_sphere_proceed",
}

# 「离开这个界面」在不同后端有不同叫法，按可用性挑一个。
LEAVE_SUBSTITUTES = (
    "proceed",
    "resolve_rewards",
    "collect_rewards_and_proceed",
    "close_shop_inventory",
    "continue_game_over",
)
LEAVE_ACTIONS = {
    "proceed": "proceed",
    "resolve_rewards": "resolve_rewards",
    "collect_rewards_and_proceed": "proceed",
    "close_shop_inventory": "close_shop",
    "continue_game_over": "continue_game_over",
}

ACTION_HELP = """动作（POST 到 mod 的 JSON）
  play_card            {"card_index":0,"target":"JAW_WORM_0"}   打一张手牌
  end_turn             {}                                       结束回合
  use_potion           {"slot":0,"target":"..."}               喝药水
  discard_potion       {"slot":0}
  claim_reward         {"index":0}                             领取战斗奖励
  select_card_reward   {"card_index":0} / skip_card_reward {}
  proceed              {}                                      奖励/营火/商店/宝箱 → 地图
  choose_map_node      {"index":0}
  choose_event_option  {"index":0} / advance_dialogue {}
  choose_rest_option   {"index":0}
  shop_purchase        {"index":0}
  select_card          {"index":0} / confirm_selection {} / cancel_selection {}
  select_bundle        {"index":0} / confirm_bundle_selection {} / cancel_bundle_selection {}
  select_relic         {"index":0} / skip_relic_selection {}
  claim_treasure_relic {"index":0}
  combat_select_card   {"card_index":0} / combat_confirm_selection {}
  crystal_sphere_click_cell {"x":4,"y":7} / crystal_sphere_set_tool {"tool":"big"}
  menu_select          {"option":"singleplayer"}"""


# ---------------------------------------------------------------------------
# 与 STS2_MCP mod 的 HTTP 通道
# ---------------------------------------------------------------------------
class Sts2Error(Exception):
    """给用户看的、已经翻译过的错误。"""


class Sts2Client:
    """STS2_MCP 的极简异步客户端（只依赖 aiohttp）。"""

    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._mode: str | None = None
        self.last_raw: dict | None = None  # "agent"（STS2-Agent）或 "mcp"（STS2_MCP）

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _open(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _json(
        self, method: str, path: str, payload: dict | None = None, *, base: str | None = None
    ) -> tuple[int, dict]:
        """不把 HTTP 状态码当异常：STS2-Agent 用 4xx + JSON 体表达业务错误。"""
        session = await self._open()
        url = f"{base or self.base_url}{path}"
        try:
            async with session.request(method, url, json=payload) as resp:
                text = await resp.text()
                try:
                    return resp.status, json.loads(text)
                except json.JSONDecodeError:
                    return resp.status, {"raw": text[:400]}
        except aiohttp.ClientError as e:
            raise Sts2Error(self._conn_hint(url, e)) from e
        except asyncio.TimeoutError as e:
            raise Sts2Error(f"等游戏响应超时（{self.timeout}s），游戏可能卡住了。") from e

    def _conn_hint(self, url: str, e: BaseException) -> str:
        return (
            f"连不上《杀戮尖塔 2》的 mod 接口（{url}）。\n"
            "依次检查：\n"
            "  1. 游戏已启动，并且进到了主菜单（mod 在游戏里跑起来就会有接口）；\n"
            "  2. <游戏目录>/mods/ 里有 STS2AIAgent.dll、STS2AIAgent.pck、STS2AIAgent.json；\n"
            "  3. 游戏设置 → Mods 已启用「STS2 AI Agent」；\n"
            "  4. 防火墙放行了本地回环。\n"
            f"原始错误：{type(e).__name__}: {e}"
        )

    async def detect(self) -> str:
        """先探一次 /health，认清后端是 STS2-Agent 还是老的 STS2_MCP。

        两个后端的默认端口不同（STS2-Agent 8080 / STS2_MCP 15526），配置写错一个数字
        就整晚连不上，所以这里顺手把另一个端口也试一下，连上了就自动改过来。
        """
        if self._mode:
            return self._mode
        try:
            status, data = await self._json("GET", "/health")
        except Sts2Error:
            for alt in self._alt_urls():
                try:
                    status, data = await self._json_at(alt, "GET", "/health")
                except Sts2Error:
                    continue
                logger.info("STS2 接口在 %s 上，自动切过去。", alt)
                self.base_url = alt
                break
            else:
                self._mode = "mcp"
                return self._mode
        inner = data.get("data") if isinstance(data, dict) else None
        if status == 200 and isinstance(inner, dict) and str(
            inner.get("service", "")
        ).startswith("sts2-ai-agent"):
            self._mode = "agent"
        else:
            self._mode = "mcp"
        logger.info("STS2 后端识别为：%s", self._mode)
        return self._mode

    def _alt_urls(self) -> list[str]:
        out: list[str] = []
        for port in ("8080", "15526"):
            url = f"http://127.0.0.1:{port}"
            if url != self.base_url:
                out.append(url)
        return out

    async def _json_at(
        self, base: str, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        return await self._json(method, path, payload, base=base)

    async def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        session = await self._open()
        url = f"{self.base_url}{path}"
        try:
            async with session.request(method, url, json=payload, params=params) as resp:
                text = await resp.text()
                if resp.status == 409:
                    raise Sts2Error(
                        "游戏不在单人模式（mod 返回 HTTP 409）。"
                        "请先回到单人模式再操作。"
                    )
                if resp.status >= 400:
                    raise Sts2Error(f"游戏返回 HTTP {resp.status}：{text[:200]}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise Sts2Error(f"游戏返回了非 JSON 内容：{text[:200]}") from e
        except aiohttp.ClientError as e:
            raise Sts2Error(
                f"连不上《杀戮尖塔 2》的 mod 接口（{url}）。\n"
                "依次检查：\n"
                "  1. 游戏已启动，并且进到了单人模式（不是主菜单）；\n"
                "  2. <游戏目录>/mods/ 里有 STS2_MCP.dll 和 STS2_MCP.json；\n"
                "  3. 游戏设置 → Mods 已启用；\n"
                "  4. 防火墙放行了本地回环。\n"
                f"原始错误：{type(e).__name__}: {e}"
            ) from e
        except asyncio.TimeoutError as e:
            raise Sts2Error(f"等游戏响应超时（{self.timeout}s），游戏可能卡住了。") from e

    async def health(self) -> bool:
        mode = await self.detect()
        if mode == "agent":
            try:
                status, data = await self._json("GET", "/health")
            except Sts2Error:
                return False
            inner = data.get("data") or {}
            return status == 200 and str(inner.get("status", "")).lower() in (
                "ready",
                "running",
                "ok",
            )
        try:
            data = await self.request("GET", "/")
        except Sts2Error:
            return False
        return str(data.get("status", "")).lower() == "ok"

    async def state(self, fmt: str = "json") -> dict:
        mode = await self.detect()
        if mode == "agent":
            status, data = await self._json("GET", "/state")
            if status != 200 or not data.get("ok"):
                err = data.get("error") or {}
                raise Sts2Error(
                    f"读游戏状态失败：{err.get('code') or status} "
                    f"{err.get('message') or str(data)[:160]}"
                )
            payload = data.get("data") or {}
            # 留着原始那一坨，调试界面形状时用得上（见 tools/live_plugin.py）
            self.last_raw = payload
            return agent_to_legacy(payload)
        return await self.request(
            "GET", "/api/v1/singleplayer", params={"format": fmt}
        )

    async def act(self, action: str, **params: Any) -> dict:
        mode = await self.detect()
        if mode == "agent":
            return await self._agent_act(action, params)
        body: dict[str, Any] = {"action": action}
        body.update({k: v for k, v in params.items() if v is not None})
        return await self.request("POST", "/api/v1/singleplayer", payload=body)

    async def _agent_act(self, action: str, params: dict) -> dict:
        native, payload = agent_action(action, params)
        if native is None:
            return {"status": "ok", "message": "（这一步只是等待）"}
        status, data = await self._post_action(native, payload)
        # 同一个“继续”在不同界面叫不同名字：拿错误里的可用动作表就地纠一次
        if not data.get("ok") and native == "proceed":
            err = data.get("error") or {}
            avail = (err.get("details") or {}).get("available_actions") or []
            for cand in (
                "resolve_rewards",
                "collect_rewards_and_proceed",
                "close_shop_inventory",
                "continue_game_over",
            ):
                if cand in avail:
                    logger.info("proceed 不可用，改用 %s", cand)
                    status, data = await self._post_action(cand, {})
                    break
        if not data.get("ok"):
            err = data.get("error") or {}
            return {
                "status": "error",
                "error": f"{err.get('code') or status}: {err.get('message') or ''}".strip(),
            }
        d = data.get("data") or {}
        st = str(d.get("status") or "completed")
        if st == "failed":
            return {"status": "error", "error": d.get("message") or "动作执行失败"}
        return {"status": "ok", "message": d.get("message") or st}

    async def _post_action(self, native: str, payload: dict) -> tuple[int, dict]:
        body: dict[str, Any] = {"action": native}
        body.update({k: v for k, v in payload.items() if v is not None})
        return await self._json("POST", "/action", payload=body)


# ---------------------------------------------------------------------------
# 后端适配：STS2-Agent（CharTyr/STS2-Agent 的后端）→ 本插件内部的 STS2_MCP 形状
# ---------------------------------------------------------------------------
_AGENT_SCREEN = {
    "MAP": "map",
    "REWARD": "rewards",
    "CHEST": "treasure",
    "EVENT": "event",
    "REST": "rest_site",
    "SHOP": "shop",
    "FAKE_MERCHANT": "shop",
    "CARD_SELECTION": "card_select",
    "BUNDLE_SELECTION": "bundle_select",
    "CRYSTAL_SPHERE": "crystal_sphere",
    "COMBAT": "monster",
    "GAME_OVER": "game_over",
    "MAIN_MENU": "menu",
    "CHARACTER_SELECT": "menu",
    "TIMELINE": "menu",
    "UNLOCK": "menu",
    "MODAL": "overlay",
    "PAUSE_MENU": "overlay",
    "CARDS_VIEW": "overlay",
    "CARD_PILE": "overlay",
    "SETTINGS": "overlay",
}

_AGENT_NODE = {
    "Rest": "RestSite",
    "RestSite": "RestSite",
    "Shop": "Shop",
    "Event": "Event",
    "Treasure": "Treasure",
    "Elite": "Elite",
    "Boss": "Boss",
    "Monster": "Monster",
}


def _act_no(raw: Any) -> int:
    """act_id 在 v0.111 上是字符串（"0" 表示第一幕），转成给人看的 1 起编号。"""
    try:
        return int(str(raw).strip()) + 1
    except (TypeError, ValueError):
        m = re.search(r"\d+", str(raw or ""))
        return int(m.group()) + 1 if m else 1


def _card_kind(text: str, card_id: str = "") -> str:
    """v0.111 的手牌没有 card_type 字段，只能从规则文本判断类别。

    注意别被「对你造成的伤害降低 50%」这类描述骗了：必须是「造成 N 点伤害」才算攻击牌。
    """
    low = f"{text} {card_id}".lower()
    if re.search(r"造成[^。]{0,12}?\d+\s*点伤害", text) or re.search(
        r"deal\s+\d+\s+damage", low
    ):
        return "Attack"
    if any(k in low for k in ("每回合", "每当", "能力", "power", "获得力量")):
        return "Power"
    return "Skill"


def agent_to_legacy(d: dict) -> dict:
    """把 STS2-Agent 的 /state 压成本插件原本吃的那套字段。"""
    screen = str(d.get("screen") or "")
    run = d.get("run") or {}
    combat = d.get("combat") or {}
    cp = combat.get("player") or {}
    readi = combat.get("action_readiness") or {}
    in_combat = bool(combat) and screen == "COMBAT"

    potions = []
    for p in run.get("potions") or []:
        if not p.get("occupied", True):
            continue
        potions.append(
            {
                "slot": p.get("index"),
                "id": p.get("potion_id"),
                "name": p.get("name"),
                "description": p.get("description") or "",
                "target_type": p.get("target_type") or "None",
                "can_use_in_combat": bool(p.get("can_use")),
                "can_discard": bool(p.get("can_discard")),
            }
        )

    hand = []
    for c in combat.get("hand") or []:
        desc = c.get("resolved_rules_text") or c.get("rules_text") or ""
        hand.append(
            {
                "index": c.get("index"),
                "id": c.get("card_id"),
                "name": c.get("name"),
                "type": _card_kind(desc, str(c.get("card_id") or "")),
                "cost": c.get("energy_cost"),
                "star_cost": c.get("star_cost"),
                "description": desc,
                "target_type": c.get("target_type") or "None",
                # 真机有种牌 target_type 看着是 None、但 requires_target 是 true
                # （比如「挑衅」：格挡 + 易伤）。只信 target_type 就会漏传目标，
                # 引擎回 invalid_target: This card requires target_index.
                "requires_target": bool(c.get("requires_target")),
                "valid_target_indices": c.get("valid_target_indices") or [],
                "can_play": bool(c.get("playable", True)),
                "unplayable_reason": c.get("unplayable_reason"),
                "is_upgraded": bool(c.get("upgraded")),
                # 引擎算好的真值（含力量/虚弱），给渲染层显示「实际」数值
                "dynamic_values": c.get("dynamic_values") or [],
                "resolved_damage": _resolved_damage(c),
                "resolved_block": _resolved_block(c),
            }
        )

    if in_combat:
        hp = cp.get("current_hp") or 0
        max_hp = cp.get("max_hp") or 0
        block = cp.get("block") or 0
        energy = cp.get("energy") or 0
    else:
        hp = run.get("current_hp") or 0
        max_hp = run.get("max_hp") or 0
        block = 0
        energy = run.get("max_energy") or 3

    deck = []
    for c in run.get("deck") or []:
        cdesc = c.get("resolved_rules_text") or c.get("rules_text") or c.get("description") or ""
        deck.append(
            {
                "id": c.get("card_id") or c.get("id"),
                "name": c.get("name"),
                "type": str(c.get("card_type") or ""),
                "cost": c.get("energy_cost", c.get("cost")),
                "description": cdesc,
                "rarity": c.get("rarity") or "Common",
                "is_upgraded": bool(c.get("upgraded")),
            }
        )

    player = {
        "character": run.get("character_name") or run.get("character_id"),
        "hp": hp,
        "max_hp": max_hp,
        "block": block,
        "energy": energy,
        "max_energy": run.get("max_energy") or 3,
        "gold": run.get("gold") or 0,
        "max_potion_slots": 3,
        "status": cp.get("powers") or [],
        "relics": run.get("relics") or [],
        "potions": potions,
        "hand": hand,
        # 整副牌：构筑层（选牌/升级/删牌）全看它
        "deck": deck,
    }

    out: dict[str, Any] = {
        "state_type": _AGENT_SCREEN.get(screen, "unknown"),
        "run": {
            "act": _act_no(run.get("act_id")),
            "floor": run.get("floor") or 0,
            "ascension": run.get("ascension") or 0,
            "character": run.get("character_name") or run.get("character_id"),
            "gold": run.get("gold") or 0,
        },
        "player": player,
        "screen": screen,
        "available_actions": d.get("available_actions") or [],
    }

    if screen == "COMBAT":
        enemies = []
        for e in combat.get("enemies") or []:
            intents = []
            for it in e.get("intents") or []:
                label = it.get("label")
                if not label and it.get("total_damage"):
                    label = str(it.get("total_damage"))
                intents.append(
                    {
                        "type": it.get("intent_type"),
                        "label": label,
                        "title": it.get("intent_type") or "",
                        "description": "",
                    }
                )
            enemies.append(
                {
                    "entity_id": str(e.get("index")),
                    "combat_id": e.get("enemy_id"),
                    "name": e.get("name"),
                    "hp": e.get("current_hp") or 0,
                    "max_hp": e.get("max_hp") or 0,
                    "block": e.get("block") or 0,
                    "status": e.get("powers") or [],
                    "intents": intents,
                }
            )
        out["battle"] = {
            "round": d.get("turn") or 1,
            "turn": "player" if readi.get("player_action_phase") else "enemy",
            "is_play_phase": bool(readi.get("can_use_combat_actions"))
            and bool(readi.get("player_action_phase")),
            # 游戏自己算过「这回合结束会死」，是最可靠的保命信号
            "lethal": bool(combat.get("end_turn_will_kill_player")),
            "enemies": enemies,
        }

    if screen == "MAP":
        m = d.get("map") or {}
        out["map"] = {
            "current_position": m.get("current_node") or {},
            "next_options": [
                {
                    "index": n.get("index"),
                    "col": n.get("col"),
                    "row": n.get("row"),
                    "type": _AGENT_NODE.get(str(n.get("node_type")), "Monster"),
                }
                for n in m.get("available_nodes") or []
            ],
            "boss": (m.get("boss_node") or {}),
        }

    if screen == "REWARD":
        r = d.get("reward") or {}
        if r.get("pending_card_choice"):
            out["state_type"] = "card_reward"
            out["card_reward"] = {
                "cards": [
                    {
                        "index": c.get("index"),
                        "id": c.get("card_id"),
                        "name": c.get("name"),
                        "type": _card_kind(
                            c.get("resolved_rules_text") or c.get("rules_text") or "",
                            str(c.get("card_id") or ""),
                        ),
                        "cost": c.get("energy_cost"),
                        "description": c.get("resolved_rules_text") or "",
                        "rarity": c.get("rarity") or "Common",
                        "is_upgraded": bool(c.get("upgraded")),
                    }
                    for c in r.get("card_options") or []
                ],
                "can_skip": True,
            }
        else:
            out["rewards"] = {
                "items": [
                    {
                        "index": it.get("index"),
                        "type": str(it.get("reward_type") or "").lower(),
                        "description": it.get("description") or "",
                    }
                    for it in r.get("rewards") or []
                    if it.get("claimable", True)
                ],
                "can_proceed": bool(r.get("can_proceed")),
            }

    if screen in ("EVENT", "FAKE_MERCHANT"):
        ev = d.get("event") or {}
        out["event"] = {
            "event_id": ev.get("event_id"),
            "event_name": ev.get("title") or "",
            "is_ancient": False,
            "in_dialogue": False,
            "body": ev.get("description") or "",
            "options": [
                {
                    "index": o.get("index"),
                    "title": o.get("title") or "",
                    "description": o.get("description") or "",
                    "is_locked": bool(o.get("is_locked")),
                    "is_proceed": bool(o.get("is_proceed")),
                }
                for o in ev.get("options") or []
            ],
        }

    if screen == "REST":
        rs = d.get("rest") or d.get("rest_site") or {}
        options: list[dict] = []
        for i, o in enumerate(rs.get("options") or []):
            oid = o.get("option_id") or o.get("id") or o.get("type") or ""
            name = o.get("title") or o.get("name") or o.get("line") or ""
            if not oid:
                # 真机上万一没给 option_id，就用标题兜底认一下
                title = str(name)
                if any(k in title for k in ("休息", "回复", "治疗", "Heal", "Rest")):
                    oid = "HEAL"
                elif any(k in title for k in ("锻造", "升级", "Smith", "Upgrade")):
                    oid = "SMITH"
            index = o.get("index")
            options.append(
                {
                    "index": i if index is None else index,
                    "id": {"HEAL": "rest", "SMITH": "smith"}.get(
                        str(oid).upper(), str(oid).lower()
                    ),
                    "name": name,
                    "description": o.get("description") or "",
                    "is_enabled": bool(
                        o.get("is_enabled", o.get("enabled", o.get("can_select", True)))
                    ),
                }
            )
        out["rest_site"] = {"options": options, "can_proceed": True}

    if screen in ("SHOP", "FAKE_MERCHANT"):
        sh = d.get("shop") or {}
        items: list[dict] = []
        for cat, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
            for it in sh.get(key) or []:
                if it.get("is_stocked") is False or not it.get("name"):
                    continue
                price = it.get("price")
                if price is None:
                    price = it.get("cost") or 0
                items.append(
                    {
                        "index": it.get("index"),
                        "category": cat,
                        "price": int(price or 0),
                        "is_stocked": True,
                        "available": bool(it.get("available", True)),
                        "can_afford": bool(it.get("enough_gold", True)),
                        "name": it.get("name"),
                        "card_name": it.get("name"),
                        "card_type": str(it.get("card_type") or "")
                        or _card_kind(
                            it.get("resolved_rules_text") or it.get("rules_text") or "",
                            str(it.get("card_id") or ""),
                        ),
                        "card_description": it.get("resolved_rules_text")
                        or it.get("rules_text")
                        or "",
                        "card_rarity": it.get("rarity") or "Common",
                    }
                )
        out["shop"] = {
            "items": items,
            "is_open": bool(sh.get("is_open")),
            "can_open": bool(sh.get("can_open")),
            "can_proceed": True,
        }
        removal = sh.get("card_removal") or {}
        if removal.get("available"):
            price = removal.get("cost")
            if price is None:
                price = removal.get("price") or 0
            out["shop"]["items"].append(
                {
                    "index": 90,  # 删牌服务不是货架商品，单独给个不会撞车的号
                    # 但发给引擎时必须用引擎自己的 option_index，否则 invalid_target
                    "native_index": removal.get("index"),
                    "category": "card_removal",
                    "price": int(price or 0),
                    "is_stocked": True,
                    "name": "删牌服务",
                    "can_afford": bool(removal.get("enough_gold", True)),
                }
            )

    if screen == "CHEST":
        ch = d.get("chest") or {}
        out["treasure"] = {
            "is_opened": bool(ch.get("is_opened")),
            "relics": [
                {"index": r.get("index"), "id": r.get("relic_id"), "name": r.get("name")}
                for r in ch.get("relic_options") or []
            ],
            "can_proceed": not bool(ch.get("relic_options")),
        }

    if screen == "CARD_SELECTION":
        sel = d.get("selection") or {}
        # 多选屏：原生界面要你点够 min_select~max_select 张，点满或 confirm_selection 才收尾。
        try:
            selected = int(sel.get("selected_count") or 0)
        except (TypeError, ValueError):
            selected = 0
        try:
            max_select = int(sel.get("max_select") or 1)
        except (TypeError, ValueError):
            max_select = 1
        try:
            min_select = int(sel.get("min_select") or 0)
        except (TypeError, ValueError):
            min_select = 0
        out["card_select"] = {
            "kind": sel.get("kind"),
            "prompt": sel.get("prompt") or "",
            "selected_count": selected,
            "min_select": min_select,
            "max_select": max(1, max_select),
            "cards": [
                {
                    "index": c.get("index"),
                    "selected": bool(c.get("selected")),
                    "id": c.get("card_id"),
                    "name": c.get("name"),
                    "type": _card_kind(
                        c.get("resolved_rules_text") or c.get("rules_text") or "",
                        str(c.get("card_id") or ""),
                    ),
                    "rarity": c.get("rarity") or "Common",
                    "description": c.get("resolved_rules_text") or "",
                }
                for c in sel.get("cards") or []
            ],
            "can_confirm": bool(sel.get("can_confirm", True)),
            "can_cancel": bool(sel.get("can_cancel", False)),
        }

    if screen == "BUNDLE_SELECTION":
        out["bundle_select"] = {
            "bundles": [
                {"index": b.get("index")} for b in d.get("bundles") or []
            ],
            "preview_showing": False,
        }

    if screen == "CRYSTAL_SPHERE":
        cs = d.get("crystal_sphere") or {}
        out["crystal_sphere"] = {
            "clickable_cells": [
                {"x": c[0], "y": c[1]}
                for c in (cs.get("hidden_cells") or [])
                if isinstance(c, (list, tuple)) and len(c) == 2
            ],
            "can_proceed": bool(cs.get("is_finished")),
            "tool": cs.get("tool") or "big",
        }

    if screen == "GAME_OVER":
        g = d.get("game_over") or {}
        out["game_over"] = {
            "message": ("你赢下了这一局！" if g.get("is_victory") else "这一局结束了。")
            + f" 到达第 {g.get('floor') or 0} 层。"
        }

    return out


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def agent_action(action: str, params: dict) -> tuple[str | None, dict]:
    """本插件内部的动作名 → STS2-Agent 的动作名。返回 (None, {}) 表示只等不动。"""
    if action in ("wait", "sleep", ""):
        return None, {}
    target = _int_or_none(params.get("target"))
    if action == "play_card":
        return "play_card", {
            "card_index": _int_or_none(params.get("card_index")),
            "target_index": target,
        }
    if action == "end_turn":
        return "end_turn", {}
    if action == "use_potion":
        return "use_potion", {
            "option_index": _int_or_none(params.get("slot")),
            "target_index": target,
        }
    if action == "discard_potion":
        return "discard_potion", {"option_index": _int_or_none(params.get("slot"))}
    if action == "claim_reward":
        return "claim_reward", {"option_index": _int_or_none(params.get("index"))}
    if action == "select_card_reward":
        return "choose_reward_card", {
            "option_index": _int_or_none(params.get("card_index"))
        }
    if action == "skip_card_reward":
        return "skip_reward_cards", {}
    if action == "proceed":
        return "proceed", {}
    if action == "resolve_rewards":
        return "resolve_rewards", {}
    if action == "choose_map_node":
        return "choose_map_node", {"option_index": _int_or_none(params.get("index"))}
    if action == "choose_event_option":
        return "choose_event_option", {"option_index": _int_or_none(params.get("index"))}
    if action == "advance_dialogue":
        return "choose_event_option", {"option_index": 0}
    if action == "choose_rest_option":
        return "choose_rest_option", {"option_index": _int_or_none(params.get("index"))}
    if action == "shop_purchase":
        cat = str(params.get("category") or "card")
        if cat == "card_removal":
            # 删牌服务不是货架商品：买完直接进「从牌库选一张删掉」的界面。
            # 引擎的 card_removal 里没有 index，这个动作就是不带参数发出去。
            return "remove_card_at_shop", {}
        native = {
            "card": "buy_card",
            "relic": "buy_relic",
            "potion": "buy_potion",
        }.get(cat, "buy_card")
        return native, {"option_index": _int_or_none(params.get("index"))}
    if action == "open_shop":
        return "open_shop_inventory", {}
    if action == "close_shop":
        return "close_shop_inventory", {}
    if action == "claim_treasure_relic":
        return "choose_treasure_relic", {"option_index": _int_or_none(params.get("index"))}
    if action == "open_chest":
        return "open_chest", {}
    if action == "select_card":
        return "select_deck_card", {"option_index": _int_or_none(params.get("index"))}
    if action == "confirm_selection":
        return "confirm_selection", {}
    if action == "cancel_selection":
        return "close_cards_view", {}
    if action == "crystal_sphere_click_cell":
        return "crystal_clear_cell", {
            "x": _int_or_none(params.get("x")),
            "y": _int_or_none(params.get("y")),
            "tool": params.get("tool"),
        }
    if action == "crystal_sphere_set_tool":
        return "crystal_set_tool", {"tool": params.get("tool") or "big"}
    if action == "crystal_sphere_proceed":
        return "proceed", {}
    if action == "combat_select_card":
        return "select_deck_card", {
            "option_index": _int_or_none(params.get("card_index"))
        }
    if action == "combat_confirm_selection":
        return "confirm_selection", {}
    if action in ("continue_run", "open_character_select", "embark", "select_character"):
        return action, {
            k: _int_or_none(v)
            for k, v in params.items()
            if k in ("option_index",)
        }
    return action, {k: v for k, v in params.items() if k != "tool"}


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def _one_line(text: Any, limit: int = 80) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _first_int(text: Any) -> int:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else 0


def _cost(card: dict) -> int:
    try:
        return int(str(card.get("cost")))
    except (TypeError, ValueError):
        return 0


def _self_hp_cost(desc: str) -> int:
    """牌面上的自伤代价：Lose 6 HP / 失去 6 点生命。"""
    m = re.search(r"lose\s+(\d+)\s+hp", desc, re.IGNORECASE)
    if not m:
        m = re.search(r"失去\s*(\d+)\s*点?生命", desc)
    return int(m.group(1)) if m else 0


def _dyn(card: dict, *names: str) -> float | None:
    """从引擎给的 dynamic_values 里取值（这是「算完力量/虚弱之后」的真值）。"""
    wanted = {n.lower() for n in names}
    for item in card.get("dynamic_values") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("name") or item.get("id") or "").lower()
        if key in wanted:
            for field in ("current_value", "value", "base_value"):
                v = item.get(field)
                if isinstance(v, (int, float)):
                    return float(v)
    return None


def _resolved_damage(card: dict) -> int:
    """这张牌此刻打出去到底多少伤害：优先引擎真值，其次从文本估。"""
    dmg = _dyn(card, "damage", "Damage", "伤害")
    if dmg is None:
        return _card_damage(str(card.get("description") or ""))
    hits = _dyn(card, "hits", "Hits", "段数", "times")
    return int(round(dmg * (hits if hits and hits > 0 else 1)))


def _resolved_block(card: dict) -> int:
    block = _dyn(card, "block", "Block", "格挡")
    if block is None:
        return _card_block(str(card.get("description") or ""))
    return int(round(block))


def _card_damage(desc: str) -> int:
    """一张攻击牌的实际伤害：“造成5点伤害两次” → 10。"""
    dmg = _first_int(desc)
    if dmg <= 0:
        return 0
    low = desc.lower()
    hits = 1
    m = re.search(r"(\d+)\s*(?:times|hits|次)", low)
    if m:
        hits = max(1, int(m.group(1)))
    elif "twice" in low or "两次" in low or "2 times" in low:
        hits = 2
    elif "thrice" in low or "三次" in low or "3 times" in low:
        hits = 3
    return dmg * hits


def _applies_vulnerable(desc: Any) -> bool:
    """这张牌会不会给敌人挂「易伤」——挂完后续攻击伤害 +50%，所以值得先出。"""
    text = str(desc or "")
    low = text.lower()
    if "vulnerable" in low:
        return True
    return "易伤" in text


def _has_status(entity: dict, *names: str) -> bool:
    """敌人身上有没有某个状态。真机 powers 的形态不固定，dict/str 都认。"""
    wanted = {n.lower() for n in names}
    for item in entity.get("status") or []:
        if isinstance(item, dict):
            keys = ("name", "display_name", "status_name", "id", "status_id")
            for k in keys:
                v = item.get(k)
                if v and str(v).lower() in wanted:
                    return True
        elif str(item).lower() in wanted:
            return True
    return False


def _intent_damage(enemy: dict) -> int:
    """把意图粗估成伤害数字：攻击意图优先用 label（"11"、"6x2"、"4×2"），否则读描述。"""
    total = 0
    for it in enemy.get("intents") or []:
        kind = str(it.get("type") or "").lower()
        if "attack" not in kind and "攻击" not in kind:
            continue
        label = _strip_bbcode(it.get("label"))
        dmg = _multi_hit_total(label)
        if not dmg:
            dmg = _first_int(it.get("description"))
        total += dmg
    return total


def _strip_bbcode(text: Any) -> str:
    """游戏里的意图标签带 BBCode（4[font_size=18]×2[/font_size]），先洗掉。"""
    return re.sub(r"\[/?[^\]]*\]", "", str(text or "")).strip()


def _multi_hit_total(text: str) -> int:
    """“4×2”/“4x2”/“4*2” → 8；“11” → 11。"""
    m = re.search(r"(\d+)\s*[x×*]\s*(\d+)", text or "", re.IGNORECASE)
    if m:
        return int(m.group(1)) * int(m.group(2))
    return _first_int(text)


def _fmt_card(card: dict) -> str:
    up = "+" if card.get("is_upgraded") else ""
    ctype = CARD_TYPE_CN.get(str(card.get("type")), str(card.get("type") or ""))
    bits = [
        f"[{card.get('index')}] {card.get('name')}{up}",
        ctype,
        f"{card.get('cost')}费",
        _one_line(card.get("description"), 52),
    ]
    # 引擎算过的实际值：文本说「造成6点伤害」，实际可能是 7（力量）或 8（易伤）
    dmg = card.get("resolved_damage")
    block = card.get("resolved_block")
    if isinstance(dmg, int) and dmg > 0:
        bits.append(f"→实际{dmg}伤")
    if isinstance(block, int) and block > 0:
        bits.append(f"→实际{block}挡")
    if card.get("can_play") is False:
        bits.append(f"（{card.get('unplayable_reason') or '不可用'}）")
    return " ".join(b for b in bits if b)


def render_state(st: dict, *, pile_limit: int = 8) -> str:
    """把 mod 返回的整坨 JSON 压成模型/人一眼能读的短文本。"""
    if not isinstance(st, dict):
        return "（读不到游戏状态）"

    stype = str(st.get("state_type") or "unknown")
    run = st.get("run") or {}
    player = st.get("player") or {}

    head = f"【{SCREEN_CN.get(stype, stype)}】"
    if run:
        head += f"第{run.get('act', '?')}幕 第{run.get('floor', '?')}层"
        if run.get("ascension"):
            head += f" 进阶{run['ascension']}"
    if player:
        head += (
            f" · {player.get('character') or '?'} "
            f"{player.get('hp', '?')}/{player.get('max_hp', '?')}HP"
            f" {player.get('gold', 0)}金"
        )

    lines = [head]
    battle = st.get("battle") or {}

    if stype in COMBAT_SCREENS + ("hand_select",) and battle:
        turn = battle.get("turn")
        phase = "你的回合" if battle.get("is_play_phase") else ("敌方回合" if turn == "enemy" else "结算中")
        lines.append(
            f"⚔ 第{battle.get('round', '?')}轮 · {phase}"
            f" · 能量 {player.get('energy', 0)}/{player.get('max_energy', 0)}"
            f" · 护甲 {player.get('block', 0)}"
        )
        lines.append(
            f"  抽牌堆 {player.get('draw_pile_count', 0)}"
            f" / 弃牌堆 {player.get('discard_pile_count', 0)}"
            f" / 消耗 {player.get('exhaust_pile_count', 0)}"
        )
        if player.get("status"):
            lines.append(
                "  状态：" + "，".join(f"{p.get('name')}{p.get('amount')}" for p in player["status"])
            )
        for i, e in enumerate(battle.get("enemies") or []):
            intents = "，".join(
                f"{it.get('title') or it.get('type') or ''} {_strip_bbcode(it.get('label'))}".strip()
                for it in (e.get("intents") or [])
            )
            lines.append(
                f"  #{i} {e.get('name')} {e.get('hp')}/{e.get('max_hp')}"
                f" 护甲{e.get('block', 0)} [{e.get('entity_id')}]"
                + (f" 意图：{intents}" if intents else "")
            )
        hand = player.get("hand") or []
        # 本回合算术：敌人这回合打你多少、现有护甲够不够、手牌最狠能打多少
        incoming = sum(_intent_damage(e) for e in battle.get("enemies") or [] if e.get("hp", 0) > 0)
        if incoming or hand:
            block_now = player.get("block", 0) or 0
            best_dmg = max((c.get("resolved_damage") or 0) for c in hand) if hand else 0
            total_dmg = sum(c.get("resolved_damage") or 0 for c in hand if c.get("can_play", True))
            gap = max(0, incoming - block_now)
            lines.append(
                f"  本回合：敌人合计 {incoming} 伤 · 现有护甲 {block_now} · 还差 {gap} 点要挡"
                f" · 手牌能打 {total_dmg} 伤（最高一张 {best_dmg}）"
            )
        if hand:
            lines.append("  手牌：")
            lines.extend("    " + _fmt_card(c) for c in hand)

    if stype == "hand_select":
        hs = st.get("hand_select") or {}
        lines.append(f"  选择：{_one_line(hs.get('prompt'), 120)}（已选 {len(hs.get('selected_cards') or [])} 张）")
        for c in hs.get("cards") or []:
            lines.append("    " + _fmt_card(c))

    elif stype == "map":
        m = st.get("map") or {}
        cur = m.get("current_position") or {}
        if cur:
            lines.append(f"🗺 当前：{NODE_CN.get(cur.get('type'), cur.get('type'))}({cur.get('col')},{cur.get('row')})")
        for o in m.get("next_options") or []:
            leads = "，".join(
                NODE_CN.get(x.get("type"), str(x.get("type")))
                for x in (o.get("leads_to") or [])[:3]
            )
            lines.append(
                f"  [{o.get('index')}] {NODE_CN.get(o.get('type'), o.get('type'))}"
                f"({o.get('col')},{o.get('row')})" + (f" → {leads}" if leads else "")
            )
        boss = m.get("boss") or {}
        if boss:
            lines.append(f"  Boss：第{boss.get('row')}层 {boss.get('name')}")

    elif stype == "rewards":
        r = st.get("rewards") or {}
        for it in r.get("items") or []:
            extra = it.get("gold_amount") or it.get("potion_name") or ""
            lines.append(
                f"  [{it.get('index')}] {it.get('type')} {extra} — {_one_line(it.get('description'), 60)}"
            )
        lines.append(f"  可以离开：{r.get('can_proceed')}")

    elif stype == "card_reward":
        cr = st.get("card_reward") or {}
        for c in cr.get("cards") or []:
            lines.append(
                f"  [{c.get('index')}] {c.get('name')} {CARD_TYPE_CN.get(c.get('type'), c.get('type'))}"
                f" {c.get('cost')}费 {c.get('rarity')} — {_one_line(c.get('description'), 52)}"
            )
        lines.append(f"  可以跳过：{cr.get('can_skip')}")

    elif stype == "event":
        ev = st.get("event") or {}
        lines.append(
            f"📜 {ev.get('event_name')}（{ev.get('event_id')}）"
            + ("  [对话中，需要 advance_dialogue]" if ev.get("in_dialogue") else "")
        )
        if ev.get("body"):
            lines.append("  " + _one_line(ev["body"], 260))
        for o in ev.get("options") or []:
            tag = "锁" if o.get("is_locked") else ("→" if o.get("is_proceed") else "·")
            lines.append(
                f"  [{o.get('index')}] {tag} {o.get('title')} — {_one_line(o.get('description'), 80)}"
            )

    elif stype == "rest_site":
        for o in (st.get("rest_site") or {}).get("options") or []:
            lines.append(
                f"  [{o.get('index')}] {o.get('name')}"
                f"{'' if o.get('is_enabled', True) else '（不可用）'} — {_one_line(o.get('description'), 60)}"
            )

    elif stype in ("shop", "fake_merchant"):
        shop = st.get("shop") or {}
        if shop.get("error"):
            lines.append(f"  （商店还没准备好：{shop.get('error')}）")
        for it in shop.get("items") or []:
            price = it.get("price", it.get("cost"))
            name = (
                it.get("card_name")
                or it.get("relic_name")
                or it.get("potion_name")
                or it.get("name")
                or it.get("category")
            )
            afford = "" if it.get("can_afford", True) else " 买不起"
            lines.append(
                f"  [{it.get('index')}] {it.get('category')} {name} {price}金{afford}"
                f"{'' if it.get('is_stocked', True) else '（售罄）'}"
            )
        lines.append(f"  可以离开：{shop.get('can_proceed')}")

    elif stype == "treasure":
        t = st.get("treasure") or {}
        if t.get("message"):
            lines.append("  " + _one_line(t["message"], 80))
        for r in t.get("relics") or []:
            lines.append(
                f"  [{r.get('index')}] {r.get('name')}（{r.get('rarity')}）— {_one_line(r.get('description'), 70)}"
            )
        lines.append(f"  可以离开：{t.get('can_proceed')}")

    elif stype == "card_select":
        cs = st.get("card_select") or {}
        lines.append(f"  {_one_line(cs.get('prompt'), 140)}（{cs.get('screen_type')}）")
        for c in cs.get("cards") or []:
            lines.append("    " + _fmt_card(c))
        lines.append(f"  可确认：{cs.get('can_confirm')}　可取消：{cs.get('can_cancel')}")

    elif stype == "bundle_select":
        bs = st.get("bundle_select") or {}
        lines.append(f"  {_one_line(bs.get('prompt'), 140)}")
        for b in bs.get("bundles") or []:
            names = "，".join(str(c.get("name")) for c in (b.get("cards") or [])[:6])
            lines.append(f"  [{b.get('index')}] {b.get('card_count')} 张：{names}")

    elif stype == "relic_select":
        rs = st.get("relic_select") or {}
        lines.append(f"  {_one_line(rs.get('prompt'), 120)}")
        for r in rs.get("relics") or []:
            lines.append(
                f"  [{r.get('index')}] {r.get('name')}（{r.get('rarity')}）— {_one_line(r.get('description'), 70)}"
            )
        lines.append(f"  可以跳过：{rs.get('can_skip')}")

    elif stype == "crystal_sphere":
        csp = st.get("crystal_sphere") or {}
        cells = csp.get("clickable_cells") or []
        lines.append(
            f"  水晶球 {csp.get('grid_width')}x{csp.get('grid_height')}"
            f" · 工具 {csp.get('tool')} · 可点击 {len(cells)} 格"
        )
        for cell in cells[:12]:
            lines.append(f"    可点 ({cell.get('x')},{cell.get('y')})")
        for item in (csp.get("revealed_items") or [])[:8]:
            lines.append(f"    已揭示：{item}")
        lines.append(f"  可以离开：{csp.get('can_proceed')}")

    elif stype == "game_over":
        go = st.get("game_over") or {}
        lines.append("  " + _one_line(go.get("message"), 160))
        lines.append(f"  选项：{go.get('options')}")

    elif stype == "menu":
        lines.append(f"  界面：{st.get('menu_screen')}")
        lines.append(f"  选项：{st.get('options')}")
        if st.get("blocked_options"):
            lines.append(f"  不可用：{st.get('blocked_options')}")

    elif stype == "overlay":
        ov = st.get("overlay") or {}
        lines.append(f"  {_one_line(ov.get('message'), 200)}")

    if stype in COMBAT_SCREENS:
        lines.append("  药水：" + (
            "，".join(
                f"[{p.get('slot')}]{p.get('name')}" for p in (player.get("potions") or [])
            ) or "无"
        ))
        if player.get("relics"):
            lines.append("  遗物：" + "，".join(str(r.get("name")) for r in player["relics"][:12]))

    if stype not in COMBAT_SCREENS and player.get("relics") and stype in ("map", "rewards"):
        lines.append("  遗物：" + "，".join(str(r.get("name")) for r in player["relics"][:12]))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 规则机器人：每个界面只决定“下一步做什么”
# ---------------------------------------------------------------------------
class RuleBot:
    """零成本的启发式玩家。返回单个动作 dict，或 None 表示不知道。"""

    def __init__(self, *, potion_hp_ratio: float = 0.32, block_priority: float = 0.15,
                 card_min: float = 18.0):
        self.potion_hp_ratio = potion_hp_ratio
        self.block_priority = block_priority
        self.card_min = card_min
        # 商店里已经买失败的条目，别在同一屏里反复重试（会把「连续失败 3 次」耗光）
        self._bad_buys: set[tuple] = set()
        # 选牌屏的本地进度（有些后端不给 selected_count，只能自己数）
        self._sel_key: tuple | None = None
        self._sel_count = 0
        self._sel_picked: set = set()
        self._sel_confirmed = False
        # 模型给的「这一回合怎么打」计划；规则层负责逐步执行与校验
        self.plan: list[dict] = []
        self._plan_key: Any = None

    # -- 回合计划：模型只出计划，由规则层逐步执行 --
    def set_plan(self, steps: list[dict], st: dict) -> int:
        """装进一份计划，记下它属于哪一回合（换回合/换战斗自动作废）。"""
        battle = st.get("battle") or {}
        self._plan_key = (str(st.get("state_type") or ""), battle.get("round"))
        self.plan = [s for s in steps if isinstance(s, dict)][:8]
        return len(self.plan)

    def clear_plan(self) -> None:
        self.plan = []
        self._plan_key = None

    def _take_plan(self, st: dict) -> dict | None:
        """从计划里取下一步并校验；计划失效就丢掉，交回启发式。"""
        if not self.plan:
            return None
        battle = st.get("battle") or {}
        key = (str(st.get("state_type") or ""), battle.get("round"))
        if self._plan_key != key:
            self.clear_plan()
            return None

        player = st.get("player") or {}
        hand = [c for c in (player.get("hand") or []) if c.get("index") is not None]
        energy = int(player.get("energy") or 0)
        enemies = [e for e in (battle.get("enemies") or []) if (e.get("hp") or 0) > 0]

        step = self.plan.pop(0)
        if step.get("end_turn") or step.get("action") == "end_turn":
            self.clear_plan()
            return {"action": "end_turn"}

        # 计划里用「牌名」而不是 index —— 出一张牌之后剩下的牌会左移，index 早就废了
        name = str(step.get("card") or step.get("name") or "").strip()
        card = None
        if name:
            for c in hand:
                if (
                    str(c.get("name") or "").strip() == name
                    and c.get("can_play") is not False
                    and _cost(c) <= energy
                ):
                    card = c
                    break
        else:
            idx = step.get("card_index")
            card = next((c for c in hand if str(c.get("index")) == str(idx)), None)
            if card is not None and (card.get("can_play") is False or _cost(card) > energy):
                card = None
        if card is None:
            logger.info("计划里的「%s」现在打不出去，丢掉剩下的计划", name or step.get("card_index"))
            self.clear_plan()
            return None

        act = {"action": "play_card", "card_index": card.get("index")}
        target = self._target_for(card, enemies, step.get("target"))
        if target:
            act["target"] = str(target)
        if not self.plan:
            self.clear_plan()
        return act

    # -- 入口：把 state_type 分发到具体处理器 --
    def next_action(self, st: dict) -> dict | None:
        stype = str(st.get("state_type") or "")
        if stype not in ("shop", "fake_merchant"):
            self._bad_buys.clear()
        if stype in COMBAT_SCREENS:
            return self._combat(st)
        handler = {
            "hand_select": self._hand_select,
            "rewards": self._rewards,
            "card_reward": self._card_reward,
            "map": self._map,
            "event": self._event,
            "rest_site": self._rest,
            "shop": self._shop,
            "fake_merchant": self._leave,
            "treasure": self._treasure,
            "card_select": self._card_select,
            "bundle_select": self._bundle_select,
            "relic_select": self._relic_select,
            "crystal_sphere": self._crystal_sphere,
            "menu": self._menu,
            "overlay": self._overlay,
        }.get(stype)
        if handler is None:
            return None
        return handler(st)

    def _menu(self, st: dict) -> dict | None:
        """主菜单 / 选人 / 时间线：只有 STS2-Agent 后端会给出 available_actions。"""
        acts = [str(a) for a in st.get("available_actions") or []]
        if not acts:
            return None
        for name in (
            "continue_run",
            "embark",
            "select_character",
            "open_character_select",
            # 死了之后 continue_game_over 常常无效，退回主菜单才是正解
            "return_to_main_menu",
            "confirm_timeline_overlay",
            "close_main_menu_submenu",
            "continue_game_over",
            "confirm_unlock",
        ):
            if name == "select_character" and "embark" in acts:
                continue
            if name in acts:
                if name == "select_character":
                    return {"action": name, "option_index": 0}
                return {"action": name}
        return None

    def _overlay(self, st: dict) -> dict | None:
        acts = [str(a) for a in st.get("available_actions") or []]
        for name in ("confirm_modal", "dismiss_modal", "close_cards_view"):
            if name in acts:
                return {"action": name}
        return None

    # -- 战斗 --
    def _combat(self, st: dict) -> dict | None:
        battle = st.get("battle") or {}
        player = st.get("player") or {}
        if not battle.get("is_play_phase") or battle.get("turn") == "enemy":
            return {"action": "wait"}

        enemies = [e for e in (battle.get("enemies") or []) if (e.get("hp") or 0) > 0]
        if not enemies:
            return {"action": "wait"}

        energy = int(player.get("energy") or 0)
        block = int(player.get("block") or 0)
        incoming = sum(_intent_damage(e) for e in enemies)
        need = max(0, incoming - block)
        hp = float(player.get("hp") or 0)
        max_hp = float(player.get("max_hp") or 1)

        # 模型给了这一回合的计划就照着打（每步都重新校验）
        step = self._take_plan(st)
        if step is not None:
            return step

        # 快死了就喝药；精英/Boss 战更早喝，省着不用等于没带
        hard = str(st.get("state_type") or "") in ("elite", "boss")
        potion = self._pick_potion(player)
        # 这一回合不挡就会死：药水现在不用就永远用不上了
        lethal_turn = bool(battle.get("lethal")) or bool(
            incoming and need >= hp
        )
        if potion is not None and lethal_turn:
            act = {"action": "use_potion", "slot": potion.get("slot")}
            if str(potion.get("target_type") or "None") in ("AnyEnemy", "AllEnemies"):
                act["target"] = self._pick_target(enemies, 0)
            return act
        if potion is not None:
            pdesc = str(potion.get("description") or "")
            pdmg = _first_int(pdesc) if "damage" in pdesc.lower() or "伤害" in pdesc else 0
            # 药水能收人头就现在用，别留到死
            if pdmg and any((e.get("hp") or 0) <= pdmg for e in enemies):
                act = {"action": "use_potion", "slot": potion.get("slot")}
                if str(potion.get("target_type") or "None") in ("AnyEnemy", "AllEnemies"):
                    act["target"] = self._pick_target(enemies, pdmg)
                return act
            limit = max(self.potion_hp_ratio, 0.7 if hard else 0.0)
            if max_hp and hp <= max_hp * limit:
                act = {"action": "use_potion", "slot": potion.get("slot")}
                if str(potion.get("target_type") or "None") in ("AnyEnemy", "AllEnemies"):
                    act["target"] = self._pick_target(enemies, 0)
                return act

        best: tuple[float, dict, str | None] | None = None
        # 手牌里的护甲上限：算不清“能不能彻底挡住这一回合”，就只能判断“挡不挡得住”
        block_cap = block
        for card in player.get("hand") or []:
            if str(card.get("type") or "") != "Skill" or card.get("can_play") is False:
                continue
            if _cost(card) > energy:
                continue
            low = str(card.get("description") or "").lower()
            if any(k in low for k in ("block", "护甲", "格挡")):
                block_cap += _first_int(low)
        leak = max(0, incoming - block)
        # 挡不干净、而且这一回合要掉一半以上的血：与其慢慢磨甲，不如抢血把仗打短
        race = leak > 0 and (incoming - block_cap) > 0 and leak >= hp * 0.5
        # 这一回合不吃也能活：不能把三点能量全拿去叠甲，否则永远打不死人（僵局）
        has_attack = any(
            str(c.get("type") or "") == "Attack" and c.get("can_play") is not False and _cost(c) <= energy
            for c in player.get("hand") or []
        )
        must_mix = has_attack and hp > incoming
        # 这一手一张攻击牌都打不出去（没有/费用不够）：格挡牌留着也没别的用途
        idle = not has_attack

        # 手上的攻击总量：用来判断「先挂易伤再打」到底能多打多少伤害
        attack_total = sum(
            _card_damage(c.get("description") or "")
            for c in player.get("hand") or []
            if str(c.get("type") or "") == "Attack"
            and c.get("can_play") is not False
            and _cost(c) <= energy
        )

        # 这一回合打不死对面、手上又有易伤牌：先挂易伤，后面每张攻击牌都多打一半。
        # 只在「这场架还要打好几回合」时才值得绕路，一回合能收掉就别磨蹭。
        attack_cards = [
            c for c in player.get("hand") or []
            if str(c.get("type") or "") == "Attack"
            and c.get("can_play") is not False
            and _cost(c) <= energy
        ]
        longest = max(((e.get("hp") or 0) for e in enemies), default=0)
        if len(attack_cards) >= 2 and longest >= attack_total * 2:
            if not lethal_turn and need <= max(1.0, hp * 0.3):
                for card in player.get("hand") or []:
                    if card.get("can_play") is False or _cost(card) > energy:
                        continue
                    if not _applies_vulnerable(str(card.get("description") or "")):
                        continue
                    tgt = self._pick_target(enemies, 0)
                    if self._needs_vulnerable(enemies, tgt):
                        act = {"action": "play_card", "card_index": card.get("index")}
                        target = self._target_for(card, enemies, tgt)
                        if target:
                            act["target"] = target
                        return act

        for card in player.get("hand") or []:
            if card.get("can_play") is False:
                continue
            cost = _cost(card)
            if cost > energy:
                continue
            score, target = self._score_card(
                card, enemies, need, hp, int(battle.get("round") or 1),
                race=race, energy=energy, max_hp=max_hp, must_mix=must_mix, idle=idle,
                lethal_turn=lethal_turn,
            )
            if best is None or score > best[0]:
                best = (score, card, target)

        # 分数太低说明打了也没用（比如满护甲还去叠甲），留着能量结束回合
        if best is not None and best[0] >= 1.0:
            act = {"action": "play_card", "card_index": best[1].get("index")}
            target = self._target_for(best[1], enemies, best[2])
            if target:
                act["target"] = target
            return act
        return {"action": "end_turn"}

    def _target_for(self, card: dict, enemies: list[dict], target: Any = None) -> Any:
        """这张牌到底要不要带目标。

        真机有牌 target_type 是 None 但 requires_target 为 true（「挑衅」这类），
        只按 target_type 判断就会漏传，引擎直接 invalid_target 把这一回合废掉。
        """
        if target:
            return target
        needs = bool(card.get("requires_target")) or str(
            card.get("target_type") or ""
        ) in ("AnyEnemy", "AllEnemies")
        if not needs:
            return None
        valid = card.get("valid_target_indices") or []
        if valid:
            return str(valid[0])
        return self._pick_target(enemies, 0)

    def _score_card(
        self,
        card: dict,
        enemies: list[dict],
        need: int,
        hp: float,
        round_no: int,
        *,
        race: bool = False,
        energy: int = 0,
        max_hp: float = 0.0,
        must_mix: bool = False,
        idle: bool = False,
        lethal_turn: bool = False,
    ) -> tuple[float, str | None]:
        ctype = str(card.get("type") or "")
        desc = str(card.get("description") or "")
        low = desc.lower()
        cost = max(1, _cost(card))
        value = _first_int(desc)
        aoe = any(k in low for k in ("all enem", "所有敌", "全体敌", "全部敌"))

        # 自伤牌（Offering 之类）：血量不够付代价时绝对不碰
        cost_hp = _self_hp_cost(desc)
        if cost_hp and hp - cost_hp <= 1:
            return 0.05, None
        if cost_hp and hp <= cost_hp * 2.5:
            return 0.2, None
        if cost_hp:
            # 花钱买节奏的牌只在“血够多、手里缺能量”时打，否则等于自己打自己
            if max_hp and hp / max_hp < 0.6:
                return 0.1, None
            if energy >= 2:
                return 0.2, None
            return 6.0, None

        if ctype == "Attack":
            dmg = _card_damage(desc)
            dmg *= len(enemies) if aoe else 1
            if dmg <= 0:
                return 0.5, None
            target = None if aoe else self._pick_target(enemies, dmg)
            lethal = any(((e.get("hp") or 0) + (e.get("block") or 0)) <= dmg for e in enemies)
            score = (500.0 if lethal else 0.0) + dmg / cost * 10.0
            if need > 0:
                score += 3.0
            if race:
                score *= 1.25
            return score, target

        if ctype == "Power":
            # 能力牌越早越值，但别把能量全花在能力上
            return (10.0 if round_no <= 2 else 6.0), None

        if ctype == "Skill":
            is_block = any(k in low for k in ("block", "护甲", "格挡"))
            if is_block and value > 0:
                if need > 0:
                    # 越危险（这一回合要吃的伤害相对血量越大）越该挡
                    danger = min(1.0, need / max(1.0, hp))
                    score = min(value, need) / cost * 10.0 * (1.0 + 2.0 * danger) + 2.0
                    if value >= need:
                        # 正好挡住这一回合的全部伤害，额外值钱
                        score += 8.0
                    if race:
                        # 挡不干净就只能抢血，叠甲只是把死亡拖两回合
                        score *= 0.35
                    if must_mix:
                        # 反正这一回合打不死我：留能量给攻击，别把甲叠到天上
                        score *= 0.6
                    if idle:
                        # 手上没有能打的攻击牌：格挡是白捡的价值，不出就浪费一张
                        score *= 2.5
                    if lethal_turn:
                        # 这一回合挡不住就死：护甲的分量必须压过一切
                        score *= 4.0
                    return score * self.block_priority, None
                return 0.6, None
            if any(k in low for k in ("draw", "抽")):
                return 5.0, None
            if any(k in low for k in ("energy", "能量")):
                return 5.5, None
            return 0.8, None

        return 0.3, None

    @staticmethod
    def _needs_vulnerable(enemies: list[dict], target_id: str | None) -> bool:
        """目标身上还没有易伤，才值得先给它挂一层。"""
        for e in enemies:
            if target_id is None or str(e.get("entity_id")) == str(target_id):
                return not _has_status(e, "Vulnerable", "易伤")
        return False

    @staticmethod
    def _pick_target(enemies: list[dict], dmg: int) -> str | None:
        if not enemies:
            return None
        for e in sorted(enemies, key=lambda x: (x.get("hp") or 0)):
            if dmg > 0 and ((e.get("hp") or 0) + (e.get("block") or 0)) <= dmg:
                return e.get("entity_id")
        worst = max(enemies, key=lambda e: _intent_damage(e))
        return worst.get("entity_id")

    @staticmethod
    def _pick_potion(player: dict) -> dict | None:
        for p in player.get("potions") or []:
            if p.get("can_use_in_combat") and p.get("slot") is not None:
                return p
        return None

    # -- 非战斗界面 --
    def _hand_select(self, st: dict) -> dict:
        hs = st.get("hand_select") or {}
        if hs.get("can_confirm"):
            return {"action": "combat_confirm_selection"}
        cards = hs.get("cards") or []
        if cards:
            return {"action": "combat_select_card", "card_index": cards[0].get("index")}
        return {"action": "wait"}

    def _rewards(self, st: dict) -> dict:
        items = (st.get("rewards") or {}).get("items") or []
        # 药水槽满时引擎会把药水奖励标成 claimable=false，硬领直接报错。
        claimable = [it for it in items if it.get("claimable", True)]
        if claimable:
            return {"action": "claim_reward", "index": claimable[0].get("index")}
        acts = [str(a) for a in st.get("available_actions") or []]
        if "resolve_rewards" in acts:
            return {"action": "resolve_rewards"}
        return {"action": "proceed"}

    def _card_reward(self, st: dict) -> dict:
        cr = st.get("card_reward") or {}
        cards = cr.get("cards") or []
        if not cards:
            return {"action": "skip_card_reward"}
        prof = _deck_profile((st.get("player") or {}).get("deck"))
        best = max(cards, key=lambda c: _deck_fit(c, prof))
        must_pick = not cr.get("can_skip", True) or len(cards) == 1
        if must_pick or _deck_fit(best, prof) >= self.card_min:
            return {"action": "select_card_reward", "card_index": best.get("index")}
        return {"action": "skip_card_reward"}

    def _map(self, st: dict) -> dict | None:
        opts = (st.get("map") or {}).get("next_options") or []
        if not opts:
            return None
        player = st.get("player") or {}
        hp = float(player.get("hp") or 0)
        max_hp = float(player.get("max_hp") or 1) or 1.0
        ratio = hp / max_hp
        gold = int(player.get("gold") or 0)
        best = max(opts, key=lambda o: self._node_score(str(o.get("type") or ""), ratio, gold))
        return {"action": "choose_map_node", "index": best.get("index")}

    @staticmethod
    def _node_score(node_type: str, hp_ratio: float, gold: int) -> float:
        if node_type == "RestSite":
            return 60.0 if hp_ratio < 0.6 else 15.0
        if node_type == "Elite":
            return 55.0 if hp_ratio > 0.8 else -25.0
        if node_type == "Treasure":
            return 40.0
        if node_type == "Shop":
            # 手里有闲钱时商店比宝箱值：药水能救命，牌能加厚度
            return 45.0 if gold >= 120 else (25.0 if gold >= 80 else 12.0)
        if node_type == "Event":
            return 25.0
        if node_type == "Monster":
            return 20.0
        return 5.0

    def _event(self, st: dict) -> dict | None:
        ev = st.get("event") or {}
        if ev.get("in_dialogue"):
            return {"action": "advance_dialogue"}
        opts = [o for o in (ev.get("options") or []) if not o.get("is_locked")]
        if not opts:
            return None
        player = st.get("player") or {}
        hp_ratio = float(player.get("hp") or 0) / (float(player.get("max_hp") or 1) or 1.0)
        gold = int(player.get("gold") or 0)
        leaves = [o for o in opts if o.get("is_proceed")]
        real = [o for o in opts if not o.get("is_proceed")] or leaves
        best = max(real, key=lambda o: _event_option_score(o, hp_ratio, gold))
        return {"action": "choose_event_option", "index": best.get("index")}

    def _rest(self, st: dict) -> dict:
        opts = [o for o in (st.get("rest_site") or {}).get("options") or [] if o.get("is_enabled", True)]
        if not opts:
            # 真机上偶尔拿不到 options 列表（或全被标成不可用）：只要引擎说这一步能选，
            # 就盲点第一个 —— 营火默认第一个是休息，比直接走出门强。
            if "choose_rest_option" in (st.get("available_actions") or []):
                return {"action": "choose_rest_option", "index": 0}
            return {"action": "proceed"}
        acts = st.get("available_actions") or []
        if acts and "choose_rest_option" not in acts and "proceed" in acts:
            # 引擎这边已经不给选选项了（比如睡过了），只剩「继续」
            return {"action": "proceed"}
        player = st.get("player") or {}
        ratio = float(player.get("hp") or 0) / (float(player.get("max_hp") or 1) or 1.0)
        prof = _deck_profile(player.get("deck"))
        rest_opt = next((o for o in opts if str(o.get("id")) == "rest"), None)
        smith_opt = next((o for o in opts if str(o.get("id")) in ("smith", "upgrade")), None)
        # 血少于 65% 就睡；否则升级 —— 升级一张核心牌比回 20 点血对后面几层更值
        if smith_opt is not None and ratio >= 0.65 and float(prof.get("upgradable") or 0) > 0:
            return {"action": "choose_rest_option", "index": smith_opt.get("index")}
        if rest_opt is not None:
            return {"action": "choose_rest_option", "index": rest_opt.get("index")}
        return {"action": "choose_rest_option", "index": opts[0].get("index")}

    def _leave(self, st: dict) -> dict:
        return {"action": "proceed"}

    def _shop(self, st: dict) -> dict:
        """先买能救命的药水，再挑一张明显更强的牌，剩下的金币留给后面的商店。

        STS2-Agent 的商店要先「打开货架」才能买、要「关上」才能走：所以先决定想不想买，
        不想买就直接走 —— 否则会在「开→关→开」之间来回振荡（真机上踩过）。
        """
        shop = st.get("shop") or {}
        player = st.get("player") or {}
        gold = int(player.get("gold") or 0)
        potions = [p for p in (player.get("potions") or []) if p.get("potion_id")]
        slots = int(player.get("max_potion_slots") or 3)
        items = [
            i
            for i in (shop.get("items") or [])
            if i.get("is_stocked", True)
            and i.get("available", True)
            and (i.get("category"), i.get("index")) not in self._bad_buys
        ]
        prof = _deck_profile(player.get("deck"))

        buy: dict | None = None
        for it in items:
            price = _price(it)
            if (
                it.get("category") == "potion"
                and len(potions) < slots
                and it.get("can_afford", True)
                and 0 < price <= gold
            ):
                buy = {"action": "shop_purchase", "index": it.get("index"), "category": "potion"}
                break
        if buy is None:
            best: tuple[float, dict] | None = None
            for it in items:
                if it.get("category") != "card":
                    continue
                price = _price(it)
                if price <= 0 or price > gold - 30 or not it.get("can_afford", True):  # 留点钱
                    continue
                card = {
                    "type": it.get("card_type"),
                    "name": it.get("card_name") or "",
                    "description": it.get("card_description") or "",
                    "rarity": it.get("card_rarity") or "Common",
                    "cost": it.get("card_cost"),
                    "is_upgraded": False,
                }
                fit = _deck_fit(card, prof)
                if fit >= self.card_min + 2 and (best is None or fit > best[0]):
                    best = (fit, it)
            if best:
                buy = {
                    "action": "shop_purchase",
                    "index": best[1].get("index"),
                    "category": "card",
                }
        # 删牌：牌组越厚越值钱，一张 75 金的删牌往往比买一张新牌强
        if buy is None and float(prof.get("size") or 0) >= 14:
            for it in items:
                if it.get("category") != "card_removal":
                    continue
                price = _price(it)
                if price <= 0 or price > gold - 40:
                    continue
                buy = {
                    "action": "shop_purchase",
                    "index": it.get("index"),
                    "native_index": it.get("native_index"),
                    "category": "card_removal",
                }
                break

        if buy is not None:
            if shop.get("is_open") is False:
                if not shop.get("can_open", True):
                    return {"action": "proceed"}
                return {"action": "open_shop"}
            return buy
        if shop.get("is_open"):
            return {"action": "close_shop"}
        return {"action": "proceed"}

    def _treasure(self, st: dict) -> dict:
        t = st.get("treasure") or {}
        if t.get("is_opened") is False:
            return {"action": "open_chest"}
        relics = t.get("relics") or []
        if relics:
            return {"action": "claim_treasure_relic", "index": relics[0].get("index")}
        if t.get("can_proceed"):
            return {"action": "proceed"}
        return {"action": "wait"}

    def _card_select(self, st: dict) -> dict | None:
        cs = st.get("card_select") or {}
        cards = [c for c in (cs.get("cards") or []) if c.get("index") is not None]
        if not cards:
            if cs.get("can_confirm"):
                return {"action": "confirm_selection"}
            if cs.get("can_cancel"):
                return {"action": "cancel_selection"}
            return None
        kind = str(cs.get("kind") or "").lower()
        prompt = str(cs.get("prompt") or "").lower()
        try:
            chosen = int(cs.get("selected_count") or 0)
        except (TypeError, ValueError):
            chosen = 0
        marked = sum(1 for c in cards if c.get("selected"))
        if marked > chosen:
            chosen = marked
        try:
            want = max(1, int(cs.get("max_select") or 1))
        except (TypeError, ValueError):
            want = 1
        want = min(want, len(cards))
        # 模拟器/老接口不给 selected 计数，靠本地记「这一屏我点过几张」
        key = (kind, prompt)
        if key != self._sel_key:
            self._sel_key = key
            self._sel_count = 0
            self._sel_picked = set()
            self._sel_confirmed = False
        # 上一次确认过、这一屏又回到「一张都没选」= 是新的一次选牌，本地计数要清零
        if self._sel_confirmed and chosen == 0 and marked == 0:
            self._sel_count = 0
            self._sel_picked = set()
            self._sel_confirmed = False
        if marked or cs.get("selected_count") is not None:
            self._sel_count = chosen
        # 选够了就收尾，别在界面里空转
        if self._sel_count >= want or chosen >= want:
            self._sel_confirmed = True
            return {"action": "confirm_selection"}
        # 真机上「删牌」和「事件选牌」都用 deck_card_select 这个 kind，只能靠提示语区分
        if "upgrade" in kind or "升级" in prompt:
            order = sorted(cards, key=_upgrade_value, reverse=True)
        elif "remove" in kind or "移除" in prompt or "删" in prompt:
            order = sorted(cards, key=lambda c: _card_strength(c))
        else:
            prof = _deck_profile((st.get("player") or {}).get("deck"))
            order = sorted(cards, key=lambda c: _deck_fit(c, prof), reverse=True)
        # 每一步都重读状态，所以「点过哪些」只用来跳过已经选中的牌；
        # 同一张牌重复点会被原生界面当成又一次点击（状态不变），必须换下一张。
        for c in order:
            if c.get("selected") or c.get("index") in self._sel_picked:
                continue
            self._sel_picked.add(c.get("index"))
            self._sel_count += 1
            return {"action": "select_card", "index": c.get("index")}
        self._sel_confirmed = True
        return {"action": "confirm_selection"}

    def _bundle_select(self, st: dict) -> dict | None:
        bs = st.get("bundle_select") or {}
        if bs.get("preview_showing"):
            return {"action": "confirm_bundle_selection"}
        bundles = bs.get("bundles") or []
        if bundles:
            return {"action": "select_bundle", "index": bundles[0].get("index")}
        return None

    def _relic_select(self, st: dict) -> dict:
        rs = st.get("relic_select") or {}
        relics = rs.get("relics") or []
        if relics:
            return {"action": "select_relic", "index": relics[0].get("index")}
        return {"action": "skip_relic_selection"}

    def _crystal_sphere(self, st: dict) -> dict:
        csp = st.get("crystal_sphere") or {}
        cells = csp.get("clickable_cells") or []
        if cells:
            cell = cells[0]
            return {"action": "crystal_sphere_click_cell", "x": cell.get("x"), "y": cell.get("y")}
        if csp.get("can_proceed"):
            return {"action": "crystal_sphere_proceed"}
        return {"action": "wait"}


def _price(item: dict) -> int:
    """商店条目价格：正常商店用 price，假商人用 cost。"""
    for key in ("price", "cost", "gold_cost"):
        val = item.get(key)
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str) and val.strip().isdigit():
            return int(val.strip())
    return 0


def _card_priority(card: dict) -> float:
    rarity = str(card.get("rarity") or "Common")
    score = {"Rare": 30.0, "Uncommon": 20.0, "Common": 12.0}.get(rarity, 10.0)
    ctype = str(card.get("type") or "")
    desc = str(card.get("description") or "")
    if ctype == "Power":
        score += 6.0
    elif ctype == "Attack":
        score += 4.0
        # 每点能量的伤害才是牌的真实强度：0 费 6 伤（Anger）比 2 费 8 伤强
        dmg = _card_damage(desc)
        try:
            cost = float(int(card.get("cost")))
        except (TypeError, ValueError):
            cost = 1.0
        if cost <= 0:
            cost = 0.5
        if dmg > 0:
            score += min(8.0, dmg / cost)
    elif ctype in ("Curse", "Status"):
        score -= 40.0
    else:
        # 纯格挡/功能牌：格挡本身也是资源，按每点能量的格挡值给分
        blk = _card_block(desc)
        if blk > 0:
            try:
                bcost = float(int(card.get("cost")))
            except (TypeError, ValueError):
                bcost = 1.0
            score += min(6.0, blk / max(bcost, 0.5) * 0.8)
    return score


def _card_block(desc: str) -> int:
    """牌面格挡值：“获得5点格挡” / “Gain 5 Block” → 5。"""
    text = str(desc or "")
    for pat in (r"获得\s*(\d+)\s*点格挡", r"gain\s+(\d+)\s+block", r"(\d+)\s*点格挡", r"(\d+)\s+block"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def _deck_profile(deck: list[dict] | None) -> dict[str, Any]:
    """把整副牌压成几个比例，供构筑决策用（缺什么补什么）。"""
    cards = [c for c in (deck or []) if isinstance(c, dict)]
    size = len(cards)
    prof: dict[str, Any] = {
        "size": float(size),
        "attack_ratio": 0.0,
        "block_ratio": 0.0,
        "powers": 0.0,
        "upgradable": 0.0,
        "curse_ratio": 0.0,
        "names": {},
    }
    if not size:
        return prof
    atk = blk = pwr = up = curse = 0
    vuln = exhaust = 0
    names: dict[str, int] = {}
    for c in cards:
        desc = str(c.get("description") or "")
        low = desc.lower()
        ctype = str(c.get("type") or "")
        if ctype == "Power":
            pwr += 1
        elif ctype == "Attack":
            atk += 1
        if _card_block(desc) > 0:
            blk += 1
        if ctype == "Curse" or "诅咒" in str(c.get("name") or ""):
            curse += 1
        elif not c.get("is_upgraded"):
            up += 1
        # 连招素材：哪些牌能给易伤 / 会消耗，决定「吃易伤」「吃消耗」的能力牌值不值得拿
        if "易伤" in desc or "vulnerable" in low:
            vuln += 1
        if "消耗" in desc or "exhaust" in low:
            exhaust += 1
        name = str(c.get("name") or "")
        if name:
            names[name] = names.get(name, 0) + 1
    prof.update(
        {
            "attack_ratio": atk / size,
            "block_ratio": blk / size,
            "powers": float(pwr),
            "upgradable": float(up),
            "curse_ratio": curse / size,
            "vuln": float(vuln),
            "exhaust": float(exhaust),
            "names": names,
        }
    )
    return prof


def _deck_fit(card: dict, prof: dict[str, Any] | None) -> float:
    """在单卡评分之上，按“这副牌现在缺什么”再调一次。

    塔牌里牌组越薄越强，所以牌组厚了以后门槛会自己抬高；
    同名牌超过两张基本是浪费牌位，直接压分。
    """
    score = _card_priority(card)
    if not prof:
        return score
    ctype = str(card.get("type") or "")
    desc = str(card.get("description") or "")
    cost = float(_cost(card) or 1)
    size = float(prof.get("size") or 0)

    if ctype == "Attack":
        dpe = _card_damage(desc) / max(cost, 0.5)
        ratio = float(prof.get("attack_ratio") or 0.0)
        if ratio < 0.40:
            score += 6.0 + min(6.0, dpe)
        elif ratio > 0.62:
            score -= 6.0
    elif _card_block(desc) > 0:
        bpe = _card_block(desc) / max(cost, 0.5)
        ratio = float(prof.get("block_ratio") or 0.0)
        if ratio < 0.30:
            score += 6.0 + min(6.0, bpe)
        elif ratio > 0.55:
            score -= 6.0
    elif ctype == "Power":
        score += 6.0 if float(prof.get("powers") or 0) < 4 else -8.0
        low = desc.lower()
        # 连招牌：没有易伤来源就别拿「给易伤时……」的能力，没有消耗就別拿「消耗时……」
        if ("易伤" in desc or "vulnerable" in low) and float(prof.get("vuln") or 0) <= 0:
            score -= 14.0
        if ("消耗" in desc or "exhaust" in low) and float(prof.get("exhaust") or 0) <= 0:
            score -= 12.0
    else:
        # 抽牌/加能量这类功能牌不嫌多，但也不额外抬高
        score += 2.0

    if size > 14:
        score -= (size - 14) * 2.2
    same = int((prof.get("names") or {}).get(str(card.get("name") or ""), 0))
    if same >= 2:
        score -= 5.0 * (same - 1)
    return score


def _upgrade_value(card: dict) -> float:
    """哪张牌最值得升级：优先上易伤/核心能力，其次看每点能量的收益。"""
    if card.get("is_upgraded"):
        return -1e9
    ctype = str(card.get("type") or "")
    if ctype in ("Curse", "Status"):
        return -1e9
    desc = str(card.get("description") or "")
    low = desc.lower()
    cost = float(_cost(card) or 1)
    score = _card_damage(desc) / max(cost, 0.5) + _card_block(desc) / max(cost, 0.5) * 0.8
    name = str(card.get("name") or "")
    if name in ("痛击", "Bash") or "易伤" in desc or "vulnerable" in low:
        score += 12.0
    if ctype == "Power":
        score += 8.0
    if "抽" in desc or "draw" in low:
        score += 3.0
    return score


def _card_strength(card: dict) -> float:
    """一张牌的大致战斗力，删牌时挑最弱的。"""
    desc = str(card.get("description") or "")
    val = _card_damage(desc) * 1.2 + _card_block(desc) * 1.0
    ctype = str(card.get("type") or "")
    if ctype == "Power":
        val += 12.0
    if ctype == "Curse":
        val -= 30.0
    return val


def _worst_card(cards: list[dict]) -> dict | None:
    """删牌目标：基础打击/防御最该删，其次是纯数值低的牌。"""
    if not cards:
        return None
    return min(cards, key=_card_strength)


def _event_option_score(opt: dict, hp_ratio: float, gold: int) -> float:
    """事件选项打分：能拿遗物/加血/升级就选，要花大半金币或塞诅咒就躲。"""
    text = _strip_bbcode(f"{opt.get('title') or ''} {opt.get('description') or ''}")
    low = text.lower()
    score = 0.0
    # 金币成本：只看「N 金币」这种写法，避免把伤害数字当成价格
    cost = 0
    for m in re.finditer(r"(\d+)\s*金币", text):
        cost = max(cost, int(m.group(1)))
    if cost > gold:
        score -= 40.0
    elif cost and cost > gold * 0.7:
        score -= 10.0
    if ("遗物" in text) or ("relic" in low):
        score += 12.0
    if ("最大生命" in text) or ("max hp" in low):
        score += 8.0
    if ("回复" in text) or ("治疗" in text) or ("heal" in low):
        score += 10.0 if hp_ratio < 0.6 else 5.0
    if ("升级" in text) or ("upgrade" in low):
        score += 6.0
    if ("移除" in text) or ("remove" in low):
        # 一次删两张会把牌组抽干，只在牌组够厚时才算好事
        score += 1.0 if re.search(r"移除\s*[^\d]{0,6}2", text) else 4.0
    if ("诅咒" in text) or ("curse" in low):
        score -= 12.0
    if ("孢子" in text) or ("spore" in low):
        score -= 6.0
    if ("失去" in text) or ("受伤" in text) or ("lose" in low):
        score -= 10.0 if hp_ratio < 0.5 else 2.0
    # 纯「离开」类选项排最后，除非别的都不可选
    if opt.get("is_proceed"):
        score -= 5.0
    return score


# ---------------------------------------------------------------------------
# 可选的模型层
# ---------------------------------------------------------------------------
LLM_SYSTEM = """你在玩《杀戮尖塔 2》(Slay the Spire 2)，用 JSON 指挥游戏。

只输出一个 JSON 对象，不要解释，不要代码块：
{"think": "一句话思路", "actions": [{"action": "动作名", "参数": 值}], "stop": false}

规则：
1) actions 最多 4 个，按顺序执行；每一步都基于“上一步执行后的新状态”，所以不要写死后续手牌的索引。
2) 战斗里牌打出去后手牌索引会左移，宁可只写 1-2 个动作，让系统重新读状态再决定。
3) 选牌优先补强核心思路（能量/抽牌/关键攻击），不要拿诅咒和状态牌。
4) 拿不准就把 actions 留空、stop 设 true，交给规则层。

""" + ACTION_HELP


PLAN_SYSTEM = """你在玩《杀戮尖塔 2》(Slay the Spire 2)，用 JSON 给出「本回合出牌计划」。

只输出一个 JSON 对象，不要解释，不要代码块，不要写思考过程：
{"think": "一句话思路", "plan": [{"card": "牌名"}, {"card": "牌名", "target": 0}, {"end_turn": true}]}

规则：
1) 牌名必须和手牌里显示的一模一样（中文就写中文），不要用编号代替名字。
2) 按顺序执行，别超过手牌张数，也别超过本回合能量能打出的张数。
3) 计划最后一定要接 {"end_turn": true}。
4) 手牌行末的「→实际N伤 / →实际N挡」和「本回合：…」那行都是引擎算好的真值（已含力量、易伤等），直接用它们算，不要自己再推。
5) 算清楚敌人意图：这回合要挡多少、够不够斩杀；先易伤后输出，别为了叠满护甲浪费能量。
6) 只输出 JSON，第一个字符必须是 {，最后一个是 }，不要输出任何解释文字。
"""


def parse_plan_json(text: str) -> list[dict]:
    """抠出「回合计划」：一个按顺序执行的动作数组。

    模型的写法很花：plan / actions / steps、元素是 {"card":..} 或
    {"action":"play_card","card_index":2} 或纯牌名字符串，都得能认。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    cands = [raw]
    cands += [m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S)]
    first, last = raw.find("{"), raw.rfind("}")
    if 0 <= first < last:
        cands.append(raw[first : last + 1])

    for cand in cands:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            obj = {"plan": obj}
        if not isinstance(obj, dict):
            continue
        steps = obj.get("plan") or obj.get("actions") or obj.get("steps")
        if not isinstance(steps, list):
            continue
        out: list[dict] = []
        for s in steps:
            if isinstance(s, str) and s.strip():
                if s.strip().lower() in ("end_turn", "end turn", "结束回合"):
                    out.append({"end_turn": True})
                else:
                    out.append({"card": s.strip()})
            elif isinstance(s, dict):
                if s.get("end_turn") or str(s.get("action") or "").lower() in ("end_turn", "结束回合"):
                    out.append({"end_turn": True})
                    continue
                name = s.get("card") or s.get("name") or s.get("card_name")
                step: dict[str, Any] = {}
                if name:
                    step["card"] = str(name).strip()
                elif s.get("card_index") is not None:
                    step["card_index"] = s.get("card_index")
                else:
                    continue
                if s.get("target") is not None:
                    step["target"] = s.get("target")
                out.append(step)
        if out:
            return out[:8]
    return []


def parse_llm_json(text: str) -> list[dict]:
    """从模型输出里抠出 actions。宽容：代码块 / 前后废话 / 单动作对象。"""
    raw = (text or "").strip()
    if not raw:
        return []
    cands = [raw]
    cands += [m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S)]
    first, last = raw.find("{"), raw.rfind("}")
    if 0 <= first < last:
        cands.append(raw[first : last + 1])

    for cand in cands:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            obj = {"actions": obj}
        if not isinstance(obj, dict):
            continue
        actions = obj.get("actions")
        if actions is None and obj.get("action"):
            actions = [obj]
        if not isinstance(actions, list):
            continue
        out: list[dict] = []
        for a in actions:
            if isinstance(a, dict) and (a.get("action") or a.get("tool")):
                item = dict(a)
                if not item.get("action") and item.get("tool"):
                    item["action"] = item.pop("tool")
                out.append(item)
            elif isinstance(a, str) and a.strip():
                out.append({"action": a.strip()})
        if out:
            return out[:4]
    return []


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------
class Sts2PlayerPlugin(Star):
    """AI 玩《杀戮尖塔 2》：规则机器人打底，可选 LLM 决策。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.client = Sts2Client(
            str(self._cfg("sts2_url", DEFAULT_URL)),
            float(self._cfg("sts2_timeout_sec", 20)),
        )
        self.bot = RuleBot(
            potion_hp_ratio=float(self._cfg("potion_hp_ratio", 0.32)),
            block_priority=float(self._cfg("block_priority", 0.15)),
            card_min=float(self._cfg("card_min", 18)),
        )
        self._stop: set[str] = set()
        self._running: set[str] = set()

    # ---- 杂项 ----
    def _cfg(self, key: str, default: Any = None) -> Any:
        if not isinstance(self.config, dict):
            return default
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    def _authorized(self, event: AstrMessageEvent) -> bool:
        allowed = self._cfg("operator_ids", []) or []
        if not allowed:
            return True
        sid = str(event.get_sender_id())
        return sid in {str(a) for a in allowed}

    async def terminate(self) -> None:
        await self.client.close()

    # ---- LLM 决策 ----
    async def _ask_llm(
        self, event: AstrMessageEvent, state_text: str, stype: str = "", plan: bool = False
    ) -> list[dict]:
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            raise Sts2Error("没有可用的 LLM Provider，请先在 WebUI 里配置一个。")
        goal = str(self._cfg("goal", "稳一点打，先把这一局玩过关：优先保证不被秒，卡组补强核心输出。"))
        allowed = SCREEN_ACTIONS.get(stype, "")
        hint = (
            f"【本界面只允许这些动作】{allowed}\n"
            "不要发明别动作；动作名必须原样来自这一行。\n"
            if allowed
            else ""
        )
        if plan:
            prompt = (
                f"【长期目标】{goal}\n"
                f"【当前界面】{SCREEN_CN.get(stype, stype)}\n"
                f"【当前场景】\n{state_text}\n\n"
                "这是你的整个回合。请一次性给出**按顺序执行**的出牌计划，不要只给一步。\n"
                "格式：{\"think\":\"一句话思路\",\"plan\":[{\"card\":\"牌名\"},{\"card\":\"牌名\",\"target\":0},{\"end_turn\":true}]}\n"
                "规则：\n"
                "1. 牌名必须和手牌里显示的一模一样（中文就写中文）\n"
                "2. 计划别超过手牌张数，也别超过能量能打出的张数\n"
                "3. 打完了要接一条 {\"end_turn\":true}\n"
                "4. 算清楚敌人意图：这回合要挡多少、够不够斩杀，再决定顺序（先易伤后输出，别浪费能量叠满护甲）"
            )
        else:
            prompt = (
                f"【长期目标】{goal}\n"
                f"【当前界面】{SCREEN_CN.get(stype, stype)}\n"
                f"{hint}"
                f"【当前场景】\n{state_text}\n\n"
                "请给出接下来的动作。"
            )
        sys_prompt = str(self._cfg("llm_system_prompt", LLM_SYSTEM))
        if plan:
            sys_prompt = str(self._cfg("llm_plan_system_prompt", PLAN_SYSTEM))
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt=sys_prompt,
            **({"max_tokens": int(self._cfg("llm_max_tokens", 800))} if plan else {}),
        )
        text = getattr(resp, "completion_text", "") or ""
        logger.debug("STS2 LLM 输出：%s", text[:400])
        if plan:
            steps = parse_plan_json(text)
            if steps:
                logger.info(
                    "STS2 模型给出回合计划 %d 步：%s",
                    len(steps),
                    " → ".join(
                        str(s.get("card") or s.get("card_index") or ("结束回合" if s.get("end_turn") else s))
                        for s in steps
                    ),
                )
            else:
                logger.info("STS2 模型没给出可用的回合计划，原文：%s", (text or "")[:200].replace("\n", " "))
            return steps
        acts = parse_llm_json(text)
        if allowed:
            # 模型偶尔会编出别的后端才有的动作（menu_select 之类），先筛掉，
            # 全被筛掉就返回空、由规则层接手，而不是拿非法动作去撞三次错误。
            names = {a.strip() for a in allowed.replace("/", " ").split() if a.strip()}
            kept = [a for a in acts if str(a.get("action") or "").strip() in names]
            if len(kept) != len(acts):
                logger.info(
                    "STS2 模型给了 %d 个本界面用不了的动作，已丢弃（界面 %s）",
                    len(acts) - len(kept),
                    stype,
                )
            acts = kept
        return acts

    def _drop_unavailable(self, st: dict, actions: list[dict]) -> list[dict]:
        """模型爱在空奖励界面反复 claim_reward。

        引擎每次都把「当前能做什么」写在 available_actions 里，拿它对一遍就能
        在发出去之前把幻觉动作过滤掉，而不是让它撞三次 invalid_action 把整局停下。
        """
        avail = {str(a) for a in (st.get("available_actions") or [])}
        if not avail:
            return actions
        out: list[dict] = []
        for act in actions:
            name = str(act.get("action") or "").strip()
            if name in ("", "wait", "sleep"):
                out.append(act)
                continue
            params = {k: v for k, v in act.items() if k not in ("action", "tool", "think", "reason")}
            if name == "play_card" and not params.get("target"):
                # 模型经常忘记给目标；需要目标的牌漏了就直接白给一个合法目标
                hand = (st.get("player") or {}).get("hand") or []
                card = next(
                    (c for c in hand if str(c.get("index")) == str(params.get("card_index"))),
                    None,
                )
                if card is not None:
                    fixed = self.bot._target_for(
                        card, (st.get("battle") or {}).get("enemies") or []
                    )
                    if fixed:
                        act["target"] = fixed
                        params["target"] = fixed
            if name == "claim_reward":
                items = (st.get("rewards") or {}).get("items") or []
                want = next(
                    (it for it in items if str(it.get("index")) == str(params.get("index"))),
                    None,
                )
                if want is None or not want.get("claimable", True):
                    logger.info(
                        "丢掉模型动作 claim_reward %s（这一项领不了：%s）",
                        params.get("index"),
                        (want or {}).get("description"),
                    )
                    continue
            try:
                native, _ = agent_action(name, params)
            except Exception:  # noqa: BLE001
                native = name
            native = native or name
            # proceed 在不同后端可能叫 resolve_rewards / collect_rewards_and_proceed /
            # close_shop_inventory。但如果当前界面一个都不给（比如钱不够的商店只剩
            # close_shop_inventory），模型硬发 proceed 就是三次 invalid_action 收场。
            if name in ("proceed", "resolve_rewards"):
                if native in avail:
                    out.append(act)
                    continue
                sub = next(
                    (s for s in LEAVE_SUBSTITUTES if s in avail),
                    None,
                )
                if sub:
                    act = {"action": LEAVE_ACTIONS[sub]}
                    out.append(act)
                else:
                    logger.info("丢掉模型动作 %s（当前可用：%s）", native, ",".join(sorted(avail))[:160])
                continue
            if native in avail:
                out.append(act)
            else:
                logger.info("丢掉模型动作 %s（当前可用：%s）", native, ",".join(sorted(avail))[:160])
        return out

    # ---- 自动推进 ----
    async def _restart(self, event: AstrMessageEvent) -> bool:
        """死了之后从「游戏结束」一路点回可行动的对局（菜单 → 选角 → 进图）。

        最多试 10 次点击，任何一步点不动就返回 False（由调用方收尾）。
        """
        for _ in range(10):
            try:
                st = await self.client.state()
            except Sts2Error as e:
                logger.warning("重开新局时读状态失败：%s", e)
                return False
            if str(st.get("state_type")) not in ("game_over", "menu", "overlay"):
                return True
            act = self.bot._menu(st)
            if not act:
                return False
            name = str(act.get("action") or "")
            params = {k: v for k, v in act.items() if k != "action"}
            try:
                res = await self.client.act(name, **params)
            except Sts2Error as e:
                logger.warning("重开新局时 %s 失败：%s", name, e)
                return False
            if str(res.get("status")) == "error":
                logger.info("重开新局：%s 不行（%s）", name, res.get("error"))
            await asyncio.sleep(0.8)
        return False

    async def _auto(
        self, event: AstrMessageEvent, steps: int = 10
    ) -> AsyncGenerator[Any, None]:
        umo = event.unified_msg_origin
        if umo in self._running:
            yield event.plain_result("已经在自动跑了。发 /sts2 停 可以打断。")
            return

        cfg_max = int(self._cfg("max_steps", 40))
        steps = max(1, min(int(steps or 10), cfg_max))
        interval = float(self._cfg("step_interval_sec", 0.3))
        report_every = max(1, int(self._cfg("report_every", 5)))
        use_llm = bool(self._cfg("use_llm", False))
        llm_combat = bool(self._cfg("llm_combat", False))
        plan_mode = bool(self._cfg("llm_combat_plan", False))
        auto_restart = bool(self._cfg("auto_restart", True))
        restarts = 0

        self._running.add(umo)
        self._stop.discard(umo)
        log: list[str] = []
        errors = 0
        last_sig = ""
        same_count = 0
        report: list[str] = []

        try:
            i = 0
            waits = 0
            plan_asked: tuple | None = None
            while i < steps:
                if umo in self._stop:
                    yield event.plain_result(f"已停止（第 {i} 步）。")
                    return

                try:
                    st = await self.client.state()
                except Sts2Error as e:
                    yield event.plain_result(str(e))
                    return

                stype = str(st.get("state_type") or "")
                if stype == "game_over":
                    yield event.plain_result("这一局结束了。\n" + render_state(st))
                    if not auto_restart or restarts >= 3:
                        return
                    if not await self._restart(event):
                        return
                    restarts += 1
                    yield event.plain_result(f"自动开新的一局（本次第 {restarts + 1} 局）…")
                    last_sig = ""
                    same_count = 0
                    continue

                sig = f"{stype}:{zlib.crc32(json.dumps(st, ensure_ascii=False, sort_keys=True).encode())}"
                # 敌人回合 / 动画结算里状态本来就不会变：等，而且不占「步数」预算
                waiting = stype in COMBAT_SCREENS and not (st.get("battle") or {}).get(
                    "is_play_phase"
                )
                if waiting:
                    waits += 1
                    if waits > 600:
                        yield event.plain_result(
                            "等了很久还没轮到我出手（敌人回合卡住了？），先停下。\n"
                            + render_state(st)
                        )
                        return
                    await asyncio.sleep(max(0.8, interval))
                    continue
                waits = 0
                same_count = same_count + 1 if sig == last_sig else 0
                last_sig = sig
                if same_count >= 8:
                    yield event.plain_result(
                        "状态卡住没变化，先停下。最近的动作：\n"
                        + "\n".join(log[-6:])
                        + "\n\n"
                        + render_state(st)
                    )
                    return

                text = render_state(st)
                actions: list[dict] = []
                ask_model = (
                    use_llm
                    and stype in LLM_SCREENS
                    and (
                        stype not in COMBAT_SCREENS
                        or (llm_combat and not plan_mode)
                    )
                )
                # 回合计划模式：战斗里一回合只问一次模型，让它给整串出牌顺序，
                # 规则层负责按牌名逐步执行并在出意外时接手（省 token 也更像人）。
                if (
                    use_llm
                    and plan_mode
                    and stype in COMBAT_SCREENS
                    and (st.get("battle") or {}).get("is_play_phase")
                ):
                    turn_key = (stype, (st.get("battle") or {}).get("round"))
                    if not self.bot.plan and plan_asked != turn_key:
                        plan_asked = turn_key
                        try:
                            plan_steps = await self._ask_llm(event, text, stype, plan=True)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("STS2 回合计划失败，回退规则层：%s", e)
                            plan_steps = []
                        if plan_steps:
                            self.bot.set_plan(plan_steps, st)
                if ask_model:
                    try:
                        actions = await self._ask_llm(event, text, stype)
                    except Exception as e:  # noqa: BLE001 —— 模型挂了就退回规则层
                        logger.warning("STS2 LLM 决策失败，回退规则层：%s", e)
                        actions = []

                if actions:
                    actions = self._drop_unavailable(st, actions)

                if not actions:
                    one = self.bot.next_action(st)
                    actions = [one] if one else []

                if not actions:
                    if stype in ("menu", "overlay", "unknown"):
                        yield event.plain_result(
                            f"停在「{SCREEN_CN.get(stype, stype)}」界面，这一步需要人来点一下。\n{text}"
                        )
                    else:
                        yield event.plain_result(
                            f"规则层不知道「{SCREEN_CN.get(stype, stype)}」该做什么，先停下。\n{text}"
                        )
                    return

                for act in actions:
                    if umo in self._stop:
                        yield event.plain_result(f"已停止（第 {i} 步）。")
                        return
                    name = str(act.get("action") or "").strip()
                    if name in ("", "wait", "sleep"):
                        await asyncio.sleep(0.6)
                        continue
                    params = {
                        k: v
                        for k, v in act.items()
                        if k not in ("action", "tool", "think", "reason")
                    }
                    try:
                        res = await self.client.act(name, **params)
                    except Sts2Error as e:
                        yield event.plain_result(f"调用游戏失败：{e}")
                        return
                    if str(res.get("status")) == "error":
                        errors += 1
                        if name == "shop_purchase":
                            # 这一格买不了（买不起 / 药水位满 / 已售出），本次进店就别再试它
                            try:
                                self.bot._bad_buys.add((params.get("category"), params.get("index")))
                            except Exception:  # noqa: BLE001
                                pass
                        log.append(f"✗ {name} {params} → {_one_line(res.get('error'), 100)}")
                        if errors >= 3:
                            yield event.plain_result(
                                "连续失败 3 次，先停下。最近：\n"
                                + "\n".join(log[-6:])
                                + "\n\n"
                                + render_state(await self.client.state())
                            )
                            return
                    else:
                        errors = 0
                        log.append(f"✓ {name} {params} → {_one_line(res.get('message'), 80)}")

                i += 1
                if i % report_every == 0 or i == steps:
                    report.append(f"— 第 {i} 步 —\n" + render_state(st))
                    yield event.plain_result("\n\n".join(report[-1:]))

                await asyncio.sleep(interval)

            yield event.plain_result(
                f"跑满 {steps} 步，先停在这里。要继续就再发 /sts2 自动 {steps}。"
            )
        finally:
            self._running.discard(umo)
            self._stop.discard(umo)

    # ---- 指令 ----
    @filter.command("sts2")
    async def sts2_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """《杀戮尖塔 2》：/sts2 [状态|自动 <n>|执行 <动作> [json]|停|帮助]"""
        parts = (arg or "").strip().split()
        sub = parts[0].lower() if parts else "状态"
        rest = parts[1:]

        if sub in ("帮助", "help", "?", "动作"):
            yield event.plain_result(ACTION_HELP)
            return
        if sub in ("停", "stop"):
            self._stop.add(event.unified_msg_origin)
            yield event.plain_result("已请求停止，最迟当前这一步结束后停下。")
            return
        if not self._authorized(event):
            yield event.plain_result("你没有操作这台电脑上游戏的权限。")
            return

        if sub in ("自动", "auto", "跑"):
            n = 10
            if rest:
                try:
                    n = int(rest[0])
                except ValueError:
                    yield event.plain_result("用法：/sts2 自动 [步数]，步数是整数。")
                    return
            async for item in self._auto(event, n):
                yield item
            return

        if sub in ("执行", "do", "act"):
            if not rest:
                yield event.plain_result("用法：/sts2 执行 play_card {\"card_index\":0}")
                return
            action = rest[0]
            params: dict[str, Any] = {}
            if len(rest) > 1:
                payload = " ".join(rest[1:])
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        params = parsed
                except json.JSONDecodeError:
                    yield event.plain_result(f"参数不是合法 JSON：{payload}")
                    return
            try:
                res = await self.client.act(action, **params)
            except Sts2Error as e:
                yield event.plain_result(str(e))
                return
            if str(res.get("status")) == "error":
                yield event.plain_result(f"游戏拒绝了这个动作：{res.get('error')}")
            else:
                yield event.plain_result(f"已执行：{res.get('message') or action}")
            return

        # 默认：状态快照
        try:
            st = await self.client.state()
        except Sts2Error as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(render_state(st))

    # ---- llm_tool：让主 LLM 在聊天里直接用 ----
    @filter.llm_tool(name="sts2_state")
    async def tool_sts2_state(self, event: AstrMessageEvent) -> str:
        """查看《杀戮尖塔 2》当前对局的真实状态（引擎真值，不是截图猜测）。

        返回当前界面、血量、能量、手牌、敌人意图、地图可选项等压缩后的文本。
        想知道现在能做什么、该做什么时先调用它。
        """
        try:
            st = await self.client.state()
        except Sts2Error as e:
            return f"读取失败：{e}"
        return render_state(st)

    @filter.llm_tool(name="sts2_action")
    async def tool_sts2_action(self, event: AstrMessageEvent, action: str, args_json: str = "{}") -> str:
        """对《杀戮尖塔 2》执行一个动作（直接改变游戏状态）。

        Args:
            action(string): 动作名，如 play_card / end_turn / choose_map_node / claim_reward / proceed
            args_json(string): JSON 对象形式的参数，如 {"card_index":0,"target":"JAW_WORM_0"}；无参数用 {}
        """
        if not self._authorized(event):
            return "当前用户没有操作游戏的权限。"
        try:
            params = json.loads(args_json or "{}")
        except json.JSONDecodeError:
            return f"参数不是合法 JSON：{args_json}"
        if not isinstance(params, dict):
            return "参数必须是 JSON 对象。"
        try:
            res = await self.client.act(action.strip(), **params)
        except Sts2Error as e:
            return f"执行失败：{e}"
        if str(res.get("status")) == "error":
            return f"游戏拒绝了这个动作：{res.get('error')}\n可用动作参考：\n{ACTION_HELP}"
        return f"已执行：{res.get('message') or action}"
