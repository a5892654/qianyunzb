"""Swept suction-cup contacts against the terrain used by the climber."""

from dataclasses import dataclass
import math

from climb_course import LEDGES, Ledge, ledge_indices


CUP_RADIUS = 6


@dataclass(frozen=True, slots=True)
class Contact:
    x: float
    y: float
    nx: float
    ny: float
    ledge: Ledge
    time: float

    @property
    def point(self):
        return self.x, self.y


def _ledge_hit(x0, y0, dx, dy, ledge):
    # Each shelf is an extruded sloped rim. Clip against its four outward planes.
    grade = ledge.slope / ledge.width
    rim = ledge.y - ledge.center * grade
    scale = math.hypot(grade, 1)
    top = CUP_RADIUS * scale
    planes = ((-1, 0, -ledge.x + CUP_RADIUS), (1, 0, ledge.right + CUP_RADIUS),
              (grade, -1, -rim + top), (-grade, 1, ledge.underside(rim) + top))
    enter, leave = 0.0, 1.0
    normal = None
    nearest = (float("-inf"), 0, 0)
    for nx, ny, boundary in planes:
        distance = nx * x0 + ny * y0 - boundary
        travel = nx * dx + ny * dy
        if distance > nearest[0]:
            nearest = (distance, nx, ny)
        if abs(travel) < 1e-8:
            if distance > 0:
                return None
            continue
        time = -distance / travel
        if travel < 0 and time >= enter:
            enter, normal = time, (nx, ny)
        elif travel > 0:
            leave = min(leave, time)
        if enter > leave:
            return None
    if normal is None:
        distance, nx, ny = nearest
        if distance < 0 and nx * dx + ny * dy < -1e-7:
            normal = nx, ny
        else:
            return None
    nx, ny = normal
    scale = math.hypot(nx, ny)
    return Contact(x0 + dx * enter + nx * 0.03, y0 + dy * enter + ny * 0.03,
                   nx / scale, ny / scale, ledge, enter)


def cup_contact(start, end, *, extra=None, nearby=None):
    """Find the first obstruction between two cup positions in world space."""
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    if nearby is None:
        close = ledge_indices(min(y0, y1) - 137, max(y0, y1) + 29)
        nearby = (LEDGES[index] for index in close)
    low_x, high_x = sorted((x0, x1))
    low_y, high_y = sorted((y0, y1))
    nearest = None
    for ledge in nearby:
        if (high_x < ledge.x - CUP_RADIUS or low_x > ledge.right + CUP_RADIUS
                or high_y < ledge.y - 24 or low_y > ledge.underside(ledge.y) + 24):
            continue
        hit = _ledge_hit(x0, y0, dx, dy, ledge)
        if hit and (nearest is None or hit.time < nearest.time):
            nearest = hit
    if extra is not None:
        hit = _ledge_hit(x0, y0, dx, dy, extra)
        if hit and (nearest is None or hit.time < nearest.time):
            nearest = hit
    return nearest


def carry_blend(flight_time):
    fraction = min(1.0, flight_time / 0.30)
    return fraction * fraction * (3 - 2 * fraction)


def carry_tip(grip, facing, from_angle, length, pose, flight_time):
    park_angle, extension = pose
    turn = math.remainder(park_angle - from_angle, 2 * math.pi)
    folding = min(1.0, flight_time / 0.092)
    angle = from_angle + turn * carry_blend(max(0.0, flight_time - 0.088))
    length *= 1 + (extension - 1) * folding
    return (grip[0] + facing * length * math.cos(angle), grip[1] + length * math.sin(angle))
