"""Original terrain and collision surfaces for the full-length cliff course."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import random


BASE_Y = 553
STEPS_PER_CHAPTER = 132


@dataclass(frozen=True, slots=True)
class Ledge:
    x: int
    y: int
    width: int
    depth: int
    section: str = ""
    material: str = "sandstone"
    slope: int = 0
    chapter: int = 0
    exposed: bool = False

    @property
    def right(self):
        return self.x + self.width

    @property
    def center(self):
        return self.x + self.width / 2

    def surface(self, x):
        return self.y + (x - self.center) * (self.slope / self.width)

    def landing_bounds(self, vx):
        margin = 17 + self.chapter
        if self.chapter >= 7 and self.material in {"steel", "snow"}:
            margin += min(10, abs(vx) * 0.035)
        return self.x + margin, self.right - margin

    def underside(self, rim):
        return rim + min(self.depth * 0.70, 88 + self.chapter * 2)


@dataclass(frozen=True, slots=True)
class Chapter:
    name: str
    backdrop: int
    materials: tuple[str, ...]
    shifts: tuple[int, ...]
    width: tuple[int, int]
    rise: tuple[int, int]
    slope: int = 0
    wind: int = 0
    gust: int = 0


CHAPTERS = (
    Chapter("旧船坞", 0, ("sandstone", "wood", "basalt"), (230, 246, -209, -255), (168, 274), (108, 140), 8),
    Chapter("潮裂玄武崖", 0, ("basalt", "sandstone"), (274, -228, -240, 260, 197), (160, 264), (110, 141), 10, 8),
    Chapter("雾松根径", 1, ("wood", "basalt", "limestone"), (-217, -178, 270, 220), (154, 253), (112, 142), 12, 14),
    Chapter("断栈横沟", 1, ("wood", "scaffold", "granite"), (291, 213, -292, -194), (148, 242), (114, 143), 14, 20, 56),
    Chapter("滴水钟乳洞", 2, ("limestone", "basalt"), (-183, 250, 200, -237), (141, 231), (116, 145), 16, 26, 80),
    Chapter("回声竖井", 2, ("limestone", "scaffold", "granite"), (240, -275, 231, -246), (135, 220), (118, 147), 18, 32, 105),
    Chapter("赤锈采石场", 3, ("steel", "sandstone", "scaffold"), (296, 185, -263, -241), (129, 209), (120, 149), 20, 38, 130),
    Chapter("废塔外架", 3, ("scaffold", "steel", "granite"), (-247, -236, 298, 205), (123, 198), (122, 151), 22, 44, 155),
    Chapter("覆雪冰碛", 4, ("snow", "granite", "limestone"), (218, 251, -220, -270), (118, 187), (124, 153), 24, 50, 180),
    Chapter("横风冰壁", 4, ("snow", "granite"), (-278, 285, -233, 258), (112, 176), (126, 155), 26, 57, 210),
    Chapter("云上断脊", 5, ("granite", "limestone", "snow"), (252, -298, -208, 281), (108, 165), (128, 157), 28, 64, 245),
    Chapter("晨曦峰肩", 5, ("granite", "snow", "steel"), (-270, 228, 245, -287), (103, 154), (130, 159), 30, 71, 285),
)


def _make_course():
    ledges = [Ledge(-140, BASE_Y, 724, 320, CHAPTERS[0].name),
              Ledge(648, 430, 420, 249, material="basalt"),
              Ledge(926, 299, 350, 280),
              Ledge(625, 166, 236, 109, material="wood", slope=-15)]
    checkpoints = [0]
    y = ledges[-1].y
    center = round(ledges[-1].center)
    for chapter, design in enumerate(CHAPTERS):
        rng = random.Random(1799 + chapter * 113)
        first = 3 if not chapter else 0
        for step in range(first, STEPS_PER_CHAPTER):
            delta = design.shifts[step % len(design.shifts)] + rng.randrange(-22, 23)
            if not 180 <= center + delta <= 1080:
                delta = -delta
            center += delta
            y -= rng.randint(*design.rise)
            width = rng.randint(*design.width)
            entry = step == 0
            summit = chapter == len(CHAPTERS) - 1 and step == STEPS_PER_CHAPTER - 1
            if entry or summit:
                width = max(width, 295)
            material = design.materials[(step + step // 5) % len(design.materials)]
            depth = (57 if material in {"steel", "scaffold", "wood"} else rng.randint(84, 167) + chapter * 2)
            slope = 0 if entry or summit else rng.randint(-design.slope, design.slope)
            exposed = bool(design.gust and not entry and not summit and (step + chapter * 3) % (22 - chapter) == 0)
            ledges.append(Ledge(center - width // 2, y, width, depth,
                                design.name if entry else "", material, slope, chapter, exposed))
            if entry:
                checkpoints.append(len(ledges) - 1)
    return tuple(ledges), tuple(checkpoints)


LEDGES, CHECKPOINTS = _make_course()
_ELEVATIONS = tuple(-ledge.y for ledge in LEDGES)


def ledge_indices(top, bottom):
    """Indices whose nominal upper surface is within the world-space interval."""
    return range(bisect_left(_ELEVATIONS, -bottom), bisect_right(_ELEVATIONS, -top))
