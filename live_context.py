"""按当前观众原话选择素材信息，不从历史回复或主动聊天提示里触发。"""
import re


_RICE = re.compile(r"白饭|米饭|饭碗|饭盆|饭的碗|小碗|碗数|几碗|多少碗|加[一1]?碗|这碗|那碗|碗饭")
_CORNER = re.compile(r"左下|右下|左上|右上|角落|画面[边底]上|屏幕[边底]上")
_ASK = re.compile(r"什么|是啥|干[嘛吗]|作用|[？?]")


def mentions_rice(text):
    return bool(_RICE.search(str(text or "")))


def rice_question(text):
    text = str(text or "")
    return mentions_rice(text) or bool(_CORNER.search(text) and _ASK.search(text)
                                     and not re.search(r"卡牌|技能|敌人|角色|血条", text))
