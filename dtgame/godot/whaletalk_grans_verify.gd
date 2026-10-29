extends "res://tests/test_grans_combat.gd"

func cases() -> Array:
	return [
		{"label": "桥接快照只读且预览来自真实机制", "run": _bridge_read},
		{"label": "蓄能光剑→解放暗剑→解放光剑的真实出牌链", "run": _bridge_relay},
		{"label": "空能量拒绝出牌不消费蓄能", "run": _bridge_rejected},
		{"label": "过期战斗/回合/手牌和动画输入保护", "run": _bridge_guards},
		{"label": "合璧不受模式开关影响", "run": _bridge_dual},
	]

func _api() -> Node:
	return get_node("/root/WhaleTalkGransBridge")

func _request(active: bool) -> Dictionary:
	var current: Dictionary = _api().snapshot()
	return {"release": active, "battle_id": current.battle_id, "turn": current.turn, "hand_ids": current.hand_ids,
		"light": current.light, "dark": current.dark, "release_mode": current.release_mode}

func _ready_to_switch() -> void:
	for i in range(240):
		if _api().snapshot().get("can_switch", false): return
		await get_tree().process_frame
	_ok(false, "切换就绪超时")

func _bridge_read() -> void:
	give_card("grans_001")
	give_card("grans_002")
	var before := GameManager.energy
	var snap: Dictionary = _api().snapshot()
	_ok(snap.get("ok", false) and snap.protocol == "whaletalk.grans.v1", "接口协议与角色可读")
	_ok(snap.light == 2 and snap.dark == 2 and not snap.release_mode, "实际双蓄2/2，默认蓄能")
	var light: Dictionary = {}
	var dark: Dictionary = {}
	for card in snap.cards:
		if card.sword == "light": light = card
		if card.sword == "dark": dark = card
	_ok(light.preview.fixed_block == 6 and dark.preview.damage == 15, "光剑6固定挡、暗剑15基础伤害预览")
	_ok(Grans.charge(_bm, "light") == 2 and GameManager.energy == before, "只读不预付资源")
	await _ready_to_switch()
	_ok(_api().set_mode(_request(true)).get("release_mode", false), "切到解放")
	_ok(Grans.charge(_bm, "light") == 2 and GameManager.energy == before, "切换不扣蓄能和能量")
	_ok(not _api().set_mode(_request(false)).get("release_mode", true), "切回蓄能")
	_ok(not hand_node("grans_001").get_card_data().get("_grans_release", false), "真实手牌回到蓄能")

func _mode_play(n: int, active: bool) -> void:
	give_card("grans_%03d" % n)
	await _ready_to_switch()
	var result: Dictionary = _api().set_mode(_request(active))
	_ok(result.get("ok", false), "切换成功 " + str(active))
	var node := hand_node("grans_%03d" % n)
	GameManager.resonance_chain = 0
	GameManager.last_element_played = ""
	await _bm._on_card_played(node.get_card_data(), node, 0)
	await settle(6)

func _bridge_relay() -> void:
	GameManager.energy = 3
	await _mode_play(1, false)
	_ok(Grans.charge(_bm, "light") == 2 and Grans.charge(_bm, "dark") == 3, "蓄能光剑不扣2光，充暗至3")
	var hp := int(enemy(0).unit_hp)
	await _mode_play(2, true)
	_ok(Grans.charge(_bm, "light") == 3 and Grans.charge(_bm, "dark") == 0, "解放暗剑消费3暗、充光至3")
	_ok(hp - int(enemy(0).unit_hp) == 19, "真实暗剑19伤害")
	await _mode_play(1, true)
	_ok(GameManager.energy == 0 and GameManager.player_block == 9, "三张3费、光剑解放9固定挡")
	_ok(Grans.charge(_bm, "light") == 0 and Grans.charge(_bm, "dark") == 1, "最终资源0光1暗")

func _bridge_rejected() -> void:
	GameManager.energy = 0
	await _mode_play(2, true)
	_ok(Grans.charge(_bm, "light") == 2 and Grans.charge(_bm, "dark") == 2, "能量不足时真实出牌不消费蓄能")
	_ok(hand_node("grans_002") != null, "拒绝后手牌仍在")

func _bridge_guards() -> void:
	await _ready_to_switch()
	for field in ["battle_id", "turn", "hand_ids", "light", "dark", "release_mode"]:
		var args := _request(true)
		if field == "battle_id": args[field] = "old"
		elif field == "hand_ids": args[field] = ["old_card"]
		elif field == "release_mode": args[field] = not args[field]
		else: args[field] = -99
		var result: Dictionary = _api().set_mode(args)
		_ok(not result.ok and result.stale, "拒绝过期 " + field)
	var bad := _request(true)
	bad.release = "false"
	_ok(not _api().set_mode(bad).ok, "拒绝非布尔值")
	_bm.set_meta("grans_cinematic_active", true)
	var blocked: Dictionary = _api().set_mode(_request(true))
	_ok(not blocked.ok and blocked.busy, "奥义演出禁止切换")
	_bm.set_meta("grans_cinematic_active", false)
	_ok(not Grans.state(_bm).get("release_mode", false), "所有拒绝均未改变模式")

func _bridge_dual() -> void:
	_banks(3, 3)
	await _mode_play(18, false)
	_ok(Grans.charge(_bm, "light") == 1 and Grans.charge(_bm, "dark") == 1, "蓄能模式合璧照常消耗两对")
