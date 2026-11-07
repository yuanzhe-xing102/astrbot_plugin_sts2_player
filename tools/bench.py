# -*- coding: utf-8 -*-
"""规则层批量跑分：多个 seed 跑满一整幕，统计胜率 / 平均剩余血量 / 卡组厚度。

不经过 HTTP，直接驱动 tools/mock_sts2.py 的 Game + 插件里的 RuleBot，
所以很快（每局几百个决策，纯内存）。

运行：
    python tools/bench.py                # 默认 40 局
    python tools/bench.py --seeds 100
    python tools/bench.py --verbose      # 每局打印一行
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import statistics
import sys
from pathlib import Path

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


def run_one(bot, seed: int, max_steps: int = 600) -> dict:
    """跑一局，返回统计。不改动 mock_sts2.GAME（用独立实例）。"""
    game = mock_sts2.Game(seed=seed)
    steps = 0
    stuck = ""
    while steps < max_steps:
        st = game.snapshot()
        if st["state_type"] == "game_over":
            break
        action = bot.next_action(st)
        if not action:
            stuck = f"无动作@{st['state_type']}"
            break
        params = {k: v for k, v in action.items() if k != "action"}
        try:
            game.act(action["action"], params)
        except mock_sts2.GameError as exc:  # 规则层给了非法参数
            stuck = f"{action['action']} 非法: {exc}"
            break
        steps += 1

    st = game.snapshot()
    over = st.get("game_over") or {}
    won = st["state_type"] == "game_over" and "赢" in over.get("message", "")
    return {
        "seed": seed,
        "won": won,
        "floor": st["run"]["floor"],
        "hp": st["player"]["hp"],
        "max_hp": st["player"]["max_hp"],
        "gold": st["player"]["gold"],
        "deck": len(game.deck),
        "steps": steps,
        "stuck": stuck,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--bp", type=float, default=0.15, help="格挡权重 block_priority")
    ap.add_argument("--potion", type=float, default=0.32, help="喝药血量阈值 potion_hp_ratio")
    ap.add_argument("--pick", type=float, default=18.0, help="选牌阈值 card_min")
    args = ap.parse_args()

    stray = Path.cwd() / "data"
    had_data = stray.exists()

    mod = load_plugin()
    bot = mod.RuleBot(potion_hp_ratio=args.potion, block_priority=args.bp, card_min=args.pick)

    rows = []
    for seed in range(args.start, args.start + args.seeds):
        row = run_one(bot, seed)
        rows.append(row)
        if args.verbose or row["stuck"]:
            print(f"seed {row['seed']:>3}  {'胜' if row['won'] else '负'}  "
                  f"层 {row['floor']}  HP {row['hp']}/{row['max_hp']}  "
                  f"金 {row['gold']}  牌 {row['deck']}  步 {row['steps']}"
                  + (f"  ⚠ {row['stuck']}" if row["stuck"] else ""))

    wins = sum(r["won"] for r in rows)
    n = len(rows)
    stuck = [r for r in rows if r["stuck"]]
    print()
    print(f"局数        {n}")
    print(f"胜率        {wins}/{n} = {wins / n:.0%}")
    print(f"平均楼层    {statistics.mean(r['floor'] for r in rows):.2f} / {mock_sts2.MAX_FLOOR}")
    print(f"平均剩余HP  {statistics.mean(r['hp'] for r in rows):.1f} "
          f"(占最大 HP {statistics.mean(r['hp'] / r['max_hp'] for r in rows):.0%})")
    print(f"平均卡组    {statistics.mean(r['deck'] for r in rows):.1f} 张")
    print(f"平均步数    {statistics.mean(r['steps'] for r in rows):.1f}")
    print(f"卡住/非法   {len(stuck)} 局")
    if stuck:
        for r in stuck[:5]:
            print(f"  seed {r['seed']}: {r['stuck']}")

    if not had_data and stray.exists():
        shutil.rmtree(stray, ignore_errors=True)
    return 0 if not stuck and wins >= n * 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
