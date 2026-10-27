# 恶魔之塔 Zero · 直播 AI 代打

鲸语自己开播、自己打游戏、自己说话。你只管看。

## 双击就完事

1. 双击 **`启动.bat`**
2. 完

启动后它会依次自动完成：开界面 → 拉起 Godot → 开始代打 → 从主菜单打到死亡或通关。
关掉命令行窗口，游戏和语音引擎会一起收掉，不留后台进程。

首次运行 `启动.bat` 会自动建 Python 环境装依赖（约 3~5 分钟）。
需要已装 `uv`（`pip install uv`），没装的话脚本会提示。

## 只有一件事要你配一次

**模型**。因为要花你的额度，我不预填。

打开 `http://127.0.0.1:8787` → 「AI 设置」→ 选 **OpenAI 兼容 API**，填：

| 项 | 值 |
|---|---|
| 接口地址 | `http://127.0.0.1:8790/v1` |
| 模型名 | `deepseek-v4.1-flash` |
| API Key | 你那个 `sk-...` |

填完点保存。之后每次双击都会自动用它，不用再碰。

想单独让打牌用别的模型（比如换强的模型打 Boss），在「游戏代打」那一栏单独填，留空就沿用上面的。

## 声音

用的是 **Qwen3-TTS 音色克隆**，跟你视频里的「大肥鱼」是同一个声音：

- 引擎：`ttsd` 常驻流式服务（`C:\gamedata\ttsd\ttsd.py`，独立 venv），走 `faster-qwen3-tts` 的 CUDA Graph 推理
- 音色参考：`C:\max\ComfyUI\input\dafeiyu_voice_ref.wav`
- 模型：`Qwen3-TTS-12Hz-1.7B-Base`（x_vector 模式，不需要参考文本）

流式是关键：以前整句合成完才播，一句要 5~7 秒，牌早打完了声音才出来；现在边生成边播，
实测**开口延迟 0.34~0.40 秒**，生成速度约 1.9~2.5 倍于播放速度（RTF>1），所以声音一路领先、不会断流。
代打仍然是「先出牌、再说话」，下一手动作前才等这句播完，音画就对得上。

`ttsd` 会在代打开始前自动拉起，模型上卡约 11 秒（比 ComfyUI 的 30~40 秒快），占用约 5.5G 显存。
它起来了就**不再碰 ComfyUI**；只有流式引擎不可用时才退回 ComfyUI 整句合成。
想直接换回老路：语音设置里勾掉「Qwen3-TTS 流式」，或把 `tts.stream_enabled` 改 `false`。

想换音色：把新参考音频丢进 `C:\max\ComfyUI\input\`，改 `tts.qwen_ref_audio` 为文件名即可。

降级链：流式 Qwen3-TTS → ComfyUI Qwen3-TTS → GPT-SoVITS → Windows 系统语音。

注意：`ttsd` 监听 **127.0.0.1:8899**，只读弹幕不发消息。8790 端口本机已被 WorkBuddy2API 占用，别再往那儿放服务。


## 画面

- **游戏窗口**：OBS 「窗口采集」抓 Godot 窗口
- **Live2D 皮套**：浏览器源 `http://127.0.0.1:8787/live2d`
- **纯字幕**：`http://127.0.0.1:8787/overlay`
- **歌单**：`http://127.0.0.1:8787/songs`

## 它是怎么连上的

```
Godot 游戏（鲸语自动拉起）
   └─ addons/demontower_mcp/net_bridge.gd   ← 游戏内 autoload，127.0.0.1:9787
        ├─ game_read.gd   读状态（血量/手牌/敌人意图/可选节点/奖励候选…）
        └─ game_act.gd    执行动作（出牌/结束回合/选路/选卡/商店/篝火/事件/复活）
                ↑ 行分隔 JSON
鲸语 WhaleTalk
   ├─ dtgame/launcher.py   启动并守住 Godot 进程
   ├─ dtgame/player.py     决策循环：读状态 → 问模型 → 执行 → 说台词
   ├─ dtgame/view.py       把状态渲染成紧凑文本
   └─ dtgame/mcp_server.py 标准 MCP server（给外部 MCP 客户端用）
```

游戏内的桥只调游戏自己的函数（`_resolve_card_play`、`_on_end_turn`、`_on_node_clicked`），
所以能量校验、姿态限制、动画演出、存档全部和真人点击一致，不是另写一套简化逻辑。

## 界面上的按钮

| 按钮 | 作用 |
|---|---|
| 开始代打 | 需要时自动拉起游戏后开打 |
| 停止 | 停代打，游戏留着 |
| 启动游戏 / 关闭游戏 | 单独控制 Godot 进程 |
| 启动时自动代打 | 关掉它，双击后只开界面不自动打 |

## 配置项（`config.json` → `dtgame`）

| 键 | 说明 |
|---|---|
| `enabled` | 启动时自动代打，默认开 |
| `auto_launch` | 代打时自动拉起游戏，默认开 |
| `project_dir` | Godot 项目目录 |
| `godot_exe` | Godot 路径，留空自动查找 |
| `openai_base` / `openai_model` / `openai_key` | 留空沿用 `ai` 段 |
| `narrate` | 台词是否出声 |
| `step_interval` | 每步间隔秒数，太小看不清演出 |

## 已知限制

- 桥只监听本机回环地址，没有鉴权。别外网暴露 9787。
- 存档槽选择、账号继承、付费复活这几个界面没接自动化，卡住需要手点一下。
- 模型偶尔输出格式不对，代码会跳过重试；连续失败 5 次会停下并在日志里说明原因，不会空转。
- `config.json` 存 B 站 cookie 和 API key，不要外传。
