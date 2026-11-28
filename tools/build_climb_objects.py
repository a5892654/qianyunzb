"""Model and render reusable climbable buildings and objects in Blender."""

from math import cos, radians, sin
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_climb_construction import ROOT, box, camera, cylinder, light, member, mesh, prism, render, setup, surface
from climb_objects import (CLIMB_OBJECTS, FRONT_FACE, PIXELS_PER_METER, PLATE_HEIGHT, PLATE_WIDTH,
                           PROJECTION_TILT, RIM_ROW)


ASSETS = ROOT / "web" / "assets" / "climb" / "terrain" / "objects"
PREVIEWS = ROOT / "outputs" / "climb" / "objects"


def palette():
    return {
        "brick": surface("Brine-stained red brick", ((0.25, "59403b"), (0.49, "985d47"), (0.77, "b88167")),
                         scale=11, relief=0.016),
        "mortar": surface("Aged lime mortar", ((0.23, "777267"), (0.71, "b5aa93")), scale=18),
        "tile": surface("Faded terracotta roof tile", ((0.24, "623932"), (0.52, "96564a"), (0.75, "ba7459")),
                        scale=12, relief=0.015),
        "cream": surface("Dusty ivory enamel", ((0.25, "777b70"), (0.55, "b7b4a1"), (0.73, "dad2b6")),
                         scale=5.6, stretch=(1.5, 1, 0.62)),
        "blue": surface("Faded dock blue enamel", ((0.26, "55473d"), (0.46, "426774"), (0.74, "72939d")),
                        scale=5.6, stretch=(1.65, 1, 0.55), metal=0.23),
        "teal": surface("Chipped petroleum-green steel", ((0.27, "6f4940"), (0.44, "3c706e"), (0.72, "63968a")),
                        scale=5.4, stretch=(1.9, 1, 0.58), metal=0.28),
        "red": surface("Oxide red enamel", ((0.28, "623b32"), (0.46, "a84d40"), (0.71, "c27155")),
                       scale=7, stretch=(1.6, 1, 0.46), metal=0.28),
        "yellow": surface("Ochre paint worn at the edges", ((0.24, "765334"), (0.45, "b5873f"), (0.77, "dfb955")),
                          scale=5.3, stretch=(1.36, 1, 0.68), metal=0.23),
        "iron": surface("Oxidized black steel", ((0.26, "292c2a"), (0.71, "58574f")), scale=8, metal=0.40),
        "zinc": surface("Dull galvanized steel", ((0.23, "575e5e"), (0.74, "a0a699")), scale=8, metal=0.53),
        "wood": surface("Stained packing-board wood", ((0.28, "604a37"), (0.73, "af9167")),
                        scale=7.7, stretch=(2.2, 1, 0.19)),
        "endwood": surface("Weathered timber ends", ((0.28, "766043"), (0.70, "b3976a")), scale=12),
        "rubber": surface("Chalky old rubber", ((0.27, "292b2c"), (0.75, "565958")), scale=16, relief=0.012),
        "glass": surface("Dusty blue window glass", ((0.24, "29414e"), (0.72, "526b75")),
                         scale=5, roughness=0.23, relief=0.003),
        "reflection": surface("Pale reflection on dusty windows", ((0.28, "688088"), (0.74, "93a9ac")),
                              roughness=0.44, relief=0.002),
        "concrete": surface("Chalky concrete with grit", ((0.24, "6a6c65"), (0.53, "9c9a8b"), (0.76, "c6c0ab")),
                            scale=12, relief=0.033),
        "plastic": surface("Stained translucent polyethylene", ((0.34, "b4af87"), (0.72, "d9d8b6")),
                           scale=4, relief=0.008),
    }


def panel(c, name, x, z, w, h, mat, *, front=FRONT_FACE, thickness=0.035, bevel=0.014):
    return box(c, name, (x, front - thickness / 2, z), (w, thickness, h), mat, bevel)


def shell(c, name, width, height, depth, mat, *, front=FRONT_FACE, bevel=0.034):
    return box(c, name, (0, front + depth / 2, -height / 2), (width, depth, height), mat, bevel)


def fastener(c, x, z, mat, *, y=FRONT_FACE - 0.063, radius=0.033):
    cylinder(c, "Hex-head fastener", (x, y, z), radius, 0.041, mat, 8, "Y")


def window(c, x, z, w, h, m):
    panel(c, "Recessed window frame", x, z, w, h, m["iron"], thickness=0.077)
    panel(c, "Blue glass", x, z, w - 0.11, h - 0.12, m["glass"], front=FRONT_FACE - 0.080)
    panel(c, "Window mullion", x, z, 0.058, h - 0.08, m["cream"], front=FRONT_FACE - 0.119)
    panel(c, "Sash bar", x, z, w - 0.095, 0.045, m["zinc"], front=FRONT_FACE - 0.119)
    y = FRONT_FACE - 0.119
    for start in (0.14, 0.22):
        mesh(c, "Window sky reflection",
             [(x + w * u, y, z + h * v) for u, v in ((-start, 0.41), (-start + 0.07, 0.41),
                                                    (-start + 0.26, -0.41), (-start + 0.20, -0.41))],
             [(3, 2, 1, 0)], m["reflection"])


def old_roof(c, w, depth, m, rows):
    ridge = FRONT_FACE + depth * 0.56
    eave = -0.48
    for start, end, start_z, end_z in ((FRONT_FACE - 0.08, ridge, eave, -0.035),
                                      (ridge, FRONT_FACE + depth + 0.08, -0.035, eave)):
        verts = [(x, y, z) for x in (-w / 2, w / 2)
                 for y, z in ((start, start_z), (end, end_z), (end, end_z - 0.12), (start, start_z - 0.12))]
        mesh(c, "Pitched terracotta roof", verts,
             [(0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0), (0, 1, 2, 3), (7, 6, 5, 4)],
             m["tile"])
        for i in range(1, rows):
            t = i / rows
            y, z = start + (end - start) * t, start_z + (end_z - start_z) * t
            member(c, "Overlapping roof tile course", (-w / 2 + 0.03, y, z + 0.012),
                   (w / 2 - 0.03, y, z + 0.012), 0.042, 0.025, m["tile"])
    member(c, "Continuous terracotta ridge", (-w / 2 + 0.014, ridge, -0.055),
           (w / 2 - 0.014, ridge, -0.055), 0.15, 0.11, m["tile"])
    y = FRONT_FACE - 0.063
    panel(c, "Weathered eave fascia", 0, -0.53, w, 0.085, m["wood"], front=y)


def masonry_building(c, m, spec, *, wide):
    w, h = spec.width, spec.height
    depth = 2.35 if wide else 1.83
    box(c, "Mortar wall with concrete footing", (0, FRONT_FACE + depth / 2, -(h + 0.47) / 2),
        (w - 0.16, depth, h - 0.47), m["mortar"])
    old_roof(c, w, depth, m, 8 if wide else 6)
    panel(c, "Concrete foundation", 0, -h + 0.10, w - 0.16, 0.20, m["concrete"], thickness=0.073)
    hw = w / 2 - 0.11
    # Build the brick face as one mesh with staggered joints in real geometry.
    verts, faces = [], []
    for row in range(11):
        z = -0.58 - row * (h - 0.83) / 11
        for i in range(int(w / 0.35) + 2):
            x0 = -hw + i * 0.35 - 0.175 * (row % 2)
            a, b = max(-hw, x0), min(hw, x0 + 0.331)
            if b - a < 0.018:
                continue
            y = FRONT_FACE - 0.019 - ((row * 7 + i * 11) % 5) * 0.005
            j = len(verts)
            verts.extend(((a, y, z - 0.15), (b, y, z - 0.15), (b, y, z), (a, y, z)))
            faces.append((j, j + 1, j + 2, j + 3))
    mesh(c, "Individual bricks and exposed mortar joints", verts, faces, m["brick"])
    door_x = -1.26 if wide else -0.89
    panel(c, "Recessed doorway", door_x, -h + 1.06, 0.90, 1.82, m["iron"], thickness=0.075)
    panel(c, "Faded blue entry door", door_x, -h + 1.05, 0.78, 1.67, m["blue"], front=FRONT_FACE - 0.08)
    panel(c, "Entry window", door_x, -h + 1.46, 0.43, 0.40, m["glass"], front=FRONT_FACE - 0.117)
    fastener(c, door_x + 0.28, -h + 0.96, m["zinc"], y=FRONT_FACE - 0.145)
    for x in ((-2.49, 0.48, 2.04) if wide else (0.54,)):
        window(c, x, -1.45, 1.16 if wide else 1.24, 0.95, m)
        panel(c, "Cast concrete sill", x, -1.98, 1.22, 0.094, m["concrete"], thickness=0.122)
    for x in (-hw + 0.15, hw - 0.15):
        member(c, "Vertical gutter downspout", (x, FRONT_FACE - 0.105, -0.56),
               (x, FRONT_FACE - 0.105, -h + 0.11), 0.073, 0.055, m["zinc"])


def site_office(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Portable corrugated office", w - 0.11, h - 0.10, 2.12, m["cream"])
    box(c, "Folded galvanized roof cap", (0, 0.07, -0.075), (w, 2.22, 0.15), m["blue"])
    panel(c, "Blue foundation rail", 0, -h + 0.075, w, 0.15, m["blue"], thickness=0.069)
    for i in range(24):
        x = (i - 11.5) * 0.185
        panel(c, "Corrugated siding peak", x, -h * 0.51, 0.067, h - 0.28, m["cream"], thickness=0.034)
    for x in (-1.26, 0.05):
        window(c, x, -1.34, 1.08, 0.88, m)
    panel(c, "Entry door casing", 1.51, -h + 0.99, 0.94, 1.84, m["blue"], thickness=0.066)
    panel(c, "Office door", 1.51, -h + 0.98, 0.80, 1.73, m["cream"], front=FRONT_FACE - 0.076)
    fastener(c, 1.77, -h + 0.93, m["iron"], y=FRONT_FACE - 0.129)
    panel(c, "Small aluminum door visor", 1.51, -1.08, 1.16, 0.105, m["zinc"], thickness=0.24)


def oil_drum(c, m, spec):
    r, h = spec.width / 2, spec.height
    y = FRONT_FACE + r
    cylinder(c, "Single sealed red oil drum", (0, y, -h / 2), r - 0.025, h - 0.05, m["red"], 48)
    cylinder(c, "Faded cream identification band", (0, y, -h * 0.49), r - 0.021, 0.32, m["cream"], 48)
    for z in (-h + 0.059, -h + 0.39, -0.43, -0.045):
        cylinder(c, "Pressed drum rim", (0, y, z), r, 0.078, m["red"], 48)
    cylinder(c, "Recessed steel drum lid", (0, y, -0.016), r - 0.061, 0.032, m["red"], 48)
    for x, radius in ((-0.29, 0.080), (0.31, 0.046)):
        cylinder(c, "Threaded bung plug", (x, y - 0.14, -0.003), radius, 0.018, m["iron"], 12)


def cargo_container(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Closed marine freight container", w, h, 2.12, m["teal"], front=FRONT_FACE + 0.056)
    for i in range(15):
        panel(c, "Pressed vertical steel corrugation", (i - 7) * 0.30, -h / 2, 0.10, h - 0.15, m["teal"],
              thickness=0.058)
    for z in (-0.093, -h + 0.098):
        panel(c, "Welded container rail", 0, z, w, 0.185, m["teal"], thickness=0.11)
    for x in (-w / 2 + 0.11, w / 2 - 0.11):
        panel(c, "Corner casting post", x, -h / 2, 0.22, h - 0.045, m["iron"], thickness=0.088)
        for z in (-0.19, -h + 0.19):
            fastener(c, x, z, m["zinc"], radius=0.054)
    for i, size in enumerate((0.34, 0.19, 0.23)):
        panel(c, "Faded cargo stencil", -1.32 + i * 0.32, -0.41, size, 0.054, m["cream"])
    for i in range(4):
        box(c, "Roof ribs", (0, -0.69 + i * 0.42, -0.018), (w - 0.33, 0.085, 0.033), m["teal"])


def shipping_crate(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Closed wooden shipping crate", w, h, 1.46, m["wood"], front=FRONT_FACE + 0.06)
    for i in range(7):
        stride = w / 7
        panel(c, "Plywood packing plank", (i - 3) * stride, -h / 2, stride - 0.02, h - 0.045, m["wood"])
    for z in (-0.105, -h + 0.12):
        panel(c, "Endgrain packing rail", 0, z, w - 0.035, 0.21, m["endwood"], thickness=0.082)
        for x in (-0.82, -0.56, 0.56, 0.82):
            fastener(c, x, z, m["iron"], y=FRONT_FACE - 0.105, radius=0.023)
    for x in (-0.76, 0.76):
        panel(c, "Steel packing strap", x, -h / 2, 0.082, h - 0.017, m["iron"], thickness=0.095)
    member(c, "Diagonal timber brace", (-0.66, FRONT_FACE - 0.07, -h + 0.26),
           (0.62, FRONT_FACE - 0.07, -0.22), 0.15, 0.10, m["endwood"])


def cable_reel(c, m, spec):
    r, h = spec.width / 2, spec.height
    y = FRONT_FACE + r
    for z in (-0.10, -h + 0.10):
        cylinder(c, "Solid timber reel flange", (0, y, z), r, 0.20, m["endwood"], 48)
    cylinder(c, "Wound black cable", (0, y, -h / 2), r - 0.13, h - 0.33, m["rubber"], 48)
    for i in range(16):
        cylinder(c, "Individual cable turn", (0, y, -0.23 - i * 0.087), r - 0.112, 0.048, m["iron"], 48)
    cylinder(c, "Recessed cable hub", (0, y, -0.006), 0.27, 0.024, m["iron"], 24)
    cylinder(c, "Zinc spindle end", (0, y, 0.0), 0.13, 0.021, m["zinc"], 20)
    for x in (-0.63, 0.58):
        box(c, "Timber flange joint", (x, y, -0.006), (0.030, 1.02, 0.014), m["wood"], 0.004)


def water_tank(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Polyethylene intermediate bulk container", w - 0.09, h - 0.10, 1.53, m["plastic"],
          front=FRONT_FACE + 0.035, bevel=0.12)
    for i in range(6):
        x = (i - 2.5) * (w - 0.08) / 5
        panel(c, "Galvanized cage upright", x, -h / 2, 0.052, h - 0.016, m["zinc"])
        box(c, "Galvanized roof guard", (x, -0.244, -0.023), (0.049, 1.53, 0.047), m["zinc"])
    for z in (-0.095, -0.48, -0.90, -1.32, -h + 0.10):
        panel(c, "Galvanized cage crossbar", 0, z, w, 0.049, m["zinc"], thickness=0.068)
    panel(c, "Pallet base", 0, -h + 0.057, w, 0.11, m["iron"])
    cylinder(c, "Red filler cap", (0.36, 0.19, -0.012), 0.175, 0.071, m["red"], 16)
    cylinder(c, "Black drain valve", (0, FRONT_FACE - 0.07, -h + 0.24), 0.11, 0.125, m["iron"], 16, "Y")


def generator(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Weatherproof diesel generator", w, h, 1.52, m["yellow"], bevel=0.08)
    panel(c, "Black powder-coated skid", 0, -h + 0.11, w - 0.015, 0.22, m["iron"], thickness=0.10)
    panel(c, "Gasketed control panel", -0.76, -0.89, 0.99, 0.97, m["iron"], thickness=0.064)
    panel(c, "Control meter screen", -0.76, -0.65, 0.46, 0.185, m["glass"], front=FRONT_FACE - 0.072)
    for i in range(3):
        fastener(c, -1 + 0.23 * i, -1.0, (m["red"], m["cream"], m["teal"])[i],
                 y=FRONT_FACE - 0.105, radius=0.061)
    panel(c, "Maintenance door trim", 0.54, -0.98, 1.38, 1.57, m["iron"])
    panel(c, "Yellow maintenance door", 0.54, -0.98, 1.30, 1.48, m["yellow"], front=FRONT_FACE - 0.037)
    for i in range(7):
        panel(c, "Engine air louvre", 0.55, -0.53 - i * 0.126, 0.84, 0.064, m["iron"],
              front=FRONT_FACE - 0.075)


def vent_unit(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Twin fan ventilation housing", w, h, 1.32, m["cream"])
    for x in (-0.66, 0.66):
        y = FRONT_FACE - 0.041
        cylinder(c, "Shaded fan recess", (x, y, -h * 0.50), 0.515, 0.065, m["iron"], 32, "Y")
        for i in range(6):
            theta = radians(i * 60)
            points = [(x + cos(theta + a) * r, y - 0.037, -h * 0.50 + sin(theta + a) * r)
                      for r, a in ((0.13, 0), (0.45, 0.32), (0.45, 0.82), (0.15, 0.34))]
            mesh(c, "Stamped fan blade", points, [(0, 1, 2, 3)], m["zinc"])
        cylinder(c, "Fan axle", (x, y - 0.056, -h * 0.50), 0.115, 0.06, m["iron"], 16, "Y")
        for z in (-0.55, -0.78, -1.01):
            panel(c, "Galvanized fan guard", x, z, 0.88, 0.023, m["zinc"], front=y - 0.085)
    for x in (-1.21, 1.21):
        for z in (-0.14, -h + 0.14):
            fastener(c, x, z, m["zinc"])


def tire_stack(c, m, spec):
    r, h = spec.width / 2, spec.height
    y = FRONT_FACE + r
    for i in range(3):
        z = -(i + 0.5) * h / 3
        cylinder(c, "Stacked rubber tire carcass", (0, y, z), r - 0.030, h / 3 - 0.035, m["rubber"], 36)
        for dz in (-0.14, 0.14):
            cylinder(c, "Tire sidewall lip", (0, y, z + dz), r, 0.072, m["rubber"], 36)
        for k in range(19):
            theta = radians(192 + k * 8.7)
            item = box(c, "Individual tire tread", (cos(theta) * (r - 0.025), y + sin(theta) * (r - 0.025), z),
                       (0.046, 0.095, 0.24), m["iron"], 0.008)
            item.rotation_euler.z = theta
    cylinder(c, "Weathered steel spare wheel rim", (0, y, -0.035), r * 0.61, 0.068, m["zinc"], 32)
    cylinder(c, "Shadowed wheel hub", (0, y, -0.014), r * 0.25, 0.026, m["iron"], 20)


def tool_chest(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Rolling tool cabinet with solid top", w, h, 1.29, m["red"])
    panel(c, "Recessed drawer surround", 0, -1.10, w - 0.16, 1.79, m["iron"], thickness=0.049)
    for i in range(6):
        z = -0.39 - i * 0.273
        panel(c, "Individual enamel drawer", 0, z, w - 0.24, 0.252, m["red"], front=FRONT_FACE - 0.054)
        panel(c, "Full-width zinc drawer pull", 0, z + 0.044, w - 0.42, 0.035, m["zinc"],
              front=FRONT_FACE - 0.096)
    for x in (-0.66, 0.66):
        cylinder(c, "Recessed black cabinet caster", (x, FRONT_FACE - 0.042, -h + 0.107),
                 0.102, 0.084, m["rubber"], 18, "Y")


def traffic_barrier(c, m, spec):
    w, h = spec.width, spec.height
    corners = [(-w / 2, -h), (w / 2, -h), (w / 2, -0.20), (w / 2 - 0.16, 0.0),
               (-w / 2 + 0.16, 0.0), (-w / 2, -0.20)]
    prism(c, "Precast concrete road divider", corners, FRONT_FACE, 0.18, m["concrete"], 0.067)
    panel(c, "Old yellow visibility band", 0, -0.57, w - 0.19, 0.45, m["yellow"])
    for i in range(7):
        x = (i - 3) * 0.43
        y = FRONT_FACE - 0.044
        mesh(c, "Worn black diagonal paint", [(x + dx, y, z) for dx, z in
             ((-0.16, -0.78), (0.037, -0.78), (0.22, -0.36), (0.02, -0.36))], [(0, 1, 2, 3)], m["iron"])
    for x in (-1.05, 1.05):
        panel(c, "Recessed forklift slot", x, -h + 0.13, 0.37, 0.16, m["iron"], thickness=0.014)


def parcel_locker(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Salvaged parcel locker", w, h, 1.32, m["teal"], bevel=0.042)
    panel(c, "Dark cabinet seams", 0, -h / 2, w - 0.14, h - 0.16, m["iron"])
    for row in range(4):
        for col in range(2):
            x, z = (col - 0.5) * 0.99, -0.38 - row * 0.57
            paint = m["blue"] if (row + col) % 3 == 0 else m["teal"]
            panel(c, "Individual parcel compartment", x, z, 0.958, 0.536, paint, front=FRONT_FACE - 0.041)
            panel(c, "Slot for paper label", x - 0.17, z + 0.12, 0.27, 0.085, m["zinc"], front=FRONT_FACE - 0.078)
            fastener(c, x + 0.33, z, m["iron"], y=FRONT_FACE - 0.097)


def pump_cabinet(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Enclosed quarry pump assembly", w, h, 1.44, m["blue"])
    panel(c, "Pump plinth", 0, -h + 0.12, w, 0.23, m["iron"])
    panel(c, "Inspection cover", 0.46, -0.94, 1.42, 1.46, m["zinc"], thickness=0.06)
    panel(c, "Gasketed painted service panel", 0.46, -0.94, 1.33, 1.35, m["blue"], front=FRONT_FACE - 0.065)
    for x in (0.13, 0.71):
        cylinder(c, "Black pressure gauge", (x, FRONT_FACE - 0.143, -0.63), 0.179, 0.112, m["iron"], 24, "Y")
        cylinder(c, "Ivory pressure gauge face", (x, FRONT_FACE - 0.202, -0.63), 0.14, 0.012, m["cream"], 24, "Y")
        member(c, "Gauge needle", (x, FRONT_FACE - 0.218, -0.63), (x + 0.076, FRONT_FACE - 0.218, -0.57),
               0.013, 0.013, m["red"])
    for z in (-0.75, -1.31):
        cylinder(c, "Gasketed pipe union", (-0.83, FRONT_FACE - 0.112, z), 0.22, 0.185, m["iron"], 16, "Y")
        cylinder(c, "Flanged steel cap", (-0.83, FRONT_FACE - 0.214, z), 0.166, 0.062, m["zinc"], 16, "Y")


def lift_cab(c, m, spec):
    w, h = spec.width, spec.height
    shell(c, "Closed mesh-backed lift car", w, h, 1.73, m["iron"])
    panel(c, "Ochre car base", 0, -h + 0.22, w, 0.44, m["yellow"], thickness=0.083)
    panel(c, "Lift canopy", 0, -0.12, w, 0.23, m["yellow"], thickness=0.083)
    panel(c, "Lift rear enclosure", 0, -1.27, w - 0.29, 2.04, m["glass"])
    for x in (-w / 2 + 0.071, 0, w / 2 - 0.071):
        panel(c, "Lift door frame", x, -h * 0.49, 0.14, h - 0.21, m["yellow"], thickness=0.091)
    for x in (-0.58, 0.58):
        for i in range(4):
            panel(c, "Lift door safety bars", x + (i - 1.5) * 0.207, -1.3, 0.045, 2.01, m["zinc"],
                  front=FRONT_FACE - 0.06)
        member(c, "Lift door diagonal brace", (x - 0.44, FRONT_FACE - 0.076, -2.28),
               (x + 0.44, FRONT_FACE - 0.076, -0.29), 0.071, 0.058, m["yellow"])
    panel(c, "Door operating handle", 0.19, -1.46, 0.11, 0.26, m["iron"], front=FRONT_FACE - 0.084)


def build():
    ASSETS.mkdir(parents=True, exist_ok=True)
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    m = palette()
    builders = {
        "oil_drum": oil_drum,
        "guard_house": lambda c, m, spec: masonry_building(c, m, spec, wide=False),
        "site_office": site_office,
        "weigh_house": lambda c, m, spec: masonry_building(c, m, spec, wide=True),
        "lift_cab": lift_cab,
        "cargo_container": cargo_container,
        "shipping_crate": shipping_crate,
        "cable_reel": cable_reel,
        "water_tank": water_tank,
        "generator": generator,
        "vent_unit": vent_unit,
        "tire_stack": tire_stack,
        "tool_chest": tool_chest,
        "traffic_barrier": traffic_barrier,
        "parcel_locker": parcel_locker,
        "pump_cabinet": pump_cabinet,
    }
    studio = bpy.context.scene
    studio.name = "Climbable Buildings and Objects"
    plates = bpy.data.scenes.new("Game Sprite Projection")
    setup(plates, PLATE_WIDTH, PLATE_HEIGHT, transparent=True)
    tilt = radians(PROJECTION_TILT)
    up = Vector((0, sin(tilt), cos(tilt)))
    rim_height = (PLATE_HEIGHT / 2 - RIM_ROW) / PIXELS_PER_METER
    target = Vector((0, FRONT_FACE, 0)) - rim_height * up
    camera(plates, "Game orthographic camera", target,
           target + Vector((0, -18 * cos(tilt), 18 * sin(tilt))), PLATE_WIDTH / PIXELS_PER_METER)
    light(plates, "Sunlight", "SUN", (-4, -7, 9), (0, 0, -1), 2.01, (1, 0.91, 0.81), 0.20)
    light(plates, "Sky bounce", "SUN", (5, -5, 6), (0, 0, -1), 1.01, (0.67, 0.83, 1), 0.27)

    models = {}
    for key, builder in builders.items():
        spec = CLIMB_OBJECTS[key]
        collection = bpy.data.collections.new(spec.label)
        collection.asset_mark()
        collection["game_asset"] = key
        collection["contact_plane"] = f"Width {spec.width:g}m, front y={FRONT_FACE:g}m, roof or lid z=0m"
        builder(collection, m, spec)
        models[key] = collection
        plates.collection.children.link(collection)
        render(plates, ASSETS / f"{key}.webp")
        plates.collection.children.unlink(collection)
    plates.collection.children.link(models["oil_drum"])

    setup(studio, 1600, 1150)
    world = next(node for node in studio.world.node_tree.nodes if node.type == "BACKGROUND")
    world.inputs["Color"].default_value = (0.14, 0.19, 0.24, 1)
    order = ("weigh_house", "guard_house", "site_office", "lift_cab",
             "cargo_container", "generator", "traffic_barrier", "pump_cabinet",
             "oil_drum", "shipping_crate", "cable_reel", "water_tank",
             "vent_unit", "tire_stack", "tool_chest", "parcel_locker")
    for i, key in enumerate(order):
        spec = CLIMB_OBJECTS[key]
        collection = models[key]
        instance = bpy.data.objects.new(spec.label, None)
        studio.collection.objects.link(instance)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        scale = min(4.65 / spec.width, 3.1 / spec.height, 1.62)
        instance.scale = (scale, scale, scale)
        instance.location = ((i % 4 - 1.5) * 5.75, 0, (1.5 - i // 4) * 4.08 + spec.height * scale / 2)
    camera(studio, "Object kit overview", (0, 0, 0), (6.2, -41, 13.0), 24.6)
    light(studio, "Catalog key", "SUN", (-8, -7, 9), (0, 0, 0), 2.28, (1, 0.92, 0.84), 0.18)
    light(studio, "Catalog fill", "SUN", (6, -7, 6), (0, 0, 0), 1.16, (0.66, 0.82, 1), 0.30)
    studio.render.image_settings.file_format = "PNG"
    render(studio, PREVIEWS / "objects-kit.png")
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.color_type = "MATERIAL"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "docs" / "cliff-objects.blend"), compress=True)


if __name__ == "__main__":
    build()
