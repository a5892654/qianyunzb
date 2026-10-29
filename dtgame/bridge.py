"""恶魔之塔 Zero 游戏桥客户端 + 动作语义封装。

Godot 侧 addons/demontower_mcp/net_bridge.gd 监听 127.0.0.1:9787，
收行分隔 JSON，回一行 JSON。这里只做转发与超时，不做业务判断。
"""

import json
import socket
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9787


class BridgeError(RuntimeError):
    pass


class GameBridge:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port

    def call(self, cmd: str, args: dict | None = None, timeout: float = 45.0) -> dict:
        payload = json.dumps({"cmd": cmd, "args": args or {}}, ensure_ascii=False) + "\n"
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as s:
                s.sendall(payload.encode("utf-8"))
                buf = b""
                deadline = time.time() + timeout
                while not buf.endswith(b"\n"):
                    if time.time() > deadline:
                        raise BridgeError(f"{cmd} 超时（{timeout}s）")
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            raise BridgeError(f"连不上游戏桥 {self.host}:{self.port}——游戏在跑吗？({e})") from e
        if not buf.strip():
            raise BridgeError(f"{cmd} 没有返回")
        try:
            return json.loads(buf.decode("utf-8", "replace"))
        except ValueError as e:
            raise BridgeError(f"{cmd} 返回的不是 JSON：{buf[:200]!r}") from e

    # ---------- 读 ----------
    def screen(self) -> dict:
        return self.call("screen", timeout=10).get("screen", {})

    def state(self) -> dict:
        r = self.call("state", timeout=20)
        return r.get("state", {})

    # ---------- 写 ----------
    def play_card(self, slot: int = -1, card_id: str = "", target: int = -1) -> dict:
        return self.call("play_card", {"slot": slot, "id": card_id, "target": target})

    def end_turn(self) -> dict:
        return self.call("end_turn", timeout=60)

    def use_potion(self, slot: int, target: int = -1) -> dict:
        return self.call("use_potion", {"slot": slot, "target": target})

    def use_estus(self, ash: bool = False) -> dict:
        """元素瓶（ash=true 是灰元素瓶）——战斗里的免费动作，不会结束回合。"""
        return self.call("use_estus", {"ash": ash})

    def soul_forge(self, op: str, slot: int = -1) -> dict:
        """灵魂锻造：op 走 card / option / forge / back 四步，升级卡有动画，给足超时。"""
        return self.call("soul_forge", {"op": op, "slot": slot}, timeout=60)

    def discard_potion(self, slot: int) -> dict:
        return self.call("discard_potion", {"slot": slot})

    def choose_node(self, col: int, row: int) -> dict:
        return self.call("choose_node", {"col": col, "row": row})

    def pick_reward(self, slot: int = -1, skip: bool = False) -> dict:
        return self.call("pick_reward", {"slot": slot, "skip": skip})

    def campfire(self, action: str = "", slot: int = -1) -> dict:
        return self.call("campfire", {"action": action, "slot": slot}, timeout=60)

    def shop(self, action: str = "buy", slot: int = -1) -> dict:
        return self.call("shop", {"action": action, "slot": slot})

    def event(self, slot: int, window_id: str = "", revision: str = "") -> dict:
        args = {"slot": slot}
        if window_id:
            args.update(window_id=window_id, revision=revision)
        return self.call("event", args)

    def overlay(self, slot: int = -1, op: str = "", window_id: str = "", revision: str = "") -> dict:
        args = {"slot": slot, "op": op}
        if window_id:
            args.update(window_id=window_id, revision=revision)
        return self.call("overlay", args)

    def use_radiance_art(self, target: int = -1, require_ultimate: bool = True) -> dict:
        return self.call("use_radiance_art", {"target": target, "require_ultimate": require_ultimate},
                         timeout=90)

    def confirm_kill(self, confirm: bool) -> dict:
        return self.call("confirm_kill", {"confirm": confirm})

    # ---------- 开局 ----------
    def enter_hall(self) -> dict:
        return self.call("enter_hall", timeout=30)

    def begin_expedition(self, depth: int | None = None, mode: str = "deep",
                         skip_tutorial: bool = True) -> dict:
        """代打只开深层探索：mode=deep + skip_tutorial 让游戏按「我是高手」口径解锁。"""
        args: dict = {"mode": mode, "skip_tutorial": skip_tutorial}
        if depth is not None:
            args["depth"] = depth
        return self.call("begin_expedition", args, timeout=60)

    def hall_run(self, op: str = "continue", mode: str = "") -> dict:
        """大厅「未完成的出征」：continue 回地图续玩，restart 结算后重新出征。"""
        args: dict = {"op": op}
        if mode:
            args["mode"] = mode
        return self.call("hall_run", args, timeout=60)

    def abandon_run(self) -> dict:
        """放弃当前出征回大厅（只用于把浅层/教学局清掉，代打只保留深层）。"""
        return self.call("abandon_run", timeout=60)

    def pick_character(self, slot: int = -1, char_id: str = "") -> dict:
        return self.call("pick_character", {"slot": slot, "id": char_id}, timeout=60)

    def continue_run(self) -> dict:
        return self.call("continue_run", timeout=60)

    def confirm_result(self) -> dict:
        return self.call("confirm_result", timeout=40)

    def confirm_death_analysis(self) -> dict:
        return self.call("confirm_death_analysis", timeout=20)

    def close_loot(self, to_reward: bool = True) -> dict:
        return self.call("close_loot", {"to_reward": to_reward}, timeout=40)

    def revive(self) -> dict:
        return self.call("revive", timeout=60)

    def return_to_hall(self) -> dict:
        """本局结算页按「返回大厅」，等到游戏真回到大厅才返回。"""
        return self.call("return_to_hall", timeout=40)
