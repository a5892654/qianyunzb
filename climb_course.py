"""Original terrain and collision surfaces for the full-length cliff course."""

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
import random

from climb_objects import CLIMB_OBJECTS


BASE_Y = 553
WATER_Y = 704
STEPS_PER_CHAPTER = 132
PIXELS_PER_ALTITUDE_METER = 25


def altitude_at(y):
    return max(0, (BASE_Y - y) / PIXELS_PER_ALTITUDE_METER)


def scenery_height(altitude):
    return 8.5 + 4.2 * altitude / (altitude + 95) + 0.028 * altitude


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
    terrain: str = ""
    slick: bool = False
    landmark: str = ""
    piece: str = ""
    model: str = ""
    solid_height: int = 0

    @property
    def right(self):
        return self.x + self.width

    @property
    def center(self):
        return self.x + self.width / 2

    @property
    def surface_name(self):
        prop = CLIMB_OBJECTS.get(self.model)
        return prop.label if prop else self.terrain

    def surface(self, x):
        return self.y + (x - self.center) * (self.slope / self.width)

    def landing_bounds(self, vx):
        margin = 17 + self.chapter
        if self.chapter >= 7 and self.material in {"steel", "snow"}:
            margin += min(10, abs(vx) * 0.035)
        return self.x + margin, self.right - margin

    def underside(self, rim):
        return rim + (self.solid_height or min(self.depth * 0.70, 88 + self.chapter * 2))


@dataclass(frozen=True, slots=True)
class Chapter:
    name: str
    materials: tuple[str, ...]
    width: tuple[int, int]
    rise: tuple[int, int]
    slope: int = 0
    wind: int = 0
    gust: int = 0


CHAPTERS = (
    Chapter("旧船坞", ("sandstone", "wood", "basalt"), (168, 274), (108, 140), 8),
    Chapter("潮裂玄武崖", ("basalt", "sandstone"), (160, 264), (110, 141), 10, 8),
    Chapter("雾松根径", ("wood", "basalt", "limestone"), (154, 253), (112, 142), 12, 14),
    Chapter("断栈横沟", ("wood", "scaffold", "granite"), (148, 242), (114, 143), 14, 20, 56),
    Chapter("风化岩壁", ("limestone", "basalt"), (141, 231), (116, 145), 16, 26, 80),
    Chapter("迎风断层", ("limestone", "scaffold", "granite"), (135, 220), (118, 147), 18, 32, 105),
    Chapter("赤锈采石场", ("steel", "sandstone", "scaffold"), (129, 209), (120, 149), 20, 38, 130),
    Chapter("废塔外架", ("scaffold", "steel", "granite"), (123, 198), (122, 151), 22, 44, 155),
    Chapter("覆雪冰碛", ("snow", "granite", "limestone"), (118, 187), (124, 153), 24, 50, 180),
    Chapter("横风冰壁", ("snow", "granite"), (112, 176), (126, 155), 26, 57, 210),
    Chapter("云上断脊", ("granite", "limestone", "snow"), (108, 165), (128, 157), 28, 64, 245),
    Chapter("晨曦峰肩", ("granite", "snow", "steel"), (103, 154), (130, 159), 30, 71, 285),
)

# Each stretch has a different silhouette and transfer rhythm.
# Entries are horizontal displacement, change in rise and change in width.
STRETCHES = (
    ("折返栈道", ((210, -12, 25), (-184, 6, -12), (236, -8, 18), (-192, 12, -18))),
    ("贴壁窄台", ((154, 10, -38), (166, 6, -27), (-174, -9, -34), (-158, 15, -41))),
    ("断崖横沟", ((309, -13, 8), (252, 0, -10), (-310, -16, -4), (-278, 4, 12), (226, 11, 22))),
    ("错层挑檐", ((181, -21, -25), (248, 15, 29), (-213, -16, -20), (-273, 15, 7), (171, -10, -15))),
    ("长台转角", ((193, -12, 42), (235, 4, -8), (209, -8, 35), (-296, -14, -25), (-190, 9, 30))),
)

CHAPTER_STRETCHES = (
    (0, 4, 2), (0, 3, 2), (4, 0, 1), (2, 4, 3),
    (3, 1, 0), (1, 3, 2), (4, 3, 2), (2, 0, 4),
    (1, 4, 3), (2, 1, 3), (3, 2, 1), (1, 3, 2),
)

@dataclass(frozen=True, slots=True)
class Landmark:
    name: str
    kind: str
    first: int
    # Each part is the visible climbing surface for its ledge.
    # Values are center, width, thickness, slope and construction piece.
    parts: tuple[tuple[int, int, int, int, str], ...]

    @property
    def last(self):
        return self.first + len(self.parts) - 1


LANDMARKS = (
    Landmark("倒挂沉船", "wreck", 65, (
        (728, 144, 35, -17, "plank"),
        (414, 181, 39, -15, "plank"),
        (631, 186, 38, 19, "plank"),
        (448, 117, 40, -22, "plank"),
        (679, 153, 37, 16, "plank"),
        (485, 140, 37, -6, "plank"),
        (687, 165, 33, 3, "plank"),
    )),
    Landmark("裂面天文钟", "clock", 416, (
        (701, 156, 32, 0, "tooth"),
        (519, 135, 38, -10, "tooth"),
        (709, 157, 37, 7, "tooth"),
        (516, 135, 39, 0, "tooth"),
        (280, 176, 34, -5, "tooth"),
        (501, 154, 38, 5, "tooth"),
        (777, 154, 39, 0, "tooth"),
    )),
)

CONSTRUCTION_MODELS = (
    "broken_concrete", "cantilever", "skip_bin", "crane_truss", "switchgear", "drum_pallet",
)
# Left edge, front rim, width and depth of the contact plane in the Blender renders.
CONSTRUCTION_PROFILE = (85, 55, 550, 145)
DOCK_CONSTRUCTION_LEDGES = (0, 1, 2, 3, 5, 7, 11, 14, 18, 24, 29, 34)


def _object_ledge(ledge, key):
    prop = CLIMB_OBJECTS[key]
    width = min(ledge.width, prop.max_width)
    return replace(ledge, x=round(ledge.center - width / 2), width=width, slope=0,
                   model=key, solid_height=round(width * prop.height / prop.width))


def _add_objects(ledges, checkpoints):
    dock = {
        1: "weigh_house", 4: "oil_drum", 6: "guard_house", 8: "cable_reel", 13: "tire_stack",
        15: "site_office", 19: "traffic_barrier", 23: "vent_unit",
        26: "lift_cab", 27: "parcel_locker", 31: "pump_cabinet",
    }
    for index, key in dock.items():
        ledges[index] = _object_ledge(ledges[index], key)

    counts = Counter(dock.values())
    chapters = ((0, 2), (1, 5), (3, 4), (5, 5), (6, 1), (7, 1))
    for chapter, interval in chapters:
        start = checkpoints[chapter]
        first = 9 if chapter == 0 else 3
        for index in range(start + first, checkpoints[chapter + 1], interval):
            ledge = ledges[index]
            if ledge.model or ledge.piece:
                continue
            depth = ledge.underside(0)
            # In tight sections, fit the object to the existing foothold and leave room for the jump below.
            matches = []
            for key, spec in CLIMB_OBJECTS.items():
                shape = ledge.width * spec.height / (depth * spec.width)
                if 0.70 <= shape <= 1.54 and ledge.width <= spec.max_width * 1.45:
                    matches.append((counts[key], abs(1 - shape), key))
            if matches:
                key = min(matches)[2]
                ledges[index] = replace(ledge, model=key)
                counts[key] += 1


def _make_course():
    ledges = [Ledge(-140, BASE_Y, 724, 320, CHAPTERS[0].name, terrain="旧船坞"),
              Ledge(648, 430, 420, 249, material="basalt", terrain="防波堤"),
              Ledge(926, 299, 350, 280, terrain="湿滑废料斗", slick=True),
              Ledge(625, 166, 236, 109, material="wood", slope=-15, terrain="折返桁架")]
    checkpoints = [0]
    y = ledges[-1].y
    center = round(ledges[-1].center)
    for chapter, design in enumerate(CHAPTERS):
        rng = random.Random(1799 + chapter * 113)
        first = 3 if not chapter else 0
        for step in range(first, STEPS_PER_CHAPTER):
            stretch, within = divmod(step, 11)
            order = CHAPTER_STRETCHES[chapter]
            terrain, moves = STRETCHES[order[stretch % len(order)]]
            delta, rise, extra_width = moves[within % len(moves)]
            if (stretch + chapter) % 2:
                delta = -delta
            delta += rng.randrange(-12, 13)
            previous_width = ledges[-1].width
            clearance = 14
            distance = max(abs(delta), (previous_width + 96 + 1) // 2 + clearance)
            delta = distance if delta > 0 else -distance
            if not 180 <= center + delta <= 1080:
                delta = -delta
            center += delta
            y -= max(119, min(163, rng.randint(*design.rise) + rise))
            width = max(96, min(rng.randint(*design.width) + extra_width,
                                2 * (distance - clearance) - previous_width))
            entry = step == 0
            summit = chapter == len(CHAPTERS) - 1 and step == STEPS_PER_CHAPTER - 1
            if entry or summit:
                width = max(width, min(236, 2 * (distance - clearance) - previous_width))
            material = design.materials[(step + step // 5) % len(design.materials)]
            depth = (57 if material in {"steel", "scaffold", "wood"} else rng.randint(84, 167) + chapter * 2)
            if abs(delta) < 220:
                depth = min(depth, 76)
            slope = 0 if entry or summit else rng.randint(-design.slope, design.slope)
            exposed = bool(design.gust and not entry and not summit and (step + chapter * 3) % (22 - chapter) == 0)
            slick = not entry and not summit and (step + chapter * 5) % (17 - chapter // 3) == 7
            ledges.append(Ledge(center - width // 2, y, width, depth,
                                design.name if entry else "", material, slope, chapter, exposed, terrain, slick))
            if entry:
                checkpoints.append(len(ledges) - 1)
    for landmark in LANDMARKS:
        for index, (center, width, depth, slope, piece) in enumerate(landmark.parts, landmark.first):
            ledges[index] = replace(ledges[index], x=center - width // 2, width=width, depth=depth,
                                    slope=slope, landmark=landmark.name, piece=piece)
    for order, index in enumerate(DOCK_CONSTRUCTION_LEDGES):
        ledges[index] = replace(ledges[index], model=CONSTRUCTION_MODELS[order % len(CONSTRUCTION_MODELS)])
    for chapter in (6, 7):
        steps = (step for step in range(STEPS_PER_CHAPTER) if step % 11 < 3)
        for order, step in enumerate(steps):
            index = checkpoints[chapter] + step
            ledges[index] = replace(ledges[index], model=CONSTRUCTION_MODELS[
                (order + 2 * (chapter - 6)) % len(CONSTRUCTION_MODELS)])
    _add_objects(ledges, checkpoints)
    return tuple(ledges), tuple(checkpoints)


LEDGES, CHECKPOINTS = _make_course()
_ELEVATIONS = tuple(-ledge.y for ledge in LEDGES)
_CHAPTER_ELEVATIONS = tuple(_ELEVATIONS[index] for index in CHECKPOINTS)
MAX_SOLID_REACH = max(ledge.underside(0) + abs(ledge.slope) / 2 for ledge in LEDGES)


def chapter_at(y):
    return max(0, bisect_right(_CHAPTER_ELEVATIONS, -y) - 1)


def ledge_indices(top, bottom):
    """Indices whose nominal upper surface is within the world-space interval."""
    return range(bisect_left(_ELEVATIONS, -bottom), bisect_right(_ELEVATIONS, -top))


def obstacle_indices(top, bottom):
    """Also include solids whose roofs are above the interval and whose walls reach it."""
    return ledge_indices(top - MAX_SOLID_REACH, bottom)
