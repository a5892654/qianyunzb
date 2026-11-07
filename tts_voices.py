"""Reference voice presets for the Qwen3-TTS streaming clone."""
import os

from dtgame import ttsd

# 参考音放在 ComfyUI/input；相对文件名让流式引擎和 ComfyUI 回退路径都能找到。
# 肥鱼两段来自 astrbot_plugin_feiyu_voice 的 CosyVoice3 声库（素材授权见该项目）。
PRESETS = (
    {
        "id": "feiyu_cute",
        "name": "肥鱼 · 可爱",
        "file": "feiyu_cute.wav",
        "note": "cute 声线 · 星落落声库",
    },
    {
        "id": "feiyu_gentle",
        "name": "肥鱼 · 温柔",
        "file": "feiyu_gentle.wav",
        "note": "gentle 声线 · 星落落声库",
    },
    {
        "id": "dafeiyu_ep07",
        "name": "大肥鱼 · EP07",
        "file": "dafeiyu_voice_ref_ep07.wav",
        "note": "干净人声 · 语速轻快",
    },
    {
        "id": "dafeiyu",
        "name": "大肥鱼 · 旧参考",
        "file": "dafeiyu_voice_ref.wav",
        "note": "含背景音 · 保留回退",
    },
)
_BY_ID = {item["id"]: item for item in PRESETS}


def resolve(settings, filename):
    return ttsd._resolve_ref({**settings, "qwen_ref_audio": filename})


def choices(settings):
    items = []
    for preset in PRESETS:
        path = resolve(settings, preset["file"])
        items.append({**preset, "path": path, "exists": os.path.isfile(path)})
    return items


def select(voice):
    preset = _BY_ID.get(str(voice or ""))
    if preset is None:
        raise ValueError("未知音色")
    return preset


def current_id(settings):
    current = _normalize(ttsd._resolve_ref(settings))
    for preset in PRESETS:
        if _normalize(resolve(settings, preset["file"])) == current:
            return preset["id"]
    return "custom"


def current_name(settings):
    preset = _BY_ID.get(current_id(settings))
    return preset["name"] if preset else "自定义参考音"


def _normalize(path):
    return os.path.normcase(os.path.normpath(str(path or "")))
