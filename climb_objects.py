"""Dimensions and names for the solid objects shared by Blender and the game."""

from dataclasses import dataclass
from math import cos, radians


@dataclass(frozen=True, slots=True)
class ClimbObject:
    label: str
    width: float
    height: float
    max_width: int

    @property
    def projection(self):
        span = self.width * PIXELS_PER_METER
        depth = self.height * PIXELS_PER_METER * cos(radians(PROJECTION_TILT))
        return (PLATE_WIDTH - span) / 2, RIM_ROW, span, depth


PLATE_WIDTH, PLATE_HEIGHT = 960, 640
PIXELS_PER_METER = 120
PROJECTION_TILT = 12
RIM_ROW = 128
FRONT_FACE = -1

CLIMB_OBJECTS = {
    "oil_drum": ClimbObject("独立油桶", 1.20, 1.62, 110),
    "guard_house": ClimbObject("红瓦门卫房", 3.80, 3.25, 220),
    "site_office": ClimbObject("工地板房", 4.60, 3.06, 275),
    "weigh_house": ClimbObject("砖砌地磅房", 6.42, 3.30, 410),
    "lift_cab": ClimbObject("井架升降轿厢", 2.36, 3.02, 150),
    "cargo_container": ClimbObject("海运集装箱", 4.75, 2.79, 255),
    "shipping_crate": ClimbObject("木制货箱", 2.06, 1.75, 155),
    "cable_reel": ClimbObject("木制电缆盘", 2.14, 1.80, 145),
    "water_tank": ClimbObject("笼装储水桶", 1.78, 1.74, 141),
    "generator": ClimbObject("柴油发电机", 2.96, 2.11, 188),
    "vent_unit": ClimbObject("双风扇通风机", 2.73, 1.59, 186),
    "tire_stack": ClimbObject("轮胎堆", 1.78, 1.29, 129),
    "tool_chest": ClimbObject("抽屉工具柜", 1.78, 2.17, 135),
    "traffic_barrier": ClimbObject("施工隔离墩", 3.36, 1.57, 208),
    "parcel_locker": ClimbObject("旧邮包储物柜", 2.18, 2.50, 157),
    "pump_cabinet": ClimbObject("高压泵机组", 2.70, 2.22, 177),
}
