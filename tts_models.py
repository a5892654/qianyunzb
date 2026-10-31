"""Shared model choices for both streaming and ComfyUI speech backends."""
from pathlib import Path

import config

SIZES = ("0.6B", "1.7B")
MODEL_DIR = Path(config.BASE_DIR) / "models" / "tts"


def model_id(size):
    if size not in SIZES:
        raise ValueError("语音模型请选择 0.6B 或 1.7B")
    return f"Qwen/Qwen3-TTS-12Hz-{size}-Base"


def selected_size(settings):
    explicit = settings.get("qwen_model_size")
    if explicit in SIZES:
        return explicit
    for key in ("ttsd_model", "qwen_model"):
        for size in SIZES:
            if f"-{size}-" in str(settings.get(key, "")):
                return size
    return "0.6B"


def selection_patch(settings, size):
    identifier = model_id(size)
    name = identifier.split("/")[-1]
    candidates = [MODEL_DIR / name]
    current = str(settings.get("ttsd_model", ""))
    if current and current not in [model_id(value) for value in SIZES]:
        candidates.insert(0, Path(current).parent / name)
    path = next((p for p in candidates if downloaded(p)), None)
    return {"qwen_model_size": size, "qwen_model": identifier,
            "ttsd_model": str(path) if path else identifier}


def choices(settings):
    """按实际解析出来的路径判断是否已下载：模型既可能在仓库 models/tts，
    也可能在旧配置指向的目录旁边（本机就是 ComfyUI 的模型目录）。"""
    items = []
    for size in SIZES:
        path = Path(selection_patch(settings, size)["ttsd_model"])
        items.append({"size": size, "name": model_id(size), "downloaded": downloaded(path)})
    return items


def downloaded(path):
    return ((path / "config.json").is_file() and (path / "model.safetensors").is_file()
            and (path / "speech_tokenizer" / "model.safetensors").is_file())
