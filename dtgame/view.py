"""把游戏状态压成给模型看的紧凑文本。

原始 state 是嵌套字典，直接塞给模型会浪费大量 token 且重点被淹没。
这里按界面类型输出人读格式，只保留做决策需要的字段。
"""

from dtgame.expedition import depth_settings

TYPE_CN = {
    "battle": "战斗", "elite": "精英", "boss": "BOSS", "campfire": "篝火",
    "shop": "商店", "mystery": "未知", "chest": "宝箱", "start": "起点",
    "event": "事件", "shrine": "神殿", "fountain": "泉水", "arena": "决斗场",
    "rift": "裂隙", "smithy": "铁匠", "dungeon": "地牢",
}

SCREEN_CN = {
    "battle": "战斗", "map": "地图", "reward": "卡牌奖励", "campfire": "篝火",
    "shop": "商店", "event": "事件", "character_select": "选角", "menu": "主菜单",
    "hall": "出征大厅", "loot": "战利品结算", "other": "其他",
    "fountain": "回响之泉", "arena": "决斗场", "rift": "深渊裂隙", "shrine": "诅咒神殿",
    "chest": "宝箱", "soul_forge": "灵魂锻造", "dungeon": "地牢", "dialog": "弹窗", "death": "死亡结算",
    "death_analysis": "死亡分析", "run_end": "本局结算",
}


def _fmt_status(status: dict) -> list[str]:
    if not status:
        return []
    bits = [f"{status.get('character') or '未知角色'}",
            f"HP {status.get('hp')}/{status.get('max_hp')}"]
    if status.get("block"):
        bits.append(f"格挡 {status['block']}")
    bits.append(f"灵魂 {status.get('souls', 0)}")
    bits.append(f"第{status.get('act', 1)}幕 第{status.get('floor', 1)}层")
    depth = status.get("expedition_depth", status.get("depth"))
    if type(depth) is int:
        bits.append(f"出征深度 {depth}")
    line = " | ".join(bits)
    extra = []
    if status.get("relics"):
        extra.append("遗物: " + ", ".join(status["relics"][:12]))
    pots = [p for p in (status.get("potions") or []) if p]
    if pots:
        extra.append("道具: " + ", ".join(pots))
    buff = status.get("buff") or {}
    debuff = status.get("debuff") or {}
    if buff:
        extra.append("增益: " + ", ".join(f"{k}x{v}" for k, v in buff.items()))
    if debuff:
        extra.append("减益: " + ", ".join(f"{k}x{v}" for k, v in debuff.items()))
    return [line] + extra


def _fmt_estus(b: dict) -> list[str]:
    """元素瓶是战斗中唯一「不消耗回合」的回血手段。
    能不能喝、为什么不能喝必须写明白：模型看不到原因就会对着满血一直按。"""
    est = b.get("estus") or {}
    if not est:
        return []
    hp, max_hp = b.get("hp"), b.get("max_hp")

    def why(uses) -> str:
        # 先说「没次数」：这是永久性的，血满不满都喝不了；
        # 反过来「血已满」是等掉血就能喝，模型得知道区别才不会一直撞。
        if not uses:
            return "没次数了"
        if hp is not None and hp == max_hp:
            return "血已满"
        return "现在喝不了"

    out = []
    if est.get("max"):
        uses = est.get("uses", 0)
        out.append(f"元素瓶 {uses}/{est['max']}（可喝，不消耗回合，action=estus）" if est.get("can_use")
                   else f"元素瓶 {uses}/{est['max']}（{why(uses)}）")
    if est.get("ash_max"):
        ash = est.get("ash_uses", 0)
        out.append(f'灰元素瓶 {ash}/{est["ash_max"]}（可喝，补光焰不补血，action=estus ash=true）'
                   if est.get("can_use_ash") else f'灰元素瓶 {ash}/{est["ash_max"]}（{why(ash)}）')
    return out


def _fmt_potions(b: dict) -> list[str]:
    """道具要给名字和效果，光给 id 模型根本不知道它是什么；
    blocked 里是游戏写的中文原因，原样透出，它才知道这瓶现在别碰。"""
    out = []
    for p in b.get("potions") or []:
        if not p:
            continue
        if isinstance(p, str):  # 旧版桥只回 id
            out.append(f"  {p}")
            continue
        desc = (p.get("desc") or "").strip().replace("\n", " ")
        blocked = (p.get("blocked") or "").strip()
        line = f"  [{p.get('slot')}] {p.get('name') or p.get('id')}"
        if desc:
            line += f": {desc}"
        if blocked:
            line += f"（现在用不了：{blocked}）"
        out.append(line)
    return ["道具（action=potion slot=序号）:", *out] if out else out


def _fmt_battle(b: dict, run: dict) -> list[str]:
    out = [f"回合 {b.get('turn')}，能量 {b.get('energy')}/{b.get('max_energy')}，"
           f"HP {b.get('hp')}/{b.get('max_hp')}" + (f"，格挡 {b['block']}" if b.get("block") else "")]
    rad, cost = b.get("radiance"), b.get("ultimate_cost")
    if rad is not None:
        if b.get("radiance_art_used"):
            out.append(f"光焰 {rad}：这回合的「光焰技艺」已经用掉了")
        elif cost is not None and rad >= cost:
            out.append(f"光焰 {rad}：奥义已就绪（需 {cost}），action=radiance 放出去，别攒着")
        else:
            out.append(f"光焰 {rad}（奥义需 {cost}；技艺每回合只有 1 次，没到阈值别按）")
    out.extend(_fmt_estus(b))
    g = b.get("grans") or {}
    if g:
        if not g.get("available"):
            out.append("【格兰斯】蓄能/解放接口不可用，不要猜测模式或输出 release：" + str(g.get("error", "")))
        else:
            mode = "解放" if g.get("release_mode") else "蓄能"
            out.append(f"【格兰斯】光蓄 {g.get('light')}/{g.get('capacity', 6)}，暗蓄 {g.get('dark')}/{g.get('capacity', 6)}，当前全手牌模式：{mode}")
            out.append("光暗蓄能跨回合保留；切换不扣资源，剑牌成功打出时才付款。")
            if g.get("can_switch"):
                out.append("光剑/暗剑出牌明确带 release=false 蓄能、release=true 解放；每张牌单独决定。")
            else:
                out.append("can_switch=false：当前不能切换，先等待结算或处理弹窗。")
    out.append("敌人:")
    for i, e in enumerate(b.get("enemies") or []):
        it = e.get("intent") or {}
        intent = it.get("name") or it.get("type") or "?"
        val = it.get("value") or 0
        hits = it.get("hits") or 1
        if it.get("type") == "attack":
            intent_txt = f"{intent} {val}" + (f"x{hits}" if hits > 1 else "") + f"（预计 {val * hits} 伤害）"
        else:
            intent_txt = f"{intent}" + (f" {val}" if val else "")
        tags = []
        if e.get("block"):
            tags.append(f"格挡{e['block']}")
        for k, v in (e.get("buffs") or {}).items():
            tags.append(f"{k}+{v}")
        for k, v in (e.get("debuffs") or {}).items():
            tags.append(f"{k}-{v}")
        tag_txt = f" [{', '.join(tags)}]" if tags else ""
        hide = "" if e.get("targetable", True) else (
            " ✕不可选中(已阵亡)" if not e.get("alive", True) else " ✕不可选中(隐匿)")
        out.append(f"  [{i}] {e.get('name')} {e.get('hp')}/{e.get('max_hp')}{tag_txt}{hide} 意图: {intent_txt}")
    out.append("手牌:")
    if not b.get("hand"):
        out.append("  （空）")
    for c in b.get("hand") or []:
        blocked = c.get("blocked") or ""
        flag = f" ✕{blocked}" if blocked else ""
        cost = c.get("cost")
        cost_txt = "?" if cost is not None and cost < 0 else str(cost)
        tgt = {"single": "需选敌", "all": "群体", "self": "自身"}.get(c.get("target") or "", "自身")
        out.append(f"  [{c.get('slot')}] {c.get('name')} 费{cost_txt} [{c.get('type')}] {tgt}{flag}")
        gc = c.get("grans") or {}
        desc = (gc.get("normal_desc") or c.get("desc") or "").strip().replace("\n", " ")
        if desc:
            out.append(f"      {'蓄能效果：' if gc.get('sword') in ('light', 'dark') else ''}{desc}")
        side = gc.get("sword")
        if side in ("light", "dark"):
            sword = "光剑" if side == "light" else "暗剑"
            cross = "暗蓄" if side == "light" else "光蓄"
            out.append(f"      {sword}；" + (f"打出后{cross}+1（另按能力结算）" if gc.get("cross_charge_available") else "本张无默认交叉充能额度"))
            if gc.get("can_release"):
                preview = gc.get("preview") or {}
                value = (f"额外{preview.get('fixed_block', 0)}固定格挡、另抽{preview.get('draw', 0)}" if side == "light"
                         else f"解放后的首段基础伤害{preview.get('damage')}（已含解放加成，不要重复加算）")
                out.append(f"      解放可用：消耗本侧全部{preview.get('n')}层；{value}。交叉充能和能力回充以出牌后的新状态为准。")
                release_desc = str(gc.get("release_desc") or "").strip().replace("\n", " ")
                if release_desc:
                    out.append("      解放效果：" + release_desc)
            else:
                out.append("      解放不可用：本侧蓄能为空、同源额度已用或临时副本；可按蓄能模式普通打出。")
        elif side == "dual":
            out.append(f"      合璧牌：按牌面自动消耗{gc.get('pair_spend', 0)}对光暗蓄能，不受开关影响，不要填 release。")
    out.append(f"牌库 {b.get('draw_pile_count')} / 弃牌 {b.get('discard_pile_count')} / 消耗 {b.get('exhaust_pile_count')}")
    out.extend(_fmt_potions(b))
    if b.get("battle_over"):
        out.append("战斗已结束，正在结算。")
    return out


def _fmt_map(m: dict) -> list[str]:
    out = [f"下一步只能进 col={m.get('current_col')} 这一列（你现在在 row={m.get('current_row')} 这条线上）"]
    if not m.get("choices"):
        out.append("这一列没有能进的节点。")
        return out
    out.append("可前往:")
    for c in m["choices"]:
        tag = "（已扫清·快速通行）" if c.get("passthrough") else ""
        out.append(f"  (col={c['col']}, row={c['row']}) {TYPE_CN.get(c['type'], c['type'])}{tag}")
    return out


def _fmt_reward(r: dict) -> list[str]:
    out = ["卡牌奖励，三选一或跳过:"]
    for o in r.get("offers") or []:
        out.append(f"  [{o['slot']}] {o.get('name')} 费{o.get('cost')} [{o.get('type')}]"
                   f" 稀有度:{o.get('rarity')}")
        desc = (o.get("desc") or "").strip().replace("\n", " ")
        if desc:
            out.append(f"      {desc}")
    out.append("  skip 可放弃")
    return out


def _fmt_campfire(c: dict) -> list[str]:
    out = ["篝火，可选:"]
    for o in c.get("options") or []:
        mark = "" if o.get("enabled", True) else " (不可用)"
        # action=false 那条是「继续前行」：它不是一件事，而是离开篝火，
        # 不标出来模型会以为还有个隐藏操作，或者压根不敢走。
        if o.get("action") is False:
            mark += "（离开篝火）"
        out.append(f"  [{o['slot']}] {o.get('text')}{mark}")
    out.append('选一项: {"action":"campfire","slot":序号}')
    return out


def _fmt_shop(s: dict) -> list[str]:
    out = [f"商店，持有 {s.get('souls')} 灵魂:"]
    for t in s.get("tiles") or []:
        sold = " (已售出)" if t.get("sold") else ""
        out.append(f"  [{t['slot']}] {t.get('kind')} {t.get('id')} 价 {t.get('price')}{sold}")
    out.append("action=leave 离开商店")
    return out


OVERLAY_SCREENS = ("fountain", "arena", "rift", "shrine", "chest", "soul_forge", "dungeon")


def _fmt_soul_forge(f: dict) -> list[str]:
    """灵魂锻造是「真选择」，不像篝火那样能自动推进，所以动作形状要写全：
    选卡 → 选强化 → 确认，漏一步就停在原地空转。"""
    out = [f"灵魂锻造，持有 {f.get('souls', 0)} 灵魂"
           + ("，本次免费" if f.get("free") else "") + ":"]
    if f.get("window_id"):
        out.append(f"window_id={f['window_id']} revision={f.get('revision')}")
    cards = f.get("cards") or []
    if cards:
        out.append("可锻造的卡:")
        for c in cards:
            out.append(f"  [{c.get('slot')}] {c.get('name')} 费{c.get('cost')} [{c.get('type')}]"
                       f" 已锻{c.get('forge_count', 0)}次")
    else:
        out.append("没有还能锻造的卡。")
    sel = f.get("selected") or {}
    out.append(f"  已选卡: [{sel.get('slot')}] {sel.get('name')}" if sel else "  还没选卡")
    for o in f.get("options") or []:
        lock = "" if o.get("affordable", True) else "（灵魂不够）"
        cur = " ←已选" if o.get("slot") == f.get("selected_option") else ""
        out.append(f"  强化 [{o.get('slot')}] {o.get('name')} 价 {o.get('cost')}{lock}{cur}")
    out.append('  {"action":"forge","op":"card","slot":卡牌序号}')
    out.append('  {"action":"forge","op":"option","slot":强化项序号}')
    if f.get("can_forge"):
        out.append('  {"action":"forge","op":"forge"}  确认锻造')
    else:
        out.append("  还不能确认锻造（卡或强化没选，或者灵魂不够）")
    out.append('  {"action":"forge","op":"back"}  不锻了，离开')
    return out


def _fmt_overlay(screen: str, body: dict) -> list[str]:
    out = [f"{SCREEN_CN.get(screen, screen)}「{body.get('panel', '')}」挡住了地图，先处理窗口:"]
    if body.get("window_id"):
        out.append(f"window_id={body['window_id']} revision={body['revision']}")
    shown = set()
    for key in ("title", "desc"):
        text = body.get(key) or ""
        if text:
            out.append(text)
            shown.add(text)
    action = "event" if screen == "event" else "overlay"
    for o in body.get("options") or []:
        lock = f" (不可用:{o.get('lock_reason') or '锁定'})" if o.get("locked") else ""
        risk = f" [风险:{o['risk']}]" if o.get("risk") else ""
        out.append(f"  选项 slot={o['slot']} {o.get('name')}{risk}{lock}")
        desc = (o.get("desc") or "").strip()
        if desc:
            out.append(f"      {desc}")
        shown.update((o.get("name") or "", desc))
    for text in body.get("texts") or []:
        if text not in shown:
            out.append(text)
            shown.add(text)
    for b in body.get("buttons") or []:
        out.append(f"  控件 slot={b['slot']} 「{b.get('text') or b.get('name')}」: action=overlay op=button")
    if any(not option.get("locked") for option in body.get("options") or []):
        out.append(f"选择未锁定选项: action={action} slot=选项序号")
    if body.get("can_back"):
        out.append("允许关闭/离开: action=overlay op=back")
    else:
        out.append("不能直接关闭；请选择上面可用的操作，不要猜 op=back。")
    return out


def _fmt_dialog(body: dict) -> list[str]:
    return _fmt_overlay("dialog", body)


def _fmt_event(e: dict) -> list[str]:
    return _fmt_overlay("event", e)


def character_selectable(character: dict) -> bool:
    return (type(character.get("slot")) is int and character["slot"] >= 0
            and not character.get("locked") and character.get("unlocked") is not False
            and character.get("enabled") is not False)


def character_unlock_known(character: dict) -> bool:
    return character.get("unlocked") is True or character.get("locked") is False


def _fmt_character(ch: dict) -> list[str]:
    out = ["选择角色:"]
    for c in ch.get("characters") or []:
        mark = " ←当前" if c.get("selected") else ""
        if not character_selectable(c):
            mark += f" ✕不可选（{c.get('lock_reason') or '未解锁或不可用'}）"
        elif not character_unlock_known(c):
            mark += "（解锁状态未知，需游戏校验；优先选已解锁角色）"
        out.append(f"  [{c.get('slot', '?')}] {c.get('name')} HP {c.get('hp')}{mark}")
    return out


def _fmt_hall(state: dict) -> list[str]:
    current, limit = depth_settings(state)
    out = ["出征前可以选择本局深度；深度越高，叠加的负面效果越多。"]
    if current is not None:
        out.append(f"当前选择深度: {current}")
    if limit is None:
        out.append("游戏桥未提供已解锁深度上限，沿用游戏当前深度，不要猜测可选范围。")
        out.append("按当前设置出征: action=expedition（不填 depth）")
    else:
        out.append(f"可选出征深度: 0～{limit}，只能选择这个范围内的整数。")
        if (state.get("hall") or {}).get("expedition_mode", (state.get("hall") or {}).get("mode")) == "shallow":
            out.append("当前是新手教学路线，本局固定深度 0。")
        for item in (state.get("hall") or {}).get("depth_debuffs") or []:
            if type(item.get("depth")) is int and item["depth"] <= limit:
                out.append(f"  深度 {item['depth']}: {item.get('name', '')} {item.get('desc', '')}")
        out.append("选择深度并出征: action=expedition depth=整数")
    return out


def _fmt_run_end(b: dict) -> list[str]:
    """结算页由代码直接按「返回大厅」，这里不给动作：正文只供日志和状态页读。"""
    out = []
    for key in ("title", "desc"):
        text = (b.get(key) or "").strip().replace("\n", " ")
        if text and text not in out:
            out.append(text)
    for text in b.get("texts") or []:
        text = (text or "").strip()
        if text and text not in out:
            out.append(text)
    out.append("这一局已结束，会自动返回大厅开下一局")
    return out


def render(state: dict) -> str:
    """state → 给模型看的文本。"""
    screen = state.get("screen", "menu")
    lines = [f"【当前界面】{SCREEN_CN.get(screen, screen)}"]
    status = _fmt_status(state.get("run") or {})
    if status:
        lines.append("【状态】" + status[0])
        lines.extend(status[1:])
    body = state.get(screen)
    if screen == "battle" and body:
        lines.append("【战场】")
        lines.extend(_fmt_battle(body, state.get("run") or {}))
    elif screen == "map" and body:
        lines.append("【地图】")
        lines.extend(_fmt_map(body))
    elif screen == "reward" and body:
        lines.extend(_fmt_reward(body))
    elif screen == "campfire" and body:
        lines.extend(_fmt_campfire(body))
    elif screen == "shop" and body:
        lines.extend(_fmt_shop(body))
    elif screen == "soul_forge" and body:
        # 现在是自己的面板，不再走 overlay 那套「挡住了地图」的说法
        lines.extend(_fmt_soul_forge(body))
    elif screen == "event" and body:
        lines.extend(_fmt_event(body))
    elif screen in (*OVERLAY_SCREENS, "loot", "death", "death_analysis") and body:
        lines.extend(_fmt_overlay(screen, body))
    elif screen == "dialog" and body:
        lines.extend(_fmt_dialog(body))
    elif screen == "run_end" and body:
        lines.extend(_fmt_run_end(body))
    elif screen == "character_select" and body:
        lines.extend(_fmt_character(body))
    elif screen == "hall":
        lines.extend(_fmt_hall(state))
    elif screen == "menu":
        lines.append("游戏在菜单里。需要先从出征大厅开始一局。")
    if state.get("busy"):
        lines.append("（游戏正在播放动画，请稍等）")
    return "\n".join(lines)
