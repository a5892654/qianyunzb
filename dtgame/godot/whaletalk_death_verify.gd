extends "res://tests/test_grans_combat.gd"
## 在真实战场还原“0血、battle_over=false、可结束回合”，不改角色数值或玩家存档。

func cases() -> Array:
	return [
		{"label": "格兰斯0血不可切换，但不应永久busy", "run": _zero_is_not_busy},
		{"label": "演出和过期战场不能插入死亡检查", "run": _guards},
		{"label": "恢复前已回1血不能结束活人回合", "run": _already_saved},
		{"label": "真实幽影卢恩和轮回之种仍可保命", "run": _death_saves},
		{"label": "其他角色也可推进死亡检查", "run": _other_character},
		{"label": "法奥尔0血挂起后判负、显示死亡分析且只记一次", "run": _faor_zero_pending},
	]

func prepare_field() -> void:
	GameManager.current_character = DataManager.get_character("twilight_grans")
	GameManager.current_oath.clear()
	GameManager.active_great_rune.clear()
	GameManager.collected_relics.clear()
	GameManager.death_save_used_this_floor = false
	GameManager.arena_active = false
	GameManager.game_mode = "deep"
	await super.prepare_field()

func _api() -> Node:
	return get_node("/root/WhaleTalkGransBridge")

func _request() -> Dictionary:
	var current: Dictionary = _api().battle_status()
	return {"battle_id": current.battle_id, "turn": current.turn}

func _zero_is_not_busy() -> void:
	GameManager.player_hp = 0
	var current: Dictionary = _api().snapshot()
	_ok(current.ok and not current.busy and not current.can_switch, "0血禁止切换，但busy只表示真实结算")
	_ok(not current.life.battle_over and current.life.hp == 0, "识别尚未判负的0血战斗")

func _guards() -> void:
	GameManager.player_hp = 0
	var args := _request()
	args.battle_id = "previous_battle"
	_ok(_api().recover_death(args).stale and not _bm.battle_over, "拒绝过期战场")
	args = _request()
	args.turn = -1
	_ok(_api().recover_death(args).stale and not _bm.battle_over, "拒绝过期回合")
	_bm.set_meta("grans_cinematic_active", true)
	var result: Dictionary = _api().recover_death(_request())
	_ok(not result.ok and result.busy and not _bm.battle_over, "奥义演出期间等待正常结算")
	_bm.set_meta("grans_cinematic_active", false)

func _already_saved() -> void:
	GameManager.player_hp = 0
	var args := _request()
	GameManager.player_hp = 1
	var turn := int(_bm.turn_number)
	var energy := GameManager.energy
	var result: Dictionary = _api().recover_death(args)
	_ok(result.ok and result.hp == 1 and not result.battle_over, "已保命回1血时不判死")
	_ok(int(_bm.turn_number) == turn and GameManager.energy == energy, "不额外结束活人回合")

func _death_saves() -> void:
	GameManager.active_great_rune = {"id": "shadow_rune"}
	GameManager.player_hp = 0
	var result: Dictionary = _api().recover_death(_request())
	_ok(result.ok and result.hp == 1 and not _bm.battle_over, "游戏原生幽影卢恩保命")
	_ok(GameManager.active_great_rune.is_empty(), "卢恩按原规则消耗")
	GameManager.collected_relics = ["rebirth_seed"]
	GameManager.player_hp = 0
	result = _api().recover_death(_request())
	_ok(result.hp == 1 and not _bm.battle_over and GameManager.death_save_used_this_floor, "轮回之种照常保命且记录本层额度")

func _other_character() -> void:
	GameManager.current_character = DataManager.get_character("witch")
	GameManager.active_great_rune = {"id": "shadow_rune"}
	GameManager.player_hp = 0
	_ok(not _api().snapshot().ok, "其他角色不是格兰斯模式接口")
	var result: Dictionary = _api().recover_death(_request())
	_ok(result.ok and result.hp == 1 and not _bm.battle_over, "通用死亡接口仍可调用游戏原生保命")

func _faor_zero_pending() -> void:
	GameManager.current_character = DataManager.get_character("witch")
	var faor: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://data/enemies/boss_faor.json"))
	_bm._init_enemies([faor])
	GameManager.faor_active = true
	GameManager.player_hp = 5
	GameManager.player_block = 0
	GameManager.set("_death_dmg_log", {})
	_bm._handle_enemy_special(enemy(0), {"name": "完全之一击", "value": 55, "discard_draw": 0})
	await settle(2)
	_ok(enemy(0).enemy_data.id == "faor_god_of_completion", "夹具确实为真实法奥尔")
	_ok(GameManager.player_hp == 0 and not _bm.battle_over, "还原法奥尔致命伤后的未结算窗口")
	var hollow := GameManager.hollow_level
	var turn := int(_bm.turn_number)
	var result: Dictionary = _api().recover_death(_request())
	_ok(result.ok and result.battle_over, "不用手动结束回合即完成死亡判定")
	_ok(GameManager.hollow_level == hollow + 1 and int(_bm.turn_number) == turn, "正常记录一次死亡，不推进额外敌方回合")
	var analysis := false
	for button in _bm.find_children("*", "Button", true, false):
		if button.text == "确认死亡": analysis = true
	_ok(analysis, "死亡分析已出现，可交给原有自动确认流程")
	_api().recover_death(_request())
	_ok(GameManager.hollow_level == hollow + 1, "重复检查不重复处罚")
