"""Shared side-view architecture for the game renderer and Blender course."""

from functools import lru_cache
import math

from climb_course import LEDGES


class Masonry:
    def __init__(self):
        self.faces = []

    def face(self, color, *points):
        self.faces.append((color, points))

    def beam(self, color, a, b, width):
        ax, ay = a
        bx, by = b
        scale = width / (2 * math.hypot(bx - ax, by - ay))
        dx, dy = (by - ay) * scale, (ax - bx) * scale
        self.face(color, (ax + dx, ay + dy), (bx + dx, by + dy), (bx - dx, by - dy), (ax - dx, ay - dy))

    def circle(self, color, x, y, radius, rim=0, sides=40):
        points = [(math.cos(step * math.tau / sides), math.sin(step * math.tau / sides))
                  for step in range(sides)]
        outer = [(x + dx * radius, y + dy * radius) for dx, dy in points]
        if not rim:
            self.face(color, *outer)
            return
        inner = [(x + dx * (radius - rim), y + dy * (radius - rim)) for dx, dy in points]
        for step in range(sides):
            nxt = (step + 1) % sides
            self.face(color, outer[step], outer[nxt], inner[nxt], inner[step])


def ledge_outline(ledge, height=None):
    """Use the top face and underside shared by the basin and suction cup."""
    x, right = ledge.x, ledge.right
    top, far = ledge.surface(x), ledge.surface(right)
    bottom = ledge.underside if height is None else lambda y: y + height
    return (x, top), (right, far), (right, bottom(far)), (x, bottom(top))


def _wreck(art, ledges):
    top, base = ledges[-1].y - 20, ledges[0].y + 90
    keel = top + 93
    hull = ((229, keel + 72), (439, top + 28), (727, top + 29), (947, keel + 102),
            (887, keel + 218), (683, keel + 286), (423, keel + 268), (257, keel + 190))
    edge, wood, brass = (56, 80, 80), (106, 107, 89), (176, 140, 91)
    art.face(edge, *hull)
    art.face(wood, (260, keel + 84), (447, top + 44), (725, top + 47), (918, keel + 113),
             (867, keel + 204), (678, keel + 268), (425, keel + 247), (279, keel + 176))
    for row in range(6):
        y = keel + 80 + row * 25
        art.beam((83, 90, 78), (271 + row * 5, y), (562, y + 18), 2)
        art.beam((83, 90, 78), (562, y + 18), (895 - row * 10, y + 26), 2)
    art.beam(brass, (389, top + 18), (327, top - 172), 8)
    art.beam(brass, (774, top + 28), (874, top - 172), 8)
    for x in range(320, 881, 88):
        y = keel + 157 + 64 * math.sin((x - 300) / 580 * math.pi)
        art.circle(edge, x, y, 17, sides=16)
        art.circle(brass, x, y, 16, rim=4, sides=16)
    for x, end in ((449, 382), (712, 803)):
        joint = (x, keel + 165)
        foot = (end, base - (65 if x < 600 else 0))
        art.beam(edge, joint, foot, 26)
        art.beam(brass, joint, foot, 5)
        art.beam(edge, (x - 124, keel + 322), (x + 120, keel + 302), 16)
        art.face((129, 122, 100), (x - 114, keel + 324), (x + 111, keel + 309),
                 (x + 82, keel + 444), (x + 17, keel + 423), (x - 53, keel + 457))
    for ledge in ledges:
        x, y = ledge.center, ledge.y + 12
        pivot = 449 if x < 560 else 712
        if abs(x - pivot) > 16:
            art.beam(edge, (x, y), (pivot, y), 10)
            art.beam(brass, (x, y), (pivot, y + 38), 5)


def _clock(art, ledges):
    x, y = 517, ledges[len(ledges) // 2].y + 20
    slate, iron, brass = (58, 71, 78), (91, 90, 80), (189, 153, 101)
    art.face(slate, (295, y + 427), (311, y - 234), (517, y - 365), (724, y - 234), (743, y + 427))
    for cx, cy, r in ((772, y + 121, 120), (320, y + 268, 93), (x, y, 241)):
        art.circle(slate, cx, cy, r)
        art.circle(brass, cx, cy, r, rim=9)
        for step in range(20):
            a = step * math.tau / 20
            edge = cx + math.cos(a) * (r - 5), cy + math.sin(a) * (r - 5)
            tooth = cx + math.cos(a) * (r + 12), cy + math.sin(a) * (r + 12)
            art.beam(iron, edge, tooth, 12)
    art.circle((170, 157, 124), x, y, 219)
    art.circle((221, 192, 140), x, y, 195, rim=3)
    for step in range(12):
        a = step * math.tau / 12
        start = x + 176 * math.cos(a), y + 176 * math.sin(a)
        end = x + 192 * math.cos(a), y + 192 * math.sin(a)
        art.beam(slate, start, end, 6)
    art.beam(slate, (x, y), (x - 44, y - 146), 13)
    art.beam(slate, (x, y), (x + 128, y + 52), 10)
    art.circle(brass, x, y, 16)
    crack = ((x + 189, y - 111), (x + 110, y - 75), (x + 78, y - 91), (x + 17, y - 28), (x + 39, y + 8))
    for a, b in zip(crack, crack[1:]):
        art.beam(iron, a, b, 4)
    for ledge in ledges:
        bx, by = ledge.center, ledge.y + 18
        if abs(bx - x) > 72:
            off = 146 if bx > x else -146
            art.beam(iron, (x + off, by + 45), (bx, by), 7)


@lru_cache(maxsize=8)
def architecture_faces(landmark):
    art = Masonry()
    painter = {"wreck": _wreck, "clock": _clock}[landmark.kind]
    painter(art, LEDGES[landmark.first:landmark.last + 1])
    return tuple(art.faces)


@lru_cache(maxsize=32)
def ledge_faces(ledge):
    base, edge, seam = {
        "plank": ((94, 81, 69), (210, 174, 124), (145, 117, 85)),
        "tooth": ((117, 87, 60), (232, 187, 125), (188, 147, 96)),
    }[ledge.piece]
    art = Masonry()
    art.face(base, *ledge_outline(ledge))
    art.face(edge, *ledge_outline(ledge, 5))
    for x in range(ledge.x + 11, ledge.right - 8, 18):
        y = ledge.surface(x)
        if ledge.piece == "tile":
            art.beam(seam, (x + 4, y + 7), (x, y + 20), 2)
            art.beam(seam, (x, y + 12), (x + 12, y + 15), 1.5)
        else:
            art.circle(seam, x, y + 12, 2.6, sides=8)
    return tuple(art.faces)
