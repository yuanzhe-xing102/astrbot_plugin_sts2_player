# 杀戮尖塔 2 自动打牌（AstrBot 插件）

让 AI 在《杀戮尖塔 2》里自己打牌：读游戏真实状态、按启发式规则出牌、自动选牌/升级/删牌，
也可以在关键界面交给大模型决策。**战斗不烧 token**——默认整局下来模型的调用次数是 0。

> 依赖游戏侧的一个 mod 暴露本地 HTTP 接口（`STS2-Agent` 或 `STS2_MCP`）。没有 mod 也能装，
> 只是读不到状态。

## 它做什么

- `/sts2 状态`：把引擎返回的一大坨 JSON 压成中文快照（血量/能量/敌人意图/手牌/地图/奖励…），
  战斗态从 ~800 token 压到 ~120 token
- `/sts2 自动 20`：连续做 20 个决策。战斗用规则层（斩杀、格挡性价比、药水时机、保命、
  先易伤后输出），非战斗接了卡组构筑（缺什么补什么、牌组过厚收紧阈值、营火升级、商店删牌）
- 死了自动回主菜单再开一局（一次自动最多重开 3 次）
- 可选：`use_llm=true` 时把选牌/事件/路线交给当前会话的模型；`llm_combat=true` 连战斗也交给它（很贵）

## 安装

仓库根目录就是插件目录（`metadata.yaml` 在根上），三种装法任选：

- **WebUI 在线装**：插件 → 安装 → 填本仓库地址，AstrBot 会自己拉取
- **WebUI 离线装**：下载 release 里的 `astrbot_plugin_sts2_player-*.zip` → 插件 → 从文件安装
- **手动**：把仓库目录改名成 `astrbot_plugin_sts2_player`，放进实例的 `data/plugins/`，重启

装好先在游戏里进到主菜单，再发 `/sts2 状态`；读不到状态时 `/sts2 状态` 会打印排查清单。
开发/测试/打包脚本见 [docs/DEV.md](docs/DEV.md)。

## 指令

| 指令 | 说明 |
|---|---|
| `/sts2` | 当前状态（中文快照） |
| `/sts2 自动 [n]` | 自动打 n 步（默认 `max_steps`） |
| `/sts2 执行 <动作> [json]` | 手动下一个动作，如 `/sts2 执行 end_turn` |
| `/sts2 停` | 停在当前这一步之后 |
| `/sts2 帮助` | 动作名 + 参数速查 |

LLM 工具：`sts2_state`（读状态）、`sts2_action`（下动作），主模型可以在对话里直接玩。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `sts2_url` | `http://127.0.0.1:15526` | mod 的本地地址（STS2-Agent 是 `8080`；写错会自动试另一个端口） |
| `sts2_timeout_sec` | 20 | 单次请求超时 |
| `operator_ids` | `[]` | 允许操作游戏的人（空 = 所有人） |
| `max_steps` | 40 | 一次自动最多几步 |
| `step_interval_sec` | 0.3 | 两步间隔 |
| `report_every` | 5 | 每几步播报一次 |
| `use_llm` | false | 关键界面问模型 |
| `llm_combat` | false | 战斗也问模型（贵） |
| `potion_hp_ratio` | 0.32 | 血量低于该比例喝药 |
| `block_priority` | 0.15 | 格挡权重（真机建议 0.6） |
| `card_min` | 18 | 选牌阈值 |
| `auto_restart` | true | 死了自动重开一局 |

## 游戏侧怎么准备

1. 订阅 Steam 创意工坊的 **STS2 AI Agent**（或把 release 里 `mod/` 下的
   `STS2AIAgent.dll`、`STS2AIAgent.pck`、`mod_id.json`（重命名为 `STS2AIAgent.json`）
   放进 `<游戏目录>/mods/`）
2. 启动游戏，进到主菜单就有接口：`curl http://127.0.0.1:8080/health`
3. 注意 `STS2-Agent` 声明了 `min_game_version`（0.17.0 要求 `0.111.0`），游戏版本太老会拒绝加载
4. 老的 `STS2_MCP`（15526）也能用，但只支持较旧的游戏版本

## 已知限制

- 规则层打不过第 1 幕 Boss 很正常：它只会「先易伤再输出」这一种连招，
  不会算力量/敏捷叠加、不会攒牌打爆发
- 只接管「对局内」，菜单/商店手点之外的部分靠 mod 的 action 列表
- 游戏还在 Early Access，接口字段会随版本漂

## 许可

MIT
