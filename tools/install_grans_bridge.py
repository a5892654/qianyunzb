"""将可版本管理的格兰斯接口安装到指定游戏工程；修改前备份，不触碰玩家存档。"""
import argparse
import datetime
import json
import re
import shutil
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "dtgame" / "godot"
ENTRY = 'WhaleTalkGransBridge="*res://addons/whaletalk/grans_bridge.gd"'


def install(project: Path, tests: bool = False) -> dict:
    project = project.resolve(strict=True)
    settings = project / "project.godot"
    if not (project / "scripts/battle/grans_card_effects.gd").is_file():
        raise ValueError("工程缺少格兰斯机制脚本，请指定实际 Godot 项目目录")
    raw = settings.read_bytes()
    text = raw.decode("utf-8-sig")
    match = re.search(r'(?m)^WhaleTalkGransBridge=.*$', text)
    if match and match.group().strip() != ENTRY:
        raise ValueError("已有不同的 WhaleTalkGransBridge 配置，请先核对，未修改工程")
    if not match:
        section = re.search(r'(?ms)^\[autoload\]\r?\n.*?(?=^\[|\Z)', text)
        if not section:
            raise ValueError("工程缺少 autoload 配置，未修改工程")
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text[:section.end()] + ENTRY + newline + newline + text[section.end():]
    changes = [(project / "addons/whaletalk/grans_bridge.gd", (SOURCE / "grans_bridge.gd").read_bytes())]
    if tests:
        for name in ("whaletalk_grans_verify.gd", "whaletalk_grans_verify.tscn",
                     "whaletalk_grans_protocol.gd", "whaletalk_grans_protocol.tscn"):
            changes.append((project / "tests" / name, (SOURCE / name).read_bytes()))
    changes.append((settings, text.encode("utf-8")))
    backup = project.parent / "outputs" / ("whaletalk_grans_install_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    changed = []
    for target, content in changes:
        if target.is_file() and target.read_bytes() == content:
            continue
        if target.is_file():
            saved = backup / target.relative_to(project)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        changed.append(str(target))
    return {"changed": changed, "backup": str(backup) if backup.exists() else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--tests", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.project, args.tests), ensure_ascii=False, indent=2))
