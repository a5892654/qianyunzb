import copy
import glob
import json
import os

import config

# One sub-folder per skin; a skin is any folder containing a *.model3.json.
_LEGACY_LIBRARY = r"E:\cli\live2d-models"
LIBRARY_DIR = os.environ.get("WHALETALK_L2D_LIBRARY") or (
    _LEGACY_LIBRARY if os.path.isdir(_LEGACY_LIBRARY) else os.path.join(config.BASE_DIR, "live2d-models")
)
PROFILE_FIELDS = (
    "model_file", "scale", "x", "y", "breath", "blink", "sway", "lipsync", "lip_gain",
    "idle_motion", "base_expressions", "emotions", "expr_hold", "bubble_x", "bubble_y",
    "parameter_map", "face_presets", "motion_resets",
)


def list_library(root=None):
    root = root or LIBRARY_DIR
    items = []
    if not os.path.isdir(root):
        return items
    for name in sorted(os.listdir(root), key=str.lower):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        files = sorted(glob.glob(os.path.join(d, "**", "*.model3.json"), recursive=True))
        if not files:
            continue
        items.append({"key": name, "dir": d, "model_file": _rel(d, files[0])})
    return items


def switch_model(l2d, key):
    """Prepare a complete skin switch before changing the active configuration."""
    target = next((item for item in list_library() if item["key"] == key), None)
    if not target:
        raise FileNotFoundError("模型库中找不到：" + key)
    profiles = copy.deepcopy(l2d.get("profiles") or {})
    current = os.path.basename(os.path.normpath(l2d.get("model_dir") or ""))
    if l2d.get("model_dir"):
        profiles[current] = {k: copy.deepcopy(l2d[k]) for k in PROFILE_FIELDS if k in l2d}
    saved = profiles.get(key) or {}
    profile = {k: copy.deepcopy(config.DEFAULTS["live2d"][k]) for k in PROFILE_FIELDS}
    defaults_path = safe_path(target["dir"], "whaletalk.model.json")
    if defaults_path:
        with open(defaults_path, encoding="utf-8-sig") as f:
            defaults = json.load(f)
        if not isinstance(defaults, dict):
            raise ValueError("模型预设必须为 JSON 对象")
        profile.update({k: copy.deepcopy(defaults[k]) for k in PROFILE_FIELDS if k in defaults})
    profile.update({k: copy.deepcopy(saved[k]) for k in PROFILE_FIELDS if k in saved})
    profile["model_dir"] = target["dir"]
    files = list_model_files(target["dir"])
    if profile["model_file"] not in files:
        profile["model_file"] = target["model_file"]
    # Defaults retain the shared emotion names used by AI/fixed replies, with only
    # expressions and motions that actually exist on this skin.
    exprs, motions = list_assets(target["dir"])
    expr_ids, motion_ids = {e["id"] for e in exprs}, {m["id"] for m in motions}
    profile["base_expressions"] = [e for e in profile["base_expressions"] if e in expr_ids]
    for emotion in profile["emotions"]:
        emotion["expressions"] = [e for e in emotion.get("expressions", []) if e in expr_ids]
        if emotion.get("motion") not in motion_ids and emotion.get("motion") != "@reset":
            emotion["motion"] = ""
    if profile["idle_motion"] not in motion_ids:
        profile["idle_motion"] = ""
    build_model_json(profile)  # Invalid JSON must leave the current skin intact.
    l2d.update(profile)
    l2d["profiles"] = profiles
    return target


def safe_path(base, rel):
    base = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base, rel))
    if full.startswith(base + os.sep) and os.path.isfile(full):
        return full
    return None


def _stem(path, suffix):
    name = os.path.basename(path)
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _rel(base, path):
    return os.path.relpath(path, base).replace("\\", "/")


def _vts_names(model_dir):
    names = {}
    for f in sorted(glob.glob(os.path.join(model_dir, "**", "*.vtube.json"), recursive=True)):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                for h in json.load(fp).get("Hotkeys", []):
                    if h.get("File") and h.get("Name"):
                        names[os.path.basename(h["File"])] = h["Name"]
        except (OSError, ValueError):
            pass
    return names


def list_model_files(model_dir):
    return sorted(_rel(model_dir, f) for f in glob.glob(os.path.join(model_dir, "**", "*.model3.json"), recursive=True))


def list_assets(model_dir):
    vts = _vts_names(model_dir)
    exprs, motions = [], []
    for f in sorted(glob.glob(os.path.join(model_dir, "**", "*.exp3.json"), recursive=True)):
        sid = _stem(f, ".exp3.json")
        label = vts.get(os.path.basename(f), sid)
        exprs.append({"id": sid, "file": _rel(model_dir, f), "label": label if label == sid else f"{label}（{sid}）"})
    for f in sorted(glob.glob(os.path.join(model_dir, "**", "*.motion3.json"), recursive=True)):
        sid = _stem(f, ".motion3.json")
        label = vts.get(os.path.basename(f), sid)
        mouth = False
        try:
            with open(f, encoding="utf-8-sig") as fp:
                data = json.load(fp)
            duration = float(data.get("Meta", {}).get("Duration", 0))
            mouth = any(c.get("Id") in ("ParamMouthOpenY", "PARAM_MOUTH_OPEN_Y", "JawOpen")
                        for c in data.get("Curves", []))
        except (OSError, ValueError, TypeError):
            duration = 0
        motions.append({"id": sid, "file": _rel(model_dir, f), "duration": duration, "mouth": mouth,
                        "label": label if label == sid else f"{label}（{sid}）"})
    return exprs, motions


def build_model_json(l2d):
    model_dir = l2d["model_dir"]
    path = safe_path(model_dir, l2d["model_file"])
    if not path:
        raise FileNotFoundError("找不到模型文件")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    sub = os.path.dirname(_rel(model_dir, path))
    refs = data.setdefault("FileReferences", {})

    def fix(p):
        return f"{sub}/{p}" if sub else p

    for key in ("Moc", "Physics", "Pose", "DisplayInfo", "UserData"):
        if refs.get(key):
            refs[key] = fix(refs[key])
    refs["Textures"] = [fix(t) for t in refs.get("Textures", [])]
    refs.pop("Expressions", None)
    _, motions = list_assets(model_dir)
    groups = {m["id"]: [{"File": m["file"], "FadeInTime": 0.3, "FadeOutTime": 0.3}] for m in motions}
    idle = l2d.get("idle_motion")
    if idle and idle in groups:
        groups["Idle"] = groups[idle]
    refs["Motions"] = groups
    return data
