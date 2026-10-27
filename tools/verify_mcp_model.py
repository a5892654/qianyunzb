import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame.bridge import GameBridge, BridgeError
from dtgame.player import Player
from dtgame import view


async def main():
    config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    ai = dict(config["ai"])
    ai.update({k: v for k, v in config["dtgame"].items() if k.startswith("openai_") and v})
    player = Player(ai, GameBridge(), narrate=False)
    expected = {"event", "dialog", "fountain"}
    seen = set()
    records = []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and seen != expected:
        try:
            state = await asyncio.to_thread(player.bridge.state)
        except BridgeError:
            await asyncio.sleep(0.2)
            continue
        screen = state.get("screen")
        if screen not in expected:
            await asyncio.sleep(0.2)
            continue
        body = state[screen]
        if not body.get("options") and not body.get("buttons") and not body.get("can_back"):
            raise AssertionError(f"{screen} 没有可用操作：{body}")
        print("MODEL_INPUT", view.render(state), flush=True)
        result = await player.step()
        record = {"screen": screen, "window_id": body["window_id"], **result}
        records.append(record)
        print("MODEL_STEP", json.dumps(record, ensure_ascii=False), flush=True)
        if not result.get("ok") and not result.get("stale"):
            raise AssertionError(f"模型操作失败：{record}")
        after = await asyncio.to_thread(player.bridge.screen)
        close_deadline = time.monotonic() + 3
        while after.get("window_id") == body["window_id"] and after.get("busy") and time.monotonic() < close_deadline:
            await asyncio.sleep(0.1)
            after = await asyncio.to_thread(player.bridge.screen)
        record["closed"] = after.get("window_id") != body["window_id"]
        record["screen_after"] = after.get("screen")
        if record["closed"]:
            seen.add(screen)
        await asyncio.sleep(0.3)
    report = {"passed": seen == expected, "seen": sorted(seen), "steps": records}
    target = Path("C:/gamedata/outputs/mcp_model_verification.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("MODEL_VERIFICATION", json.dumps(report, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise AssertionError("事件、GUI 控件弹窗、真实泉水未全部完成")


if __name__ == "__main__":
    asyncio.run(main())
