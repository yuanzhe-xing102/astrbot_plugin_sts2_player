"""逐步打印规则机器人的决策，用来人工检查打法是否合理。

用法（在 astrbot-sts2-player 目录下）：
    <实例 venv python> tools\\trace_bot.py [最大步数] [--seed N]
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(r"C:\Users\Administrator\.astrbot_launcher\instances"
                           r"\2f597663-e149-4d1f-8644-244dcd5d4e6c\core")))

import mock_sts2  # noqa: E402


def load_plugin():
    path = ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("sts2_plugin_main", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sts2_plugin_main"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


async def main() -> int:
    argv = sys.argv[1:]
    seed = 7
    if "--seed" in argv:
        i = argv.index("--seed")
        seed = int(argv[i + 1])
        del argv[i:i + 2]
    steps = int(argv[0]) if argv else 200
    plugin_mod = load_plugin()
    mock_sts2.GAME = mock_sts2.Game(seed=seed)  # Handler 每次调用都读模块级 GAME
    server = mock_sts2.create_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    client = plugin_mod.Sts2Client(f"http://127.0.0.1:{port}")
    bot = plugin_mod.RuleBot()
    same = 0
    last_key = ""
    try:
        for i in range(steps):
            st = await client.state()
            if not isinstance(st, dict):
                print(f"[{i}] 非状态：{st!r}")
                break
            key = plugin_mod.render_state(st, pile_limit=3)
            same = same + 1 if key == last_key else 0
            last_key = key
            action = bot.next_action(st)
            print(f"\n===== 第 {i} 步 state_type={st.get('state_type')} 动作={action} =====")
            print(key)
            if action is None:
                print("（机器人没有动作，停下）")
                break
            res = await client.act(action["action"], **{k: v for k, v in action.items() if k != "action"})
            print("结果：", res)
            if same >= 3:
                print("（状态原地打转，停下）")
                break
        final = await client.state()
        print("\n===== 最终状态 =====")
        print(plugin_mod.render_state(final))
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
