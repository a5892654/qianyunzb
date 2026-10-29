"""启动隔离存档的 Godot 实战夹具，验证 Player→TCP→模式切换→实际出牌。"""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame.bridge import BridgeError, GameBridge
from dtgame.player import Player


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


async def exercise(bridge):
    deadline = time.monotonic() + 35
    while True:
        try:
            await asyncio.to_thread(bridge.screen)
            break
        except BridgeError:
            if time.monotonic() >= deadline: raise
            await asyncio.sleep(0.2)
    player = Player({}, bridge, narrate=False)
    plan = [("light", False), ("dark", True), ("light", True)]
    selected = []
    def decide(text):
        side, release = plan[len(selected)]
        current = player._last_state
        assert current["battle"]["grans"]["available"], text
        card = next(c for c in current["battle"]["hand"] if c["grans"]["sword"] == side)
        selected.append((side, release))
        return {"action": "play", "slot": card["slot"], "id": card["id"], "target": 0, "release": release}
    player._ask = AsyncMock(side_effect=decide)
    successes = 0
    while successes < len(plan):
        if time.monotonic() > deadline: raise AssertionError("实际出牌链超时")
        result = await player.step()
        if result.get("action", {}).get("action") == "wait" or result.get("stale"):
            await asyncio.sleep(0.1)
            continue
        assert result["ok"], result
        successes += 1
    final = await asyncio.to_thread(bridge.state)
    g, battle = final["battle"]["grans"], final["battle"]
    assert (g["light"], g["dark"], battle["energy"], battle["block"]) == (0, 1, 0, 9), final
    assert g["release_mode"] is True, final
    assert player._ask.await_count == 3
    await asyncio.to_thread(bridge.call, "test_finish", timeout=2)
    return {"sequence": selected, "light": g["light"], "dark": g["dark"], "energy": battle["energy"], "block": battle["block"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    output = project.parent / "outputs" / ("whaletalk_grans_live_" + uuid.uuid4().hex)
    output.mkdir(parents=True)
    env = os.environ.copy()
    env.update(APPDATA=str(output / "userdata"), DEMON_TOWER_TEST_USERDATA_ROOT=str(output / "userdata"),
               DEMON_TOWER_TEST_SERVER="", WHALETALK_GRANS_NO_TCP="0")
    primary, extra = free_port(), free_port()
    while extra == primary: extra = free_port()
    env.update(WHALETALK_TEST_PRIMARY_PORT=str(primary), WHALETALK_GRANS_PORT=str(extra))
    log = output / "godot.log"
    with log.open("w", encoding="utf-8") as stream:
        proc = subprocess.Popen([str(args.godot), "--headless", "--path", str(project),
                                 "res://tests/whaletalk_grans_protocol.tscn"], env=env,
                                stdout=stream, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            result = asyncio.run(exercise(GameBridge(port=primary, grans_port=extra)))
            code = proc.wait(timeout=8)
            assert code == 0, f"Godot退出码{code}，日志{log}"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
    text = log.read_text(encoding="utf-8")
    assert not any(s in text for s in ("SCRIPT ERROR:", "Parse Error", "EXPANSION_FAIL")), str(log)
    report = {"ok": True, **result, "log": str(log), "userdata": str(output / "userdata")}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
