"""恶魔之塔 Zero MCP server。

stdio MCP server，把游戏桥能力暴露成标准 MCP tools，
任何 MCP 客户端（Qoder / Claude Desktop / Codex 等）都能直接接管游戏。

运行：
    python -m dtgame.mcp_server

客户端配置（示例）：
    {
      "mcpServers": {
        "demontower": {
          "command": "python",
          "args": ["-m", "dtgame.mcp_server"],
          "cwd": "<鲸语目录>"
        }
      }
    }
"""

import asyncio
import json
import sys

from dtgame.bridge import BridgeError, GameBridge
from dtgame.expedition import MAX_DEPTH, depth_settings
from dtgame import view

PROTOCOL_VERSION = "2024-11-05"


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS = [
    {
        "name": "game_screen",
        "description": "查询游戏当前停在哪个界面（battle/map/reward/campfire/shop/event/menu）以及是否正在播动画。",
        "inputSchema": _schema({}),
    },
    {
        "name": "game_state",
        "description": "读取完整游戏状态并返回人读文本：血量、能量、手牌、敌人意图、可选节点、奖励候选等。做任何操作前先调这个。",
        "inputSchema": _schema({}),
    },
    {
        "name": "begin_expedition",
        "description": "在出征大厅选择已解锁的深度并开始新出征。先用 game_state 查看当前深度和解锁上限；深度只能在新局开始前调整。省略 depth 沿用游戏设置。",
        "inputSchema": _schema({
            "depth": {"type": "integer", "minimum": 0, "maximum": MAX_DEPTH,
                      "description": "出征深度，必须处于 game_state 显示的已解锁范围"},
        }),
    },
    {
        "name": "play_card",
        "description": "战斗中使用一张手牌。slot 是手牌序号；单体牌需要 target 指定敌人序号。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "手牌序号，见 game_state 的 hand"},
            "target": {"type": "integer", "description": "敌人序号，见 game_state 的 enemies"},
            "release": {"type": "boolean", "description": "仅格兰斯光剑/暗剑：false 蓄能，true 解放。先设置全手牌模式再出牌；合璧牌不要填。"},
        }, ["slot"]),
    },
    {
        "name": "set_grans_mode",
        "description": "切换格兰斯全手牌蓄能/解放模式。只切换开关，不出牌、不扣能量或蓄能；通常直接在 play_card 中带 release 更方便。",
        "inputSchema": _schema({"release": {"type": "boolean", "description": "true 解放，false 蓄能"}}, ["release"]),
    },
    {
        "name": "end_turn",
        "description": "战斗结束当前回合，把控制权交给敌人。会等到敌人回合结算完毕才返回。",
        "inputSchema": _schema({}),
    },
    {
        "name": "use_potion",
        "description": "战斗中使用一个道具（药水）。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "道具槽位"},
            "target": {"type": "integer", "description": "目标敌人序号，非指向性道具可省略"},
        }, ["slot"]),
    },
    {
        "name": "choose_node",
        "description": "在地图上选择一个可达节点前进。",
        "inputSchema": _schema({
            "col": {"type": "integer", "description": "列号"},
            "row": {"type": "integer", "description": "行号"},
        }, ["col", "row"]),
    },
    {
        "name": "pick_reward",
        "description": "在卡牌奖励界面选一张卡加入牌组，或用 skip=true 放弃。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "候选序号"},
            "skip": {"type": "boolean", "description": "true 表示放弃本次奖励"},
        }),
    },
    {
        "name": "campfire_action",
        "description": "篝火界面操作。slot 选择某个选项；或用 op=continue 直接离开篝火继续前进。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "选项序号"},
            "op": {"type": "string", "description": "填 continue 表示离开篝火"},
        }),
    },
    {
        "name": "shop_action",
        "description": "商店界面操作。slot 购买货架某项；或用 op=leave 离开商店。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "货架序号"},
            "op": {"type": "string", "description": "填 leave 表示离开商店"},
        }),
    },
    {
        "name": "event_choose",
        "description": "在当前事件面板选择未锁定选项；操作后返回真实状态。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "选项序号"},
            "window_id": {"type": "string", "description": "game_state 中的窗口标识"},
            "revision": {"type": "string", "description": "game_state 中的选项版本"},
        }, ["slot", "window_id", "revision"]),
    },
    {
        "name": "overlay_action",
        "description": "处理地图上的事件、弹窗和系统窗口。slot 选择选项；op=button 按控件序号点击；仅在状态允许时 op=back 关闭。返回是否真正关闭及操作后状态。",
        "inputSchema": _schema({
            "slot": {"type": "integer", "description": "选项或控件序号"},
            "op": {"type": "string", "enum": ["", "button", "back"]},
            "window_id": {"type": "string", "description": "game_state 中的窗口标识"},
            "revision": {"type": "string", "description": "game_state 中的选项版本"},
        }, ["window_id", "revision"]),
    },
]


class Server:
    def __init__(self):
        self.bridge = GameBridge()

    # ---------- 工具实现 ----------
    def call_tool(self, name: str, args: dict):
        b = self.bridge
        if name == "game_screen":
            return json.dumps(b.screen(), ensure_ascii=False)
        if name == "game_state":
            return view.render(b.state())
        if name == "begin_expedition":
            state = b.state()
            if state.get("screen") != "hall" or state.get("busy"):
                raise BridgeError("只能在可操作的出征大厅选择深度并开始新局")
            if "depth" in args:
                depth = args["depth"]
                _, limit = depth_settings(state)
                if limit is None:
                    raise BridgeError("游戏桥未提供已解锁深度上限，请省略 depth 沿用当前设置")
                if type(depth) is not int or not 0 <= depth <= limit:
                    raise ValueError(f"出征深度必须是 0～{limit} 的整数")
                return _j(b.begin_expedition(depth))
            return _j(b.begin_expedition())
        if name == "play_card":
            if "release" in args:
                if type(args["release"]) is not bool:
                    raise ValueError("release 必须是布尔值")
                return _j(b.play_card(int(args.get("slot", -1)), "", int(args.get("target", -1)), release=args["release"]))
            return _j(b.play_card(int(args.get("slot", -1)), "", int(args.get("target", -1))))
        if name == "set_grans_mode":
            if type(args.get("release")) is not bool:
                raise ValueError("release 必须是布尔值")
            b.state()  # 用这次读到的战斗/回合/手牌作为切换的前置条件。
            return _j(b.set_grans_mode(args["release"]))
        if name == "end_turn":
            return _j(b.end_turn())
        if name == "use_potion":
            return _j(b.use_potion(int(args.get("slot", -1)), int(args.get("target", -1))))
        if name == "choose_node":
            return _j(b.choose_node(int(args.get("col", -1)), int(args.get("row", -1))))
        if name == "pick_reward":
            return _j(b.pick_reward(int(args.get("slot", -1)), bool(args.get("skip", False))))
        if name == "campfire_action":
            return _j(b.campfire(str(args.get("op", "")), int(args.get("slot", -1))))
        if name == "shop_action":
            return _j(b.shop(str(args.get("op", "buy")), int(args.get("slot", -1))))
        if name == "event_choose":
            return _j(b.event(int(args.get("slot", -1)), str(args["window_id"]), str(args["revision"])))
        if name == "overlay_action":
            return _j(b.overlay(int(args.get("slot", -1)), str(args.get("op", "")),
                                str(args["window_id"]), str(args["revision"])))
        raise ValueError(f"未知工具：{name}")

    # ---------- JSON-RPC ----------
    def handle(self, req: dict) -> dict | None:
        method = req.get("method", "")
        rid = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demontower-zero", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                text = self.call_tool(name, args)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except (BridgeError, ValueError, KeyError, TypeError) as e:
                result = {"content": [{"type": "text", "text": f"失败：{e}"}], "isError": True}
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            result = {}
        else:
            if rid is None:
                return None
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未实现：{method}"}}

        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def _j(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    if obj.get("ok") is False:
        raise BridgeError(text)
    return text


def main():
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = server.handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
