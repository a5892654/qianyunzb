extends Node
## WhaleTalk 的格兰斯补充接口。只读真实战场；切换调用游戏自己的 set_release_mode。
const Rules = preload("res://scripts/battle/grans_card_effects.gd")
const Norms = preload("res://scripts/cards/card_norms.gd")
const PROTOCOL := "whaletalk.grans.v1"
var _server := TCPServer.new()
var _peers: Array[Dictionary] = []

func _ready() -> void:
	if OS.get_environment("WHALETALK_GRANS_NO_TCP") == "1":
		set_process(false)
		return
	var port := int(ProjectSettings.get_setting("whaletalk/grans_port", 9788))
	var test_port := OS.get_environment("WHALETALK_GRANS_PORT")
	if test_port.is_valid_int() and int(test_port) > 0 and int(test_port) <= 65535:
		port = int(test_port)
	var error := _server.listen(port, "127.0.0.1")
	if error != OK:
		push_warning("[WhaleTalk] 格兰斯接口未启动，端口 %d：%s" % [port, error_string(error)])
		set_process(false)

func _process(_delta: float) -> void:
	while _server.is_connection_available():
		var peer := _server.take_connection()
		if _peers.size() >= 4:
			peer.disconnect_from_host()
		else:
			_peers.append({"peer": peer, "bytes": PackedByteArray(), "at": Time.get_ticks_msec()})
	for i in range(_peers.size() - 1, -1, -1):
		var entry: Dictionary = _peers[i]
		var peer: StreamPeerTCP = entry.peer
		peer.poll()
		var remove := peer.get_status() != StreamPeerTCP.STATUS_CONNECTED or Time.get_ticks_msec() - int(entry.at) > 3000
		if not remove and peer.get_available_bytes() > 0:
			var chunk := peer.get_data(peer.get_available_bytes())
			if int(chunk[0]) != OK:
				remove = true
			else:
				var bytes: PackedByteArray = entry.bytes
				bytes.append_array(chunk[1])
				entry.bytes = bytes
				var end := bytes.find(10)
				if bytes.size() > 65536:
					remove = true
				elif end >= 0:
					var request: Variant = JSON.parse_string(bytes.slice(0, end).get_string_from_utf8())
					var response := dispatch(request) if request is Dictionary else _error("请求必须是 JSON 对象")
					peer.put_data((JSON.stringify(response) + "\n").to_utf8_buffer())
					remove = true
		if remove:
			peer.disconnect_from_host()
			_peers.remove_at(i)

func _error(message: String, stale := false, busy := false) -> Dictionary:
	return {"ok": false, "protocol": PROTOCOL, "error": message, "stale": stale, "busy": busy}

func _host() -> Node:
	if not Rules.is_grans(): return null
	var reference: Variant = GameManager.get_meta("grans_battle_host", null)
	var host: Node = reference.get_ref() if reference is WeakRef else null
	return host if is_instance_valid(host) and host.is_inside_tree() and not host.is_queued_for_deletion() else null

func _busy(host: Node) -> bool:
	return (bool(host.get("battle_over")) or GameManager.player_hp <= 0
		or bool(host.get("_entry_in_progress")) or bool(host.get("_end_turn_guard"))
		or bool(host.get("_player_attack_playing")) or bool(host.get("_harmony_resonance_playing"))
		or bool(host.get("_admin_card_auth_pending")) or bool(host.get("_alice_input_blocking"))
		or bool(host.get_meta("grans_cinematic_active", false))
		or not (host.get_meta("_pending_card_choices", []) as Array).is_empty()
		or host.get_node_or_null("CardChoiceLayer") != null)

func snapshot() -> Dictionary:
	var host := _host()
	if host == null: return _error("当前不是格兰斯战斗")
	var data: Dictionary = Rules.state(host)
	var hand_ids: Array = GameManager.hand.duplicate()
	var cards: Array[Dictionary] = []
	for card in host.get("hand_cards"):
		if not is_instance_valid(card) or not card.has_method("get_card_data"): continue
		var cd: Dictionary = card.get_card_data()
		var slot := hand_ids.find(str(cd.get("id", "")))
		if slot < 0: continue
		var side: String = Rules.sword(cd)
		var quota: Dictionary = data.get("quota", {})
		var cross := side in ["light", "dark"] and not bool(cd.get("_grans_no_resource", false)) and not bool(quota.get(Rules.origin(cd) + ":cross", false))
		var cap: int = Rules.pair_cap(cd, Rules.charge(host, "light") == Rules.charge(host, "dark") and Rules.charge(host, "light") > 0)
		var pairs: int = mini(cap, mini(Rules.charge(host, "light"), Rules.charge(host, "dark"))) if not cd.get("_grans_no_resource", false) else 0
		cards.append({"slot": slot, "id": str(cd.get("id", "")), "name": str(cd.get("name", "")),
			"sword": side, "can_release": Rules.can_release(host, cd), "cross_charge_available": cross,
			"preview": Rules.release_preview(host, cd), "pair_spend": pairs,
			"normal_desc": Norms.render(Rules.display_description(host, cd, false), cd),
			"release_desc": Rules.display_description(host, cd, true)})
	return {"ok": true, "protocol": PROTOCOL, "battle_id": str(host.get_instance_id()),
		"character_id": Rules.CHAR_ID, "turn": int(host.get("turn_number")), "hand_ids": hand_ids,
		"light": Rules.charge(host, "light"), "dark": Rules.charge(host, "dark"), "capacity": Rules.CAP,
		"release_mode": bool(data.get("release_mode", false)), "can_switch": not _busy(host),
		"busy": _busy(host), "cards": cards}

func set_mode(args: Dictionary) -> Dictionary:
	if typeof(args.get("release")) != TYPE_BOOL: return _error("release 必须是布尔值")
	var current := snapshot()
	if not current.get("ok", false): return current
	if (str(args.get("battle_id", "")) != current.battle_id
		or args.get("turn", -1) != current.turn or args.get("hand_ids", []) != current.hand_ids
		or args.get("light", -1) != current.light or args.get("dark", -1) != current.dark
		or args.get("release_mode") != current.release_mode):
		return _error("战斗、回合、手牌或蓄能模式已经变化，请重读状态", true)
	if not current.can_switch: return _error("正在结算、播放动画或等待卡牌选择，暂不能切换", false, true)
	Rules.set_release_mode(_host(), bool(args.release))
	var after := snapshot()
	if after.get("release_mode") != args.release: return _error("游戏没有完成模式切换")
	return after

func dispatch(request: Dictionary) -> Dictionary:
	var args: Variant = request.get("args", {})
	if not args is Dictionary: return _error("args 必须是对象")
	match str(request.get("cmd", "")):
		"grans_state": return snapshot()
		"grans_mode": return set_mode(args)
	return _error("未知格兰斯接口命令")

func _exit_tree() -> void:
	for entry in _peers: (entry.peer as StreamPeerTCP).disconnect_from_host()
	_peers.clear()
	_server.stop()
