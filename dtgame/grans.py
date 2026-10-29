"""格兰斯的桥接契约和决策说明；收益取自游戏预览，不在代打端重算战斗。"""

PROTOCOL = "whaletalk.grans.v1"

PROMPT = """【格兰斯：光暗双剑蓄能／解放】
当前角色格兰斯有光蓄、暗蓄两条资源，各0～6，开战各2，跨回合保留。
左侧只有一个全手牌共用开关，切换不耗能量、不扣蓄能；成功打出才按实际模式付款。
打出光剑通常给暗蓄+1，打出暗剑通常给光蓄+1；同源本回合额度和副本限制以状态为准。

对光剑、暗剑出牌时，明确带 release，让代码先设好模式再出这张牌：
  {"action":"play","slot":手牌序号,"target":敌人序号,"release":false}  蓄能打出，保留本侧蓄能
  {"action":"play","slot":手牌序号,"target":敌人序号,"release":true}   解放打出，消耗本侧全部可用蓄能
全手牌开关会保留，不要认为打完一张自动切回；下一张继续明确带 true 或 false。
只对状态标明的光剑/暗剑使用 release；合璧牌、普通牌不要带这个字段。

光剑解放每层额外3固定格挡，满6另抽2；固定部分不吃敏捷放大。
暗剑解放每层给首段基础伤害+4，满6另+8；游戏预览已算入强化、能力和满蓄翻倍，不要重复加算。
合璧牌始终按牌面上限自动消耗成对光暗蓄能，不受蓄能/解放开关控制，不能靠 release=false 保住资源。
同源回手、重奏与临时副本可能没有再次解放或交叉充能额度，遵守 can_release 和实际卡牌说明。
奥义按实际成对蓄能结算，无需先切解放；仍要满足光焰费用和本回合技艺次数。

先看敌人意图、能量、当前两侧蓄能和逐张解放预览，安排出牌顺序。
没有生存压力时可先蓄能，为满蓄或合璧留资源；需要挡伤、斩杀或防止溢出时主动解放，别死等满6。
光剑保命和暗剑斩杀优先，打完一张后重读资源再决定下一张。
状态若说接口不可用或 can_switch=false，别猜开关、别输出 release，按可执行操作处理或等待。
"""


def is_grans(state: dict | None) -> bool:
    state = state or {}
    run = state.get("run") or {}
    if run.get("character_id"):
        return run["character_id"] == "twilight_grans"
    if run.get("character"):
        return "格兰斯" in str(run["character"])
    return any(str(c.get("id", "")).startswith("grans_")
               for c in (state.get("battle") or {}).get("hand") or [])


def validate_snapshot(snapshot: dict) -> None:
    if (not isinstance(snapshot, dict) or not snapshot.get("ok")
            or snapshot.get("protocol") != PROTOCOL or snapshot.get("character_id") != "twilight_grans"
            or not snapshot.get("battle_id") or type(snapshot.get("turn")) is not int
            or type(snapshot.get("release_mode")) is not bool
            or type(snapshot.get("can_switch")) is not bool
            or not isinstance(snapshot.get("hand_ids"), list) or not isinstance(snapshot.get("cards"), list)):
        raise ValueError("格兰斯接口没有返回有效的战斗快照")


def merge_snapshot(state: dict, snapshot: dict) -> dict:
    validate_snapshot(snapshot)
    battle = state.get("battle") or {}
    if type(battle.get("turn")) is int and battle["turn"] != snapshot["turn"]:
        raise ValueError("格兰斯快照与当前回合不一致，请重读状态")
    hand = battle.get("hand") or []
    if len(hand) != len(snapshot["hand_ids"]):
        raise ValueError("格兰斯快照与当前手牌数量不一致，请重读状态")
    ids = [c.get("id") for c in hand]
    if ids and all(ids) and ids != snapshot["hand_ids"]:
        raise ValueError("格兰斯快照与当前手牌不一致，请重读状态")
    cards = []
    for card in hand:
        extra = next((c for c in snapshot["cards"] if (
            c.get("id") == card["id"] if card.get("id") else
            c.get("slot") == card.get("slot") and c.get("name") == card.get("name"))), None)
        if extra is None:
            raise ValueError("格兰斯卡牌预览与当前手牌不一致，请重读状态")
        cards.append({**card, "grans": extra})
    return {**state, "busy": bool(state.get("busy") or snapshot.get("busy")), "battle": {
        **battle, "hand": cards, "grans": {**snapshot, "available": True}}}
