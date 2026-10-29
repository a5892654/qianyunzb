extends "res://tests/whaletalk_grans_protocol.gd"

func _prepare_protocol() -> void:
	GameManager.collected_relics.clear()
	GameManager.active_great_rune.clear()
	GameManager.arena_active = false
	GameManager.game_mode = "deep"
	GameManager.energy = 3
	var faor: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://data/enemies/boss_faor.json"))
	_bm._init_enemies([faor])
	GameManager.faor_active = true
	GameManager.player_hp = 5
	GameManager.player_block = 0
	GameManager.set("_death_dmg_log", {})
	_bm._handle_enemy_special(enemy(0), {"name": "完全之一击", "value": 55, "discard_draw": 0})
	_ok(GameManager.player_hp == 0 and not _bm.battle_over, "初始为法奥尔战场0血、尚未判负")

func _state() -> Dictionary:
	var current := super._state()
	var analysis := false
	for button in _bm.find_children("*", "Button", true, false):
		if button.text == "确认死亡": analysis = true
	current["test"] = {"hollow": GameManager.hollow_level, "turn": _bm.turn_number,
		"energy": GameManager.energy, "analysis": analysis}
	if _bm.battle_over:
		return {"screen": "death_analysis", "busy": false, "run": current.run,
			"death_analysis": {"texts": ["确认死亡"]},
			"test": current.test}
	# 模拟旧桥的“0血=busy”，代打仍必须能读到HP并走恢复分支。
	current.busy = true
	current.battle.dead = true
	return current
