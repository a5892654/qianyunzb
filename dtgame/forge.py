"""灵魂锻造参谋：按牌型、费用和强化预览挑更值的一项。

分数只是排序，不替代模型看牌面说明；灵魂不够的选项不参与推荐。
"""


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _card_stats(card: dict) -> str:
    bits = []
    if _int(card.get("damage")):
        bits.append(f"伤害{_int(card['damage'])}")
    if _int(card.get("block")):
        bits.append(f"格挡{_int(card['block'])}")
    if _int(card.get("erosion_value")):
        bits.append(f"侵蚀{_int(card['erosion_value'])}")
    if _int(card.get("memory_value")):
        bits.append(f"记忆{_int(card['memory_value'])}")
    return " ".join(bits) or "无数值"


def _route_score(delta: dict) -> int:
    score = 72
    for key in ("damage", "block", "erosion_value", "memory_value", "posture_damage"):
        if _int(delta.get(key)) > 0:
            score += 8
    if _int(delta.get("draw")) > 0 or _int(delta.get("energy_gain")) > 0:
        score += 18
    if _int(delta.get("cost")) < 0:
        score += 26
    if _int(delta.get("cost")) > 0:
        score -= 14
    if _int(delta.get("self_damage")) > 0 or _int(delta.get("hp_cost")) > 0:
        score -= 16
    if delta.get("remove_tags"):
        score += 10
    if delta.get("add_tags"):
        score += 5
    return score


def _score(option: dict, card: dict) -> tuple[int, str]:
    kind = str(option.get("type") or "")
    cost = _int(card.get("cost"))
    damage = _int(card.get("damage"))
    block = _int(card.get("block"))
    memory = _int(card.get("memory_value"))
    forge_count = _int(card.get("forge_count"))
    score = 40
    reason = option.get("preview") or ""

    if kind == "sharpen":
        score = 105 if cost >= 2 else 95
        reason = "核心牌降 1 费，长期每回合都能多打一张，通常比单次 +3 数值更划算"
    elif kind == "enchant_replay":
        score = 100 if (damage or block) else 82
        reason = "效果触发两次，相当于整张牌翻倍，关键牌收益最高"
    elif kind == "enchant_eternal":
        score = 90
        reason = "消耗/能力牌不再消失，长局收益高"
    elif kind in ("tiff_forge", "leia_forge"):
        score = 92
        reason = "角色专属强化，通常比通用加数值更贴合这套牌"
    elif kind == "forge_route":
        score = _route_score(option.get("delta") or {})
        reason = option.get("preview") or "专属强化路线"
    elif kind == "strengthen":
        score = 60 + min(10, damage // 4)
        reason = "伤害固定 +3，直接抬斩杀线；但只是单次数值"
    elif kind == "strengthen_block":
        score = 58 + min(10, block // 4)
        reason = "格挡固定 +3，提高每回合的有效血量"
    elif kind == "enchant_lightweight":
        score = 84 if cost >= 1 else 20
        reason = "降 1 费但数值 ×0.7，适合高费核心牌"
    elif kind == "enchant_corrupted":
        score = 70
        reason = "伤害 ×1.5，但每次打出扣 3 HP，血线紧时要谨慎"
    elif kind.startswith("enchant_"):
        score = 78
        reason = option.get("preview") or "专属附魔"
    elif kind == "memory_awaken":
        score = 68 if memory > 0 else 20
        reason = "记忆值翻倍，只有记忆流核心才值得"
    elif kind == "infuse":
        score = 45
        reason = "侵蚀 +3 偏持续伤害，爆发不如直接加伤害"
    elif kind == "infuse_element":
        score = 35
        reason = "元素质变偏构筑，牌组不围绕该元素时收益很低"
    elif kind == "curse_engrave":
        score = 25
        reason = "带代价的强化，只有这张牌是绝对核心时才考虑"
    score -= forge_count * 12
    return score, reason


def _best(state: dict):
    card = state.get("selected") or {}
    options = [o for o in state.get("options") or [] if o.get("affordable", True)]
    if not options:
        return None
    # 分数相同优先便宜的那项，再按界面顺序。
    return max(options, key=lambda o: (_score(o, card)[0], -_int(o.get("cost")),
                                       -_int(o.get("slot"))))


def recommend(state: dict):
    if not state.get("options"):
        return None, "这张卡没有可用强化项"
    option = _best(state)
    if not option:
        return None, "当前灵魂不够任何强化，先离开攒灵魂"
    return _int(option.get("slot")), _score(option, state.get("selected") or {})[1]


def notes(state: dict) -> list[str]:
    selected = state.get("selected") or {}
    if not selected:
        cards = state.get("cards") or []
        if not cards:
            return ["没有可锻造的卡，直接离开。"]
        return ["先选一张卡：优先核心输出/防御牌；灵魂有限时别把每张卡都摸一遍。"]

    out = []
    free = bool(state.get("free"))
    stats = _card_stats(selected)
    forge_count = _int(selected.get("forge_count"))
    out.append(f"当前卡「{selected.get('name')}」：费{_int(selected.get('cost'))}，{stats}，"
               f"已锻{forge_count}次。")
    desc = str(selected.get("desc") or "").strip().replace("\n", " ")
    if desc:
        out.append("牌面：" + desc[:100])

    slot, reason = recommend(state)
    if slot is None:
        out.append(reason + "。")
    else:
        option = next((o for o in state.get("options") or [] if _int(o.get("slot")) == slot), {})
        price = "免费" if free else f"{_int(option.get('cost'))} 魂"
        preview = str(option.get("preview") or "").strip()
        out.append(f"优先推荐 [{slot}] {option.get('name')}（{preview + '，' if preview else ''}{price}）：{reason}。")
    if forge_count and not free:
        mult = 1.6 if forge_count == 1 else 2.5
        out.append(f"这张已锻 {forge_count} 次，本次费用 ×{mult:g}；除非它是绝对核心，否则换一张更划算。")
    out.append("界面默认停在第一项，不代表最优；确认前先比较全部强化项。")
    return out
