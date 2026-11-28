"""Build editable Blender scenery and export lightweight world-space triangle meshes."""

from math import cos, exp, pi, sin
from pathlib import Path
import random
import sys

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_climb_construction import ROOT, box, camera, color, cylinder, light, member, mesh
from climb_course import LEDGES, altitude_at, scenery_height


ASSETS = ROOT / "web" / "assets" / "climb" / "terrain" / "distant"


def palette(*hex_colors):
    materials = []
    for key in hex_colors:
        name = f"Scenery {key.upper()}"
        item = bpy.data.materials.get(name)
        if item is None:
            item = bpy.data.materials.new(name)
            item.diffuse_color = color(key)
            item["srgb"] = list(bytes.fromhex(key))
        materials.append(item)
    return materials


def terrain(c, name, seed, span, near, depth, base, relief, materials, *, snow=None):
    rng = random.Random(seed)
    columns, rows = 76, 17
    peaks = [(rng.uniform(-span * .5, span * .5), rng.uniform(.62, 1), rng.uniform(span * .06, span * .15))
             for _ in range(8)]
    vertices = []
    for row in range(rows):
        t = row / (rows - 1)
        crest = max(0, 1 - abs(t - .70) / .70) ** .80
        for column in range(columns):
            x = span * (column / (columns - 1) - .5)
            mass = max(.20, *(high * exp(-(abs(x - center) / spread) ** 1.35) for center, high, spread in peaks))
            fragments = (sin(x * .18 + t * 7.8) * .036 + sin(x * .64 - t * 6.8) * .014
                         + rng.uniform(-.012, .012))
            z = base + relief * (crest * mass + fragments)
            vertices.append((x + rng.uniform(-.24, .24), near + t * depth + rng.uniform(-.35, .35), z))

    faces = []
    for row in range(rows - 1):
        for column in range(columns - 1):
            a = row * columns + column
            b, d = a + 1, a + columns
            faces.extend(((a, b, d), (b, d + 1, d)) if (row + column) % 2 else
                         ((a, b, d + 1), (a, d + 1, d)))

    item = mesh(c, name, vertices, faces, materials[0])
    for mat in materials[1:]:
        item.data.materials.append(mat)
    item["landscape"] = True
    item["bedrock_count"] = len(materials) - (2 if snow else 0)
    item["surface_style"] = 1
    if snow:
        item["snowline"] = snow
    for face in item.data.polygons:
        face.use_smooth = True
        z = sum(vertices[k][2] for k in face.vertices) / 3
        if snow and z > snow + rng.uniform(-1.7, 1.2) and face.normal.z > .29:
            face.material_index = rng.randrange(len(materials) - 2, len(materials))
        else:
            x, y, _ = face.center
            pattern = .47 + .22 * sin(x * .18 + y * .14) + .19 * cos(y * .37 - x * .11)
            face.material_index = int(pattern * item["bedrock_count"])
    return vertices


def water(c, name, base, materials):
    depth_steps = (3, 25, 52, 96, 162, 270, 480)
    verts = [(x, y, base + (.12 * sin(x * .13 + i) if i < 4 else 0))
             for i, y in enumerate(depth_steps) for x in (-450, -120, -35, 35, 120, 450)]
    faces = [(a, a + 1, a + 7, a + 6) for i in range(len(depth_steps) - 1)
             for a in range(i * 6, i * 6 + 5)]
    surface = mesh(c, name, verts, faces, materials[0])
    surface["foundation"] = True
    surface["surface_style"] = 3
    for mat in materials[1:]:
        surface.data.materials.append(mat)
    for p in surface.data.polygons:
        p.material_index = min(len(materials) - 1, p.index // 10)


def crane(c, x, y, base, scale, direction, paint, iron):
    def pt(px, py, pz):
        return (x + px * scale * direction, y + py * scale, base + pz * scale)

    def beam(name, a, b, width, mat=paint):
        return member(c, name, pt(*a), pt(*b), width * scale, width * scale, mat)

    box(c, "Crane foundation", pt(0, 0, -.4), (3 * scale, 2.3 * scale, .8 * scale), iron, bevel=0)
    for u in (-.51, .51):
        for v in (-.44, .44):
            beam("Crane mast upright", (u, v, 0), (u, v, 21.3), .15)
    for bay in range(16):
        low, high = bay * 1.32, (bay + 1) * 1.32
        d = (-1) ** bay * .51
        for v in (-.44, .44):
            beam("Crane mast diagonal", (-d, v, low), (d, v, high), .106)
            beam("Crane mast rung", (-.53, v, low), (.53, v, low), .125)
    for j in range(11):
        a, b = -4 + j * 1.52, -4 + (j + 1) * 1.52
        for z in (20.95, 21.95):
            beam("Crane jib chord", (a, -.30, z), (b, -.30, z), .16)
        beam("Crane jib web", (a, -.30, 20.95), (b, -.30, 21.95), .12)
    box(c, "Operator cab", pt(-.85, -.56, 20), (1.23 * scale, .96 * scale, 1.38 * scale), paint, bevel=0)
    box(c, "Operator cab dark window", pt(-.85, -1.052, 20.21), (.90 * scale, .08 * scale, .52 * scale),
        iron, bevel=0)
    beam("Hoist cable", (7.2, -.4, 21), (7.2, -.4, 11.3), .060, iron)
    box(c, "Suspended hook block", pt(7.2, -.4, 11.2), (.4 * scale, .31 * scale, .51 * scale), paint, bevel=0)


def concrete_shell(c, x, y, base, floors, scale, wall, underside):
    for level in range(floors + 1):
        z = base + level * 3.15 * scale
        box(c, "Precast floor slab", (x, y, z), (8.8 * scale, 5.4 * scale, .46 * scale), wall, bevel=0)
        for u in (-3.35, -1.13, 1.13, 3.35):
            box(c, "Slab form-tie recess", (x + u * scale, y - 2.714 * scale, z),
                (.16 * scale, .035 * scale, .085 * scale), underside, bevel=0)
        if level == floors:
            break
        for u in (-3.85, 0, 3.85):
            for v in (-2.25, 2.25):
                box(c, "Concrete pier", (x + u * scale, y + v * scale, z + 1.56 * scale),
                    (.62 * scale, .67 * scale, 3.12 * scale), wall, bevel=0)
        member(c, "Diagonal steel bracing", (x - 3.78 * scale, y + 1.7 * scale, z),
               (x - .10 * scale, y + 1.7 * scale, z + 3.1 * scale), .20 * scale, .20 * scale, underside)
    for u in (-2.7, 1.75, 3.3):
        box(c, "Unfinished roof wall", (x + u * scale, y + 1.55 * scale, z + .53 * scale),
            (1.07 * scale, .5 * scale, 1.07 * scale), wall, bevel=0)


def fir(c, pos, scale, phase, bark, needles):
    x, y, z = pos
    verts = []
    faces = []
    sides = 10
    for level, (r, bottom, top) in enumerate(((1.13, .54, 2.73), (.89, 1.57, 3.45), (.57, 2.55, 4.17))):
        start = len(verts)
        for i in range(sides):
            theta = i * 2 * pi / sides + phase + level * .27
            verts.append((x + cos(theta) * r * scale, y + sin(theta) * r * scale,
                          z + (bottom + .10 * sin(i * 4)) * scale))
        verts.append((x + .14 * scale, y + .14 * scale, z + top * scale))
        faces.extend((start + i, start + (i + 1) % sides, start + sides) for i in range(sides))
    tree = mesh(c, "Mountain fir with layered crown", verts, faces, needles)
    trunk = cylinder(c, "Fir trunk", (x, y, z + scale * .59), scale * .10, scale * 1.18, bark, 5)
    return tree, trunk


def wharf(c, z, concrete, iron, rust):
    top = z - 2.85
    columns, rows = 16, 11
    width, depth = 41.6, 25.3
    vertices = [(2.1 + width * (i / (columns - 1) - .5), 22 + depth * (j / (rows - 1) - .5), top)
                for j in range(rows) for i in range(columns)]
    faces = [(a, a + 1, a + columns + 1, a + columns)
             for j in range(rows - 1) for a in range(j * columns, j * columns + columns - 1)]
    border = [*range(columns), *range(2 * columns - 1, rows * columns, columns),
              *range(rows * columns - 2, (rows - 1) * columns - 1, -1),
              *range((rows - 2) * columns, 0, -columns)]
    foot = len(vertices)
    vertices.extend((vertices[i][0], vertices[i][1], top - 1.34) for i in border)
    faces.extend((border[j], foot + j, foot + (j + 1) % len(border), border[(j + 1) % len(border)])
                 for j in range(len(border)))
    faces.append(tuple(reversed(range(foot, len(vertices)))))
    mesh(c, "Wide loading quay deck", vertices, faces, concrete)
    for x in range(-18, 24, 4):
        box(c, "Vertical board-formed quay joint", (x, 9.32, top - .69), (.08, .055, 1.18), iron, bevel=0)
        cylinder(c, "Quayside mooring bollard", (x + .48, 10.1, top + .245), .16, .49, iron, 8)
        cylinder(c, "Rounded bollard cap", (x + .48, 10.1, top + .455), .22, .12, iron, 8)
    box(c, "Disused dock workshop walls", (-16.3, 23.5, top + 1.36), (6.7, 4.2, 2.72), rust, bevel=0)
    for x in (-18.1, -16.0, -14.25):
        box(c, "Dock workshop recessed window", (x, 21.38, top + 1.64), (1.14, .09, .84), iron, bevel=0)
    member(c, "Workshop front roof pitch", (-16.3, 21.2, top + 2.73), (-16.3, 23.55, top + 3.53),
           7.75, .20, iron)
    member(c, "Workshop rear roof pitch", (-16.3, 23.55, top + 3.53), (-16.3, 25.8, top + 2.73),
           7.75, .20, iron)


def escarpment(collection, rock):
    columns, rows = 92, 190
    vertices = []
    for row in range(rows):
        z = -16 + row * 1.44
        edge = (18 + 8 * sin(z * .058 + .9) + 4.2 * sin(z * .123) + max(0, z - 200) * .43
                + 19 * exp(-(z / 43) ** 2))
        crest = max(0, (z - 218) / 37)
        for index in range(columns):
            t = index / (columns - 1)
            x = edge + t * (113 - max(0, z - 200) * .6)
            crack = sin(x * .31 - z * .11 + sin(z * .073) * 1.8) ** 3
            rock_step = 2.1 * sin(x * .164 + z * .39) + 1.18 * sin(z * .76 + x * .29)
            y = 60 + 14 * cos(t * pi * .82) + 6 * sin(z * .07) + 4.4 * crack + rock_step
            height = z + .36 * sin(index * .87 + row * .75) + crest * (3.1 * sin(x * .134) - 4 * t)
            vertices.append((x, y, height))
    faces = [(a, a + 1, a + columns + 1, a + columns) for row in range(rows - 1)
             for a in range(row * columns, row * columns + columns - 1)]
    item = mesh(collection, "Continuous seaward escarpment, from tidal rock to summit", vertices, faces, rock[0])
    for material in rock[1:]:
        item.data.materials.append(material)
    item["landscape"], item["bedrock_count"], item["snowline"], item["surface_style"] = True, len(rock) - 2, 152, 1
    for face in item.data.polygons:
        x, _, z = face.center
        face.use_smooth = True
        if z > 153 and face.normal.z > .22:
            face.material_index = len(rock) - 1
        else:
            face.material_index = int((.48 + .35 * sin(z * .11 + x * .089)) * item["bedrock_count"])


def quarry_buttress(c, rock):
    columns, rows = 27, 31
    verts = []
    for row in range(rows):
        height = row / (rows - 1)
        for index in range(columns):
            width = index / (columns - 1)
            x = 27 - 9 * height + width * (36 + 13 * height)
            y = 70 - 26.5 * height + 28 * (2 * width - 1) ** 2 + 2.5 * sin(width * 24 + height * 7)
            z = 92 + 30 * height + sin(width * 12 + height * 16) * (1 - height) * 1.8
            verts.append((x, y, z))
    faces = [(a, a + 1, a + columns + 1, a + columns) for row in range(rows - 1)
             for a in range(row * columns, row * columns + columns - 1)]
    buttress = mesh(c, "Quarry platform founded on the natural slope", verts, faces, rock[0])
    for mat in rock[1:]:
        buttress.data.materials.append(mat)
    for face in buttress.data.polygons:
        face.use_smooth = True
    buttress["landscape"], buttress["bedrock_count"], buttress["surface_style"] = True, len(rock), 1


def continuous_world(scene):
    def collection(name):
        result = bpy.data.collections.new(name)
        scene.collection.children.link(result)
        return result

    massif = collection("01 · Continuous outdoor mountain")
    cliff = palette("85827a", "737779", "898b82", "656d71", "a4abac", "e6edef")
    escarpment(massif, cliff)
    terrain(massif, "Distant snow ranges rooted in the lower valleys", 61, 900, 388, 360, -19, 205,
            palette("74818d", "8593a0", "677784", "8d98a1", "b7c9d4", "e6edf1"), snow=127)
    terrain(massif, "Ocean-facing middle ridge", 264, 296, 112, 160, -12, 86,
            palette("75818a", "828d92", "788483", "86918b"))
    terrain(massif, "Folded slopes behind the wooded shoulder", 258, 252, 166, 132, -11, 92,
            palette("68796d", "6f8076", "85897a", "6d7f77"))

    coast = collection("02 · Old harbor at sea level")
    concrete, iron, paint, rust = palette("92978d", "535f63", "ce8858", "886653")
    water(coast, "Open harbor water", -5, palette("537f98", "6497aa", "8fb1bc"))
    terrain(coast, "Coastal scree at the foot of the mountain", 29, 106, 34, 44, -8, 19,
            palette("868b81", "9a9988", "a5a798", "76847e"))
    wharf(coast, -1, concrete, iron, rust)
    crane(coast, -8, 15.8, -3, 1.04, 1, paint, iron)
    concrete_shell(coast, 13.6, 17.2, -3.8, 6, 1, concrete, iron)
    for i in range(4):
        box(coast, "Abandoned dock equipment", (-11 + 2.5 * i, 23 + i, -1.2 + i * .14),
            (1.65, 1.25, 1.2 + i * .36), rust if i % 2 else concrete, bevel=0)

    forest = collection("03 · Fir forest on the lower slopes")
    vertices = terrain(forest, "Grounded west shoulder", 837, 188, 126, 119, -12, 77,
                       palette("536354", "677665", "7e8975", "596d61", "687972"))
    rng = random.Random(718)
    bark, pine, shade, pale = palette("55594c", "3b5747", "52715c", "627e64")
    wooded = [point for point in vertices if -61 < point[0] < 15 and 12 < point[2] < 89]
    for point in rng.sample(wooded, min(136, len(wooded))):
        fir(forest, point, rng.uniform(.65, 1.48), rng.uniform(0, pi), bark, rng.choice((pine, shade, pale)))

    quarry = collection("04 · Open-air quarry terrace")
    quarry_buttress(quarry, cliff[:4])
    box(quarry, "Quarry bench against the escarpment", (39, 60, 122),
        (42, 33, 3.3), concrete, bevel=0)
    crane(quarry, 23, 53, 123.65, .92, -1, paint, iron)
    concrete_shell(quarry, 37.8, 57, 123.65, 6, 1.08, concrete, iron)
    for i in range(3):
        cylinder(quarry, "Aggregate silo", (22 + 4.2 * i, 65, 127), 1.52, 6.7, concrete, 18)


def export(scene, filename):
    positions, indices, normals, foundations = [], [], [], []
    all_corner_normals, all_corner_colors, styles = [], [], []
    for item in scene.objects:
        if item.type != "MESH":
            continue
        offset = len(positions)
        data = item.data
        data.calc_loop_triangles()
        world = item.matrix_world
        normal_space = world.to_3x3().inverted_safe().transposed()
        positions.extend(tuple(world @ vertex.co) for vertex in data.vertices)
        indices.extend(tuple(offset + vertex for vertex in tri.vertices) for tri in data.loop_triangles)
        bedrock_count = item.get("bedrock_count", 0)
        if bedrock_count:
            average = np.mean([list(mat["srgb"]) for mat in data.materials[:bedrock_count]], axis=0)
        for tri in data.loop_triangles:
            ink = list(data.materials[tri.material_index]["srgb"])
            normal = tri.normal
            if item.get("landscape"):
                smooth = sum((data.vertices[index].normal for index in tri.vertices), Vector()).normalized()
                normal = normal.lerp(smooth, .93).normalized()
                if tri.material_index < bedrock_count:
                    ink = np.rint(np.array(ink) * .22 + average * .78)
            normals.append(tuple((normal_space @ normal).normalized()))
            corner_normals, corner_colors = [], []
            for index in tri.vertices:
                vert = data.vertices[index]
                smoothed = tri.normal.lerp(vert.normal, .97) if item.get("landscape") else normal
                corner_normals.append(tuple((normal_space @ smoothed).normalized()))
                if bedrock_count:
                    x, y, z = world @ vert.co
                    tint = average * (.95 + .055 * sin(x * .13 + y * .052) + .037 * cos(z * .17 - x * .12))
                    if "snowline" in item:
                        cover = max(0, min(1, (z - item["snowline"] + 3) / 6))
                        cover *= max(0.11, min(1, (vert.normal.z + .17) / .61))
                        snow = np.array(list(data.materials[-1]["srgb"]))
                        tint = tint * (1 - cover) + snow * cover
                    corner_colors.append(np.clip(tint, 0, 255))
                else:
                    corner_colors.append(ink)
            all_corner_normals.append(corner_normals)
            all_corner_colors.append(corner_colors)
            styles.append(item.get("surface_style", 0))
        foundations.extend([bool(item.get("foundation"))] * len(data.loop_triangles))
    vertices = np.asarray(positions, dtype=np.float32)
    triangles = np.asarray(indices, dtype=np.uint16 if len(vertices) <= 65536 else np.uint32)
    np.savez_compressed(filename, vertices=vertices, triangles=triangles,
                       normals=np.asarray(normals, dtype=np.float32),
                       foundation=np.asarray(foundations),
                       corner_normals=np.asarray(all_corner_normals, dtype=np.float32),
                       corner_colors=np.asarray(all_corner_colors, dtype=np.uint8),
                       style=np.asarray(styles, dtype=np.uint8))
    print(f"{filename.name}: {len(vertices)} vertices, {len(triangles)} triangles")


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.name = "One mountain · Sea to summit"
    continuous_world(scene)
    scene.unit_settings.system = "METRIC"
    scene.world = bpy.data.worlds.new("Coastal air")
    scene.world.color = (.23, .32, .44)
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    z = scenery_height(0)
    view = camera(scene, "Continuous ascent camera", (0, 25, z - 2.46), (0, -22, z), 45)
    view.data.type, view.data.lens, view.data.clip_end = "PERSP", 27.135, 1150
    for step in range(31):
        elevation = altitude_at(LEDGES[-1].y) * step / 30
        view.location.z = scenery_height(elevation)
        view.keyframe_insert(data_path="location", frame=1 + step * 8)
    scene.frame_set(1)
    light(scene, "High-left sun", "SUN", (-18, -24, 34), (0, 35, 5), 2.1, (1, .93, .81), .24)
    light(scene, "Blue daylight bounce", "SUN", (20, -8, 12), (0, 38, 6), .65, (.67, .81, 1), .30)
    bpy.context.view_layer.update()
    export(scene, ASSETS / "world.npz")
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.color_type = "MATERIAL"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "docs" / "cliff-distant-3d.blend"), compress=True)


if __name__ == "__main__":
    build()
