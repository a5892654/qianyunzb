"""战斗数值参谋：把当前局面算成几句可执行的判断。

只做基础牌面的算术，不逐张复刻条件加成、抽牌连锁和特殊效果。
结论交给模型参考，不替它做决定。
"""

_TYPE_ATTACK = "attack"


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _card_playable(card: dict) -> bool:
    return card.get("playable", True) and not card.get("blocked")


def _card_block(card: dict, dexterity: int, block_mult: float, fragile: bool) -> int:
    raw = max(0, _int(card.get("block")))
    if raw <= 0:
        return 0
    # 与 GameManager.gain_block 同序：深度倍率先作用于牌面值，再加敏捷，最后算脆弱。
    amount = int(raw * block_mult) + dexterity
    if fragile:
        amount = int(amount * 0.75)
    return max(0, amount)


def _card_damage(card: dict, strength: int) -> int:
    base = max(0, _int(card.get("damage")))
    if base <= 0:
        return 0
    hits = max(1, _int(card.get("hits"), 1))
    return (base + strength) * hits


def _best_value(cards: list[dict], energy: int, value) -> int:
    """能量上限内的 0/1 背包：每张牌只能打一次，取总收益最大的一组。"""
    if energy <= 0:
        return 0
    dp = [-1] * (energy + 1)
    dp[0] = 0
    for card in cards:
        if not _card_playable(card):
            continue
        cost = _int(card.get("cost"))
        if cost < 0 or cost > energy:
            continue
        gain = value(card)
        if gain <= 0:
            continue
        for spent in range(energy, cost - 1, -1):
            if dp[spent - cost] >= 0:
                dp[spent] = max(dp[spent], dp[spent - cost] + gain)
    return max(dp)


def _incoming(battle: dict) -> tuple[int, int, list[str]]:
    total = 0
    biggest = 0
    parts = []
    for enemy in battle.get("enemies") or []:
        if enemy.get("alive") is False:
            continue
        intent = enemy.get("intent") or {}
        if str(intent.get("type") or "") != _TYPE_ATTACK:
            continue
        value = max(0, _int(intent.get("value")))
        hits = max(1, _int(intent.get("hits"), 1))
        damage = value * hits
        if damage <= 0:
            continue
        total += damage
        biggest = max(biggest, damage)
        name = enemy.get("name") or "敌人"
        parts.append(f"{name} {damage}" + (f"（{value}×{hits}）" if hits > 1 else ""))
    return total, biggest, parts


def _effective_hp(enemy: dict) -> int:
    return max(0, _int(enemy.get("hp"))) + max(0, _int(enemy.get("block")))


def _vulnerable_mult(enemy: dict) -> float:
    debuffs = enemy.get("debuffs") or {}
    if _int(debuffs.get("vulnerable")) <= 0:
        return 1.0
    return 3.0 if _int(debuffs.get("vulnerable_boost")) > 0 else 1.5


def notes(battle: dict) -> list[str]:
    if not battle or battle.get("battle_over"):
        return []
    enemies = [e for e in battle.get("enemies") or [] if e.get("alive") is not False]
    hand = battle.get("hand") or []
    if not enemies and not hand:
        return []

    hp = _int(battle.get("hp"))
    max_hp = max(1, _int(battle.get("max_hp"), 1))
    block = max(0, _int(battle.get("block")))
    energy = max(0, _int(battle.get("energy")))
    strength = _int(battle.get("strength"))
    dexterity = _int(battle.get("dexterity"))
    block_mult = float(battle.get("block_mult") or 1.0)
    fragile = bool(battle.get("fragile"))

    incoming, biggest, parts = _incoming(battle)
    has_card_numbers = any("damage" in c or "block" in c for c in hand)
    best_damage = 0
    best_block = 0
    if has_card_numbers:
        best_damage = _best_value(hand, energy, lambda c: _card_damage(c, strength))
        best_block = _best_value(
            hand, energy, lambda c: _card_block(c, dexterity, block_mult, fragile))
        best_block = max(0, min(best_block, max_hp - block))

    out = []
    if incoming:
        hurt = max(0, incoming - block)
        names = "，".join(parts)
        out.append(f"敌方本回合总伤害 {incoming}（{names}）；"
                   f"你 {hp}/{max_hp} 血、{block} 格挡，"
                   + ("现有格挡能全挡。" if hurt == 0 else f"不防会掉 {hurt}。"))
    else:
        out.append("敌人本回合没有攻击意图。")

    if has_card_numbers:
        bits = [f"能量 {energy} 内手牌最多约 {best_block} 点格挡、{best_damage} 点伤害"]
        if strength or dexterity:
            bits.append(f"已计入力量 {strength}、敏捷 {dexterity}")
        bits.append("未计抽牌/回能连锁")
        out.append("，".join(bits) + "。")

    killable: list[str] = []
    if enemies:
        hp_bits = []
        for e in enemies:
            mult = _vulnerable_mult(e)
            suffix = f"（易伤×{mult:g}）" if mult > 1.0 else ""
            hp_bits.append(f"{e.get('name') or '敌人'} {_effective_hp(e)}{suffix}")
        out.append("敌方有效血量（含格挡）：" + "、".join(hp_bits) + "。")
        if has_card_numbers and best_damage > 0:
            total_hp = sum(_effective_hp(e) for e in enemies)
            killable = [e.get("name") or "敌人" for e in enemies
                        if e.get("targetable", True)
                        and _effective_hp(e) <= int(best_damage * _vulnerable_mult(e))]
            if total_hp <= best_damage:
                out.append("全打输出约够清场。")
            elif killable:
                out.append("全力单体约可击杀：" + "、".join(killable) + "。")

    estus = battle.get("estus") or {}
    estus_ready = bool(estus.get("can_use"))
    estus_uses = _int(estus.get("uses"))
    if incoming <= 0:
        out.append("优先输出或铺能力，别把能量花在防御上。")
    elif has_card_numbers and best_damage > 0 and enemies and sum(
            _effective_hp(e) for e in enemies) <= best_damage:
        out.append("有斩杀窗口：先输出收场，不为多余格挡浪费能量。")
    elif block + best_block >= incoming:
        out.append("能全挡；如果斩杀窗口更划算，也可以先输出再补挡。")
    elif has_card_numbers and hp + block + best_block > incoming:
        remaining = incoming - block - best_block
        lead = "格挡全交也仍会掉" if best_block else "手牌没有可用格挡，会掉"
        out.append(f"{lead} {remaining} 血；血线安全就直接输出，否则先补格挡。")
    elif has_card_numbers:
        tip = "即使全防也会倒下"
        if estus_ready:
            tip += f"，元素瓶现在能喝（剩 {estus_uses} 次）"
        out.append(tip + "；先回血/用道具，或优先击杀最大威胁。")
    else:
        out.append("牌面数值缺失，先按敌人意图留够格挡。")
    if killable and sum(_effective_hp(e) for e in enemies) > best_damage:
        out.append("打不死全部时优先集火：" + "、".join(killable) + "，减少后续回合压力。")
    if biggest and biggest >= hp + block:
        out.append(f"最大单段伤害 {biggest}，已经够打倒现在的你，别只顾着挡小伤害。")
    return out
