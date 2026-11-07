# AstrBot × 杀戮尖塔 2 自动打牌插件

让 AstrBot 里的 AI 真的去玩《杀戮尖塔 2》（Slay the Spire 2）：插件通过游戏内 mod
暴露的本地 HTTP 接口读取**引擎真值**（血量、手牌、敌人意图、地图节点……），再由
「规则机器人打底 + 可选 LLM 决策」把动作发回游戏。

- 不是截图识别、不是模拟点击：没有坐标漂移、没有卡面文字 OCR、回合制也不怕慢。
- 纯文本模型就能玩；默认配置下 **0 token**（规则层全包），打开 LLM 时只在关键界面问一次。
- **会打牌，也会组卡**：战斗按伤害/格挡/斩杀算分，选牌按卡组缺口（缺攻击补攻击、缺格挡补格挡、
  牌组过厚就收紧门槛），营火知道该升级还是睡觉，商店知道该买牌还是花钱删牌。
- 插件只有单文件 `main.py`，不依赖额外 pip 包（`aiohttp` 是 AstrBot 自带的）。
- **两套后端都支持**：`STS2-Agent`（适配 v0.111+，推荐）和老的 `STS2_MCP`（v0.103.x），
  插件启动时读 `/health` 自动识别，端口写错也会自动换到另一个。

```
AstrBot 群聊 / WebUI
        │  /sts2 自动 20        或   主模型调用 llm_tool: sts2_state / sts2_action
        ▼
 astrbot_plugin_sts2_player (main.py)
        │  ①GET  /state 或 /api/v1/singleplayer   → 引擎真值 JSON
        │  ②render_state() 压缩成中文短文本        → 规则层 / LLM 决策
        │  ③POST /action 或 /api/v1/singleplayer   → play_card / end_turn / choose_map_node ...
        ▼
 STS2AIAgent.dll  (游戏 mod)  http://127.0.0.1:8080
        ▼
 《杀戮尖塔 2》本体（Early Access，v0.111.x）
```

## 一、准备三件事

### 1. 游戏侧：装 STS2-Agent mod（推荐）

1. 游戏要 **v0.111.0 或更高**（mod 清单里写死了 `min_game_version: 0.111.0`，
   低版本会在日志里报 `declared min game version ... is higher than the current game version`）。
   本机就是这么升上去的：Steam → 库 → 右键游戏 → 属性 → 测试版 → 选 `public-beta`，
   更新完 `release_info.json` 里的 `version` 就是 `v0.111.0`。
2. 二选一装 mod：

   - **创意工坊**（最省事）：订阅 `id=3796486050`，用 Steam 的「Play with Mods」启动；
   - **手动**：下载 [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent) 的 release，
     把 `mod/` 里的**三个文件**拷进游戏的 `mods/` 目录：

     ```
     D:\steam\steamapps\common\Slay the Spire 2\mods\STS2AIAgent.dll
     D:\steam\steamapps\common\Slay the Spire 2\mods\STS2AIAgent.pck
     D:\steam\steamapps\common\Slay the Spire 2\mods\STS2AIAgent.json   ← 由 mod_id.json 改名
     ```

     ⚠️ 配置文件必须叫 `STS2AIAgent.json`（= mod id），叫 `mod_id.json` 游戏根本不认；
     也不要同时用创意工坊和手动安装，会被加载两次。
3. 启动游戏，进到主菜单，验证服务：

   ```powershell
   curl http://127.0.0.1:8080/health
   # {"ok":true,...,"data":{"service":"sts2-ai-agent","mod_version":"0.17.0","game_version":"v0.111.0",...}}
   ```

**更老的游戏（v0.103.x 及以前）** 只能用老的 `STS2_MCP`：把 `STS2_MCP.dll` + `STS2_MCP.json`
放进 `mods/`，接口在 `http://127.0.0.1:15526`。它已经跟不上 0.107+ 的战斗接口
（`MissingMethodException: CombatManager.get_IsPlayPhase`），在老版本上才可用。

### 2. AstrBot 侧：装插件

本仓库的根目录就是插件目录。把整个仓库目录改名（或复制）成 `astrbot_plugin_sts2_player/`
放进 AstrBot 的 `data/plugins/` 下，重载插件（或在 WebUI 的插件管理里用 zip 安装，
zip 用 `python tools/pack.py` 生成）。加载后会有：

- 指令 `/sts2`（状态 / 自动 <n> / 执行 <动作> [json] / 停 / 帮助）
- LLM 工具 `sts2_state`、`sts2_action`（主模型可以直接在聊天里读状态、下动作）

**没有 QQ/微信账号也能先试**：AstrBot 自带 `webchat` 平台，在实例的 `data/cmd_config.json`
的 `platform` 里加一条 `{"id": "webchat", "type": "webchat", "enable": true, "config": {}}`，
重启后打开 WebUI（默认 `http://localhost:6185`，用户名 `astrbot`）→ 「对话」页，
直接发 `/sts2 状态` 就能看到游戏画面快照。

装好之后建议跑一次自检（会读实例里那份插件 + 真游戏接口，只读不动作）：

```powershell
$py tools\check_installed.py          # 打印状态 / 帮助 / 后端健康
$py tools\check_installed.py --act 6  # 再真打 6 个决策
```

### 3. 配置

WebUI → 插件 → `astrbot_plugin_sts2_player`：

| 键 | 默认 | 说明 |
|---|---|---|
| `sts2_url` | `http://127.0.0.1:15526` | mod 的本地地址（STS2-Agent 是 `8080`；写错会自动试另一个端口） |
| `sts2_timeout_sec` | 20 | 单次请求超时；游戏卡顿时别调太小 |
| `operator_ids` | `[]` | 允许操作游戏的人，空 = 所有人 |
| `max_steps` | 40 | 一次 `/sts2 自动` 最多几步 |
| `step_interval_sec` | 0.3 | 两步间隔，给动画留时间 |
| `report_every` | 5 | 每几步在聊天里播报一次 |
| `use_llm` | false | 打开后关键界面问模型（战斗默认仍走规则层） |
| `llm_combat` | false | 战斗中是否也问模型（很贵，不建议） |
| `potion_hp_ratio` | 0.32 | 血量低于该比例时喝药 |
| `block_priority` | 0.15 | 格挡权重：调高更保守（叠甲多、打得慢），调低更激进（抢血） |
| `card_min` | 18 | 选牌阈值：评分高于它才拿（Common 12 / Uncommon 20 / Rare 30 起步） |
| `auto_restart` | true | 死了自动回主菜单再开一局（一次自动最多重开 3 次） |
| `goal` | 稳一点打…… | 写进提示词的长期目标 |
| `llm_system_prompt` | 空 | 自定义系统提示词，空 = 用内置动作说明 |

## 二、怎么用

在群里（或 WebUI 会话里）：

```
/sts2                 # 看当前状态（压缩后的中文快照）
/sts2 自动 20         # 自动打 20 步（一步 = 读一次状态 + 一个动作）
/sts2 执行 play_card {"card_index":0,"target":"JAW_WORM_0"}
/sts2 执行 end_turn
/sts2 停
/sts2 帮助            # 动作名 + 参数速查
```

也可以让主模型自己玩：模型调用 `sts2_state` 拿状态，调用 `sts2_action` 下动作——适合
「先看一眼再决定」的对话式玩法。

## 三、决策架构

**规则层（`RuleBot`）负责全部战斗**，一次只出一个动作（`next_action(state)`），
每步都重新读状态，天然规避「出牌后手牌索引左移」这个最大的坑：

- 攻击：伤害/费用比，能斩杀则大幅加权；单体优先补刀/打最疼的敌人
- 格挡：按 `min(格挡值, 本回合要吃的伤害)` 计分，并乘上危险系数（要吃的伤害相对血量越大越优先挡）；
  正好挡住全部伤害时额外加分
- 能力牌早期优先，抽牌/回能次级；自伤牌（Offering 这类 `Lose 6 HP`）在血量付不起时直接否决
- 精英/Boss 战把喝药阈值抬到 55%，避免「药水带进棺材」
- 非战斗界面（奖励、选牌、地图、事件、营火、商店、宝箱、选牌覆盖层、水晶球）各有对应策略

**构筑层（`_deck_fit` / `_upgrade_value` / `_worst_card`）决定这副牌往哪个方向长**，
不再只看单卡稀有度：

- 选牌先算当前卡组画像（攻击牌占比、格挡来源占比、能力牌数量、总张数、同名重复），
  缺攻击就抬攻击牌、缺格挡就抬格挡牌，能力牌超过 4 张就不再要
- **牌组越厚门槛越高**（超过 14 张，每多一张门槛 +2.2）：塔牌里「薄」才是强，
  20 张的杂牌堆打不过 15 张的精牌组
- 同名牌超过两张直接压分（第三张打击/防御基本是占牌位）
- 营火：血量 ≥65% 就升级而不是睡觉，升级目标按「痛击（上易伤）> 能力牌 > 每点能量收益」排序
- 商店：牌组厚到 14 张以上时，优先买 75 金的删牌服务，删掉最没用的那张（基础打击/防御），
  而不是再塞一张新牌进去

**LLM 层是可选的**：`use_llm=true` 且（非战斗界面 或 `llm_combat=true`）时，把压缩状态交给模型，
模型返回 `{"actions":[{...}]}` 就执行（最多 4 个），解析失败或模型挂了自动回退规则层。

**状态压缩**是这个插件省 token 的关键。实测一场普通战斗：

| 形态 | 字符数 | 约合 token |
|---|---|---|
| mod 原始 JSON | 2508 | ~800 |
| `render_state()` 中文快照 | 347 | ~120 |

社区作者给的参考是「整局裸喂全量 JSON：Claude Sonnet 4.6 ≈ 8M tokens、GPT-5.4 ≈ 7.34M」，
这也是本项目把战斗交给规则层的直接原因——**一场战斗十几到几十次决策，全问模型既慢又贵**。

## 四、本地自测（不需要真游戏）

`tools/` 下有三个东西：

- `mock_sts2.py`：按 STS2MCP 协议写的模拟服务器（战斗/奖励/选牌/地图/营火/商店/宝箱/事件/
  水晶球/假商人/游戏结束），可单独跑：`python tools/mock_sts2.py --port 15526`
- `test_plugin.py`：起模拟服务器 → 真 import 插件 → 用假 Context/Provider/Event 驱动 `/sts2 自动`，
  断言「打到第 6 层 Boss 并把它打掉」「选牌不拿诅咒」「LLM 动作真的生效」
- `bench.py`：**批量跑分**。不经过 HTTP，直接驱动模拟器的状态机 + 规则层，跑 N 个种子，
  输出胜率/平均楼层/平均剩余血量/平均步数，用来客观比较参数好坏（`--bp` `--pick` `--potion`）
- `trace_bot.py`：逐步打印规则机器人的每个决策，用来人工检查打法
- `live_plugin.py`：**打真游戏**（走插件本体，不是旁路脚本）。`python tools/live_plugin.py --steps 200 --bp 0.6 --start-run`
  会自动过菜单、开一局、一路打到你停手；`--verbose` 打印每个动作的返回值
- `check_installed.py`：**验机**。直接加载 AstrBot 实例里那份插件（不是工作区这份），跑
  `/sts2 状态` + 健康检查；加 `--act N` 还会真打 N 步；加 `--zip dist/xxx.zip` 则先解开安装包再验
- `pack.py`：**打包**。产出一个能被 WebUI「从文件安装」接受的 zip，并用 AstrBot 自己的
  `_PluginUpdater.validate_plugin_archive` 做校验
- `make_logo.py`：生成插件列表用的 `logo.png`
- `live_agent.py`：早期的独立极简驱动（只用来验证接口，逻辑比插件本体简陋）

用 AstrBot 实例自带的 venv 跑（不需要额外装包）：

```powershell
$py = 'C:\Users\Administrator\.astrbot_launcher\instances\<实例id>\venv\Scripts\python.exe'
& $py tools\test_plugin.py            # 端到端
& $py tools\bench.py --seeds 100      # 批量跑分
& $py tools\trace_bot.py 60           # 逐步看决策
```

最近一次跑通的记录：模拟局从 80/80HP 出发，打完第 1 幕第 6 层 Boss（Vantom 85HP，攻击
10/12/18 轮换），结束时 12/80HP、149 金，全程无「连续失败」。

### 跑分基线（120 局，默认参数）

| 指标 | 构筑层 + 连招层 | 只看单卡（旧） |
|---|---|---|
| 胜率 | 109/120 = **91%** | 48/60 = 80% |
| 平均剩余血量 | 25.0 / 80（31%） | 20.6 / 80（26%） |
| 平均卡组 | 14.7 张 | 14.3 张 |
| 平均步数 | 72.6 | 71.7 |
| 卡住/非法动作 | 0 | 0 |

模拟器现在也真的会算易伤了（`VULNERABLE` 让受到攻击伤害 ×1.5，回合结束衰减），
所以「先痛击挂易伤、再打击」这条连招在跑分里能被看见——之前不加这段机制时，
连招层反而会让胜率掉 10 个百分点（等于白花 2 能量）。

参数扫描（60 局一批，同一套模拟器）：

| `block_priority` | 胜率 | 平均步数 | 备注 |
|---|---|---|---|
| 0.15（默认） | 85% | 70.8 | 基本不叠甲，纯抢血 |
| 1.0 | 87% | 71.2 | 差不多 |
| 2.0 | 72% | 135.4 | 明显变慢，开始磨甲 |

结论：这套模拟器里「能打死就别挡」是数学上更优的（挡 5 点要花 1 能量 = 少 6 伤，而伤害是
阈值资源）；真实游戏敌人血量/成长更高、卡组更厚，格挡权重应该往上调。这个数字的意义不是
「机器人有 90% 胜率就能通关真实游戏」，而是**同一套参数换一下就能比较出哪个更好**。

`tools/test_plugin.py` 里还有 7 条构筑层的单元断言（缺攻击补攻击、缺格挡补格挡、牌组过厚跳过、
升级选痛击、删牌删打击、满血升级残血睡觉、牌组厚了买删牌）和 3 条连招断言（打不死先挂易伤、
已有易伤不重复挂、能斩杀时不绕路），改权重时先跑它。

## 五、真机实测（STS2 v0.111.0 + STS2-Agent v0.17.0）

真游戏上用**插件本体**（不是旁路脚本）跑 `tools/live_plugin.py`——它走的就是
`/sts2 自动` 的完整链路：`Sts2Client` 识别后端 → `agent_to_legacy` 把 STS2-Agent 的状态
压成插件原来吃的那套字段 → `RuleBot` 决策 → 动作翻译回 STS2-Agent 的原生动作。

| 局 | 结果 | 死因 |
|---|---|---|
| 第 1 局（接着存档打） | 第 17 层 | Boss，手里没牌可挡 |
| 第 2 局 | 第 11 层 | 一路抢血，血线越拖越低 |
| 第 3 局（修完卡牌识别/保命逻辑） | **第 15 层** | 第 15 层战斗 |
| 第 4 局（加构筑层，未接连招门控） | 第 8 层 | 双敌 +「缩小」debuff，打击只剩 4 伤 |
| 第 5 局（构筑层 + 连招门控） | 第 12 层 | 第 12 层战斗，卡组里已有「黑暗之拥」 |

修掉的三个真机专属问题：

1. **卡牌类别靠文本猜**：v0.111 的手牌没有 `card_type`，原来「文本里有『造成』和『伤害』
   就算攻击牌」，把「巨像（获得 4 格挡；有易伤的敌人对你造成的伤害降低 50%）」误判成攻击牌，
   每回合白扔 1 点能量。现在要求匹配 `造成 N 点伤害`。
2. **商店开门/关门振荡**：STS2-Agent 要「打开货架」才能买、「关上」才能走，规则层原来
   开→关→开无限循环（实测 100 步原地打转）。现在先算「想不想买」，不想买直接走。
3. **它自己算好了会不会死**：`combat.end_turn_will_kill_player` 是引擎给的结论，现在映射成
   `battle.lethal`：这回合会死就优先喝药、格挡评分 ×4。

另外真机上踩到的坑：`act_id` 是字符串 `"0"`；意图标签带 BBCode（`4[font_size=18]×2[/font_size]`），
伤害要洗成 8；`run.potions` 里空槽是 `occupied:false`；商店空药水位是 `is_stocked:false` + `name:null`。

## 六、已知限制

- 规则层能稳定走完前十几层（真机最好成绩第 17 层），但**还是打不过第 1 幕 Boss**：构筑层会
  「缺什么补什么」，却仍然看不懂具体牌的机制（比如「每当你给予易伤时抽牌」要不要拿，取决于
  卡组里有多少张牌能给易伤），也算不出「易伤 + 痛击 + 多段攻击」这类连招的期望伤害。
  要更强就打开 `use_llm`（关键界面问模型）或 `llm_combat`（战斗也问，很贵）。
- 真机成绩波动很大（同一套代码，8 层到 17 层都出现过），前 10 层的运气（敌人组合、有没有商店、
  有没有营火）影响比规则层微调更大；模拟器 90% 胜率不能直接翻译成真游戏胜率。
- 游戏是 Early Access，mod 接口随版本变：`MIN_GAME_VERSION` 对不上时 mod 根本不加载
  （STS2-Agent 0.17.0 要求 ≥ v0.111.0）；`state_type` 出现未知值时插件会「不知道怎么打就停下」，
  而不是瞎点。
- 只接管**对局内**操作：主菜单/角色选择已能自动过（`continue_run`→`embark`），但首次进新档、
  时间线解锁这类界面仍可能需要人点一下。
- 没有推送，只能轮询；`end_turn` 之后要读两次才能看到新一手牌（插件已按间隔处理）。
- 单人/多人互斥：游戏在多人模式时请求返回 409，插件会提示切回单人。
- 规则层参数是在**自建模拟器**上调的，不是真实游戏的统计最优；真机上建议先用 `/sts2 自动 40`
  打两局看 log，再改 `block_priority` / `card_min`（真机上 `0.6` 比模拟器的最优 `0.15` 稳）。

## 七、为什么不做「截图 + 模拟点击」

上一轮做《文明 6》时试过像素路线（截图 → VLM 给坐标 → SendInput 注入）：游戏用
Raw Input / 过滤注入，注入基本无效，而且需要能看图的模型。STS2 虽然回合制无速度要求，
但卡面文字、手牌位置、拖拽出牌这些仍然脆弱，且每步都要把整张图喂给模型。
状态级接口能拿到 100% 准确的 `card.can_play` / `unplayable_reason` / 敌人意图数字，
校验与回退都简单得多，所以这里只做状态级路线。

## 八、打包与上架

AstrBot 的「从文件安装」吃的是一个 zip，里面必须有 `metadata.yaml`（放根目录或唯一的顶层目录都行）。
本仓库已经把这步脚本化：

```powershell
$py = 'C:\Users\Administrator\.astrbot_launcher\instances\<实例id>\venv\Scripts\python.exe'
& $py tools\pack.py
# ✅ 打包完成：dist\astrbot_plugin_sts2_player-v0.4.1.zip
#    校验通过：metadata 位于 astrbot_plugin_sts2_player/metadata.yaml
```

`pack.py` 做三件事：按白名单收文件（`main.py` / `metadata.yaml` / `_conf_schema.json` / `logo.png` /
`README.md`）、用 AstrBot 的 `_PluginUpdater.validate_plugin_archive` 校验、打印 sha256。

想连包一起验「解开后能不能真跑」，再加一条：

```powershell
& $py tools\check_installed.py --zip dist\astrbot_plugin_sts2_player-v0.4.1.zip
# ✅ 安装包解开后可以正常工作
```

### 让它带「检查更新」按钮

WebUI 只有在 `metadata.yaml` 的 `repo` 非空、且安装方式被识别为仓库/市场时才给更新按钮。
填上你自己的仓库地址即可：

```powershell
& $py tools\pack.py --repo https://github.com/<你>/astrbot_plugin_sts2_player
```

上架到 AstrBot 插件市场还需要：仓库根目录有 `metadata.yaml`（字段 `name` / `desc` / `version` /
`author` 必填）、`logo.png`、`README.md`，按版本打 tag（`v0.4.1`），再把仓库地址提交到
[AstrBot 插件市场](https://github.com/AstrBotDevs/AstrBot_Plugins_Collection) 的收集仓库。

当前 `metadata.yaml`：

```yaml
name: astrbot_plugin_sts2_player
display_name: 杀戮尖塔 2 自动打牌
short_desc: 让 AI 自己打《杀戮尖塔 2》：读引擎真值、规则层出牌与卡组构筑，零 token 战斗
desc: 让 AI 玩《杀戮尖塔 2》：…（长描述，列表里展开看）
version: v0.4.1
author: dsh
repo: ""            # 填上仓库地址才能检查更新
astrbot_version: ">=4.16.0"
support_platforms: []   # 空 = 平台无关（纯指令 + LLM 工具，不依赖任何平台特性）
```

## 九、参考

- [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent)：当前推荐后端（v0.111+），本地 REST + MCP
- [Gennadiyev/STS2MCP](https://github.com/Gennadiyev/STS2MCP)（MIT）：老后端（v0.103.x），本插件最早按它实现
- [chiang881/sts2-compact-mcp](https://github.com/chiang881/sts2-compact-mcp)：token 压缩代理，思路相近
- AstrBot 插件规范：`Star` 子类 + `@filter.command` + `@filter.llm_tool`，`main.py` 单文件加载
