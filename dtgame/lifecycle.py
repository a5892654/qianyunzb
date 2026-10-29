"""识别尚未完成死亡结算的战场；HP 与忙碌/战斗结束是不同状态。"""

from dtgame.grans import PROTOCOL


def player_hp(state: dict):
    battle = state.get("battle") or {}
    life = battle.get("life") or {}
    sources = ([life.get("hp")] if life.get("available") else [])
    sources.extend((battle.get("hp"), (state.get("run") or {}).get("hp")))
    return next((value for value in sources if type(value) in (int, float)), None)


def needs_recovery(state: dict) -> bool:
    if state.get("screen") != "battle":
        return False
    hp = player_hp(state)
    return hp <= 0 if hp is not None else bool((state.get("battle") or {}).get("dead"))


def validate_life(life: dict) -> None:
    if (not isinstance(life, dict) or not life.get("ok") or life.get("protocol") != PROTOCOL
            or not life.get("battle_id") or type(life.get("turn")) is not int
            or type(life.get("hp")) is not int or type(life.get("battle_over")) is not bool
            or type(life.get("busy")) is not bool):
        raise ValueError("游戏接口没有返回有效的死亡结算状态")


def merge_life(state: dict, life: dict) -> dict:
    validate_life(life)
    battle = state.get("battle") or {}
    if type(battle.get("turn")) is int and battle["turn"] != life["turn"]:
        raise ValueError("死亡结算状态与当前回合不一致，请重读状态")
    return {**state, "busy": life["busy"], "run": {**state.get("run", {}), "hp": life["hp"]},
            "battle": {**battle, "hp": life["hp"], "dead": life["hp"] <= 0,
                       "battle_over": life["battle_over"], "life": {**life, "available": True}}}
