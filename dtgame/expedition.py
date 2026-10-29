"""出征深度的读取与校验。只使用游戏桥报告的解锁范围，不修改账号进度。"""

MAX_DEPTH = 20


def depth_settings(state: dict) -> tuple[int | None, int | None]:
    hall = state.get("hall") or {}
    if hall.get("expedition_mode", hall.get("mode")) == "shallow":
        return 0, 0
    current = hall.get("selected_depth", hall.get("depth"))
    limit = hall.get("unlocked_depth")
    limit = max(0, min(MAX_DEPTH, limit)) if type(limit) is int else None
    current = max(0, min(limit if limit is not None else MAX_DEPTH, current)) if type(current) is int else None
    return current, limit


def choose_depth(state: dict, requested, fallback: int = 0) -> int | None:
    current, limit = depth_settings(state)
    if limit is None:
        return None  # 旧桥未给解锁上限：不猜测，begin_expedition 不传 depth。
    if type(requested) is not int:
        requested = current if current is not None else fallback
    if type(requested) is not int:
        requested = 0
    return max(0, min(limit, requested))
