"""Weather rotation and its effect on the climbing run."""

from dataclasses import dataclass

FIRST_WEATHER_TURN = 9
WEATHER_TURNS = (9, 14)
WEATHER_SECONDS = (32, 48)
FIRST_WEATHER_SECONDS = 55
WEATHER_BLEND = 2.4


def temperature_at(altitude):
    """Game air temperature in Celsius, with the dock at 24°C."""
    return 24 - altitude * 0.0065


@dataclass(frozen=True, slots=True)
class Weather:
    key: str
    name: str
    hint: str
    fact: str
    emotion: str
    notice: str
    wind: int = 0
    gust: int = 0
    gust_scale: float = 1.0
    slip: float = 0.0
    slip_fact: str = ""
    drift: float = 0.0
    brace: float = 0.0
    slip_lift: float = 0.33


WEATHERS = {
    "clear": Weather("clear", "晴朗", "风稳 · 阵风少", "天气转晴，风稳下来，吸盘抓得最踏实", "开心",
                     "天气转晴 · 风稳好抓", gust_scale=0.65),
    "tail": Weather("tail", "顺风", "顺推 · 节奏更快", "顺风起来了，起跳节奏都轻快，能省不少力", "开心",
                    "顺风起来了 · 攀升更省力", wind=30, brace=-0.8),
    "head": Weather("head", "逆风", "顶风 · 起跳吃力", "逆风压上来了，每一次起跳都更吃力，节奏得慢下来", "惊讶",
                    "逆风压上来了 · 跳距缩水", wind=-35, brace=0.9),
    "gusty": Weather("gusty", "阵风", "横推 · 航向偏斜", "阵风上来了，半空航向被横着推，得提前修正", "惊讶",
                     "阵风上来了 · 半空会横推", gust=70, brace=0.35),
    "storm": Weather("storm", "雷雨", "湿滑 · 偶尔打滑", "雷雨浇下来了，岩面湿滑，偶尔会打滑", "惊讶",
                     "雷雨来了 · 岩面湿滑", gust=20, gust_scale=0.9, slip=0.02,
                     slip_fact="雷雨浇得岩面发滑，吸盘一起跳就打滑", slip_lift=0.78, brace=0.8),
    "fog": Weather("fog", "浓雾", "遮眼 · 起跳谨慎", "大雾压下来了，看不清落点，每次起跳都要多等一会儿", "尴尬",
                   "浓雾压下来了 · 落点看不清", gust_scale=0.6, drift=8, brace=1.1),
    "blizzard": Weather("blizzard", "暴风雪", "横风夹雪 · 又慢又滑", "暴风雪扑上来了，横风夹雪，又冷又滑，节奏全拖慢", "惊讶",
                        "暴风雪来了 · 横风夹雪", wind=-25, gust=80, gust_scale=1.1, slip=0.015,
                        slip_fact="暴风雪把岩面冻得发滑，吸盘抓不实", slip_lift=0.75, brace=1.3),
}

CLEAR = WEATHERS["clear"]

_WEIGHT_TABLE = {
    0: (("clear", 5), ("tail", 3), ("head", 1), ("gusty", 1), ("storm", 1), ("fog", 1)),
    4: (("clear", 4), ("tail", 3), ("head", 1), ("gusty", 1), ("storm", 1), ("fog", 1)),
    8: (("clear", 3), ("tail", 3), ("head", 1), ("gusty", 1), ("fog", 1), ("blizzard", 1)),
}


def pick_weather(rng, chapter, exclude=None):
    table = _WEIGHT_TABLE[max(key for key in _WEIGHT_TABLE if key <= chapter)]
    choices = [(key, weight) for key, weight in table if key != exclude]
    total = sum(weight for _, weight in choices)
    roll = rng.uniform(0, total)
    for key, weight in choices:
        roll -= weight
        if roll <= 0:
            return WEATHERS[key]
    return WEATHERS[choices[-1][0]]
