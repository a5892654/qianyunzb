extends "res://tests/test_grans_combat.gd"
## 给 Python 端到端测试提供最小旧桥入口；真正模式开关走生产的补充接口。

func cases() -> Array:
	return [{"label": "Python真实TCP出牌链", "run": _serve}]

func _state() -> Dictionary:
	var snapshot: Dictionary = get_node("/root/WhaleTalkGransBridge").snapshot()
	var hand: Array = []
	for card in _bm.hand_cards:
		if not is_instance_valid(card): continue
		var cd: Dictionary = card.get_card_data()
		var slot := GameManager.hand.find(str(cd.id))
		if slot < 0: continue
		hand.append({"slot": slot, "id": cd.id, "name": cd.get("name", ""), "cost": cd.get("cost", 1),
			"type": cd.get("type", ""), "target": cd.get("target", "single"), "desc": cd.get("description", "")})
	hand.sort_custom(func(a: Dictionary, b: Dictionary): return int(a.slot) < int(b.slot))
	return {"screen": "battle", "busy": snapshot.get("busy", true),
		"run": {"character": "光暗双剑·格兰斯", "character_id": "twilight_grans", "hp": GameManager.player_hp, "max_hp": GameManager.player_max_hp},
		"battle": {"turn": _bm.turn_number, "energy": GameManager.energy, "max_energy": GameManager.max_energy,
			"hp": GameManager.player_hp, "max_hp": GameManager.player_max_hp, "block": GameManager.player_block,
			"hand": hand, "enemies": [{"name": "演武傀儡", "hp": enemy(0).unit_hp, "max_hp": 900,
				"intent": {"type": "attack", "value": 3}}]}}

func _serve() -> void:
	GameManager.energy = 3
	give_card("grans_001")
	give_card("grans_002")
	give_card("grans_001")
	var server := TCPServer.new()
	var error := server.listen(int(OS.get_environment("WHALETALK_TEST_PRIMARY_PORT")), "127.0.0.1")
	_ok(error == OK, "测试旧桥监听成功")
	if error != OK: return
	var end := Time.get_ticks_msec() + 60000
	var finished := false
	while Time.get_ticks_msec() < end and not finished:
		if not server.is_connection_available():
			await get_tree().process_frame
			continue
		var peer := server.take_connection()
		var bytes := PackedByteArray()
		var deadline := Time.get_ticks_msec() + 2000
		while bytes.find(10) < 0 and Time.get_ticks_msec() < deadline:
			peer.poll()
			if peer.get_available_bytes() > 0:
				var chunk := peer.get_data(peer.get_available_bytes())
				if int(chunk[0]) == OK: bytes.append_array(chunk[1])
			await get_tree().process_frame
		var request: Variant = JSON.parse_string(bytes.get_string_from_utf8())
		var response := {"ok": false}
		if request is Dictionary:
			var cmd := str(request.get("cmd", ""))
			var args: Dictionary = request.get("args", {})
			match cmd:
				"state": response = {"state": _state()}
				"screen": response = {"screen": {"screen": "battle", "busy": _state().busy}}
				"play_card":
					var slot := int(args.get("slot", -1))
					if slot >= 0 and slot < GameManager.hand.size():
						var id := str(GameManager.hand[slot])
						if str(args.get("id", id)) == id:
							var node := hand_node(id)
							GameManager.resonance_chain = 0
							GameManager.last_element_played = ""
							await _bm._on_card_played(node.get_card_data(), node, int(args.get("target", 0)))
							response = {"ok": not GameManager.hand.has(id)}
				"test_finish":
					finished = true
					response = {"ok": true}
		peer.put_data((JSON.stringify(response) + "\n").to_utf8_buffer())
		peer.disconnect_from_host()
	server.stop()
	_ok(finished, "Python完成所有TCP断言")
