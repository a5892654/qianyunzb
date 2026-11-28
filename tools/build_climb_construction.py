"""Build the dock and quarry models with Blender. Run with blender -b --python."""

from math import cos, pi, radians, sin
from pathlib import Path
import random
import sys

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets" / "climb" / "terrain" / "construction"
PREVIEWS = ROOT / "outputs" / "climb" / "construction"
sys.path.insert(0, str(ROOT))

from climb_course import CONSTRUCTION_MODELS, CONSTRUCTION_PROFILE


def color(hex_rgb):
    def linear(value):
        value /= 255
        return value / 12.92 if value < 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (*[linear(channel) for channel in bytes.fromhex(hex_rgb)], 1)


def ramp(nodes, points):
    node = nodes.new("ShaderNodeValToRGB")
    for index, (position, rgb) in enumerate(points):
        stops = node.color_ramp.elements
        stop = stops[index] if index < 2 else stops.new(position)
        stop.position = position
        stop.color = color(rgb)
    return node


def surface(name, tones, *, scale=3, stretch=(1, 1, 1), metal=0, roughness=0.83, relief=0.022):
    material = bpy.data.materials.new(name)
    material.diffuse_color = color(tones[len(tones) // 2][1])
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    shader = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Metallic"].default_value = metal
    shader.inputs["Roughness"].default_value = roughness
    coord = nodes.new("ShaderNodeTexCoord")
    stretch_node = nodes.new("ShaderNodeVectorMath")
    stretch_node.operation = "MULTIPLY"
    stretch_node.inputs[1].default_value = stretch
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = 4
    noise.inputs["Roughness"].default_value = 0.72
    grain = nodes.new("ShaderNodeTexNoise")
    grain.inputs["Scale"].default_value = 53
    grain.inputs["Detail"].default_value = 2
    small = nodes.new("ShaderNodeBump")
    small.inputs["Strength"].default_value = 0.38
    small.inputs["Distance"].default_value = relief * 0.5
    rough = nodes.new("ShaderNodeBump")
    rough.inputs["Strength"].default_value = 0.32
    rough.inputs["Distance"].default_value = relief
    palette = ramp(nodes, tones)
    links.new(coord.outputs["Object"], stretch_node.inputs[0])
    links.new(stretch_node.outputs["Vector"], noise.inputs["Vector"])
    links.new(coord.outputs["Object"], grain.inputs["Vector"])
    links.new(noise.outputs["Fac"], palette.inputs[0])
    links.new(palette.outputs[0], shader.inputs["Base Color"])
    links.new(grain.outputs["Fac"], small.inputs["Height"])
    links.new(noise.outputs["Fac"], rough.inputs["Height"])
    links.new(small.outputs[0], rough.inputs["Normal"])
    links.new(rough.outputs[0], shader.inputs["Normal"])
    return material


def mesh(collection, name, verts, faces, material):
    data = bpy.data.meshes.new(name)
    data.from_pydata(verts, [], faces)
    data.update()
    item = bpy.data.objects.new(name, data)
    collection.objects.link(item)
    item.data.materials.append(material)
    return item


def prism(collection, name, outline, near, far, material, bevel=0.025):
    n = len(outline)
    verts = [(x, y, z) for y in (near, far) for x, z in outline]
    faces = [tuple(range(n)), tuple(reversed(range(n, 2 * n)))]
    faces += [(i, i + n, (i + 1) % n + n, (i + 1) % n) for i in range(n)]
    item = mesh(collection, name, verts, faces, material)
    if bevel:
        modifier = item.modifiers.new("Worn edges", "BEVEL")
        modifier.width = bevel
        modifier.segments = 2
        modifier.harden_normals = True
        item.modifiers.new("Planar normals", "WEIGHTED_NORMAL")
    return item


def box(collection, name, at, size, material, bevel=0.018):
    x, y, z = (length / 2 for length in size)
    item = prism(collection, name, [(-x, -z), (x, -z), (x, z), (-x, z)], -y, y, material, bevel)
    item.location = at
    return item


def member(collection, name, start, end, width, depth, material):
    start, end = Vector(start), Vector(end)
    delta = end - start
    item = box(collection, name, (start + end) / 2, (width, depth, delta.length),
               material, min(width, depth) * 0.13)
    item.rotation_euler = delta.to_track_quat("Z", "Y").to_euler()
    return item


def cylinder(collection, name, center, radius, height, material, sides=16, axis="Z"):
    rings = [(radius * cos(2 * pi * i / sides), radius * sin(2 * pi * i / sides), z)
             for z in (-height / 2, height / 2) for i in range(sides)]
    if axis == "Y":
        rings = [(x, -z, y) for x, y, z in rings]
    faces = [tuple(reversed(range(sides))), tuple(range(sides, 2 * sides))]
    faces += [(i, (i + 1) % sides, (i + 1) % sides + sides, i + sides) for i in range(sides)]
    item = mesh(collection, name, rings, faces, material)
    item.location = center
    for face in item.data.polygons[2:]:
        face.use_smooth = sides > 8
    return item


def concrete(collection, m, *, slab):
    if slab:
        outline = [(-2.75, -1.36), (-2.2, -1.41), (-1.63, -1.5), (-0.61, -1.44),
                   (-0.23, -1.5), (0.84, -1.48), (1.11, -1.4), (2.36, -1.44), (2.75, -1.31),
                   (2.75, -0.17), (2.65, 0), (1.67, 0), (1.56, -0.065), (1.35, 0),
                   (-0.79, 0), (-0.89, -0.048), (-1.12, 0), (-2.66, 0), (-2.75, -0.085)]
        prism(collection, "Broken slab with spalled corners", outline, -0.68, 0.66, m["concrete"], 0.045)
    else:
        prism(collection, "Precast bridge edge",
              [(-2.75, -1.37), (-2.3, -1.5), (2.5, -1.5), (2.75, -1.4), (2.75, -0.13),
               (2.68, 0), (-2.69, 0), (-2.75, -0.09)], -0.68, 0.77, m["concrete"], 0.035)
        box(collection, "Formwork seam", (0, -0.685, -0.59), (5.35, 0.018, 0.021), m["stone"])
        for x in (-1.88, 0.65, 1.82):
            box(collection, "Cast rib", (x, -0.72, -0.95), (0.28, 0.22, 0.92), m["concrete"])
        for x in (-2.35, -2.0, 1.95, 2.30):
            box(collection, "Faded ochre edge paint", (x, -0.69, -0.17), (0.27, 0.028, 0.13),
                m["yellow"])
    rng = random.Random(89 if slab else 31)
    for x, z in ((-2.43, -0.26), (-0.91, -0.24), (1.42, -0.16), (2.33, -1.23), (-1.53, -1.13)):
        w = rng.uniform(0.22, 0.49)
        points = [(x - w * 0.5, -0.686, z), (x - w * 0.32, -0.687, z + 0.07),
                  (x + w * 0.11, -0.687, z + 0.034), (x + w * 0.48, -0.687, z + 0.08),
                  (x + w * 0.39, -0.686, z - 0.063), (x - w * 0.19, -0.686, z - 0.09)]
        mesh(collection, "Exposed aggregate", points, [tuple(reversed(range(6)))], m["stone"])
        for _ in range(8):
            spot_x = x + rng.uniform(-0.2, 0.2) * w
            spot_z = z + rng.uniform(-0.05, 0.05)
            box(collection, "Aggregate fleck", (spot_x, -0.694, spot_z),
                (rng.uniform(0.012, 0.061), 0.011, rng.uniform(0.012, 0.036)), m["concrete"], 0.005)
        start = (x, -0.690, z)
        for _ in range(4):
            end = (start[0] + rng.uniform(-0.11, 0.13), -0.691,
                   max(-1.39, start[2] - rng.uniform(0.033, 0.09)))
            member(collection, "Hairline crack", start, end, 0.008, 0.012, m["stone"])
            start = end
    for x in (-2.02, -0.44, 0.53, 2.2):
        cylinder(collection, "Recessed tie mark", (x, -0.685, -0.36), 0.038, 0.023, m["stone"], 8, "Y")


def skip_bin(collection, m):
    outline = [(-1.98, -1.46), (1.94, -1.46), (2.68, -0.65), (2.73, -0.11),
               (-2.73, -0.11), (-2.65, -0.64)]
    prism(collection, "Pressed skip sides and floor", outline, -0.59, 0.72, m["yellow"], 0.045)
    box(collection, "Worn steel rim", (0, -0.59, -0.092), (5.5, 0.18, 0.184), m["yellow"], 0.024)
    box(collection, "Salvage piled flush with rim", (0, 0.073, -0.1), (5.21, 1.41, 0.132), m["stone"])
    for x in (-1.59, -0.72, 0.66, 1.55):
        member(collection, "Pressed side stiffener", (x * 0.91, -0.64, -1.36),
               (x, -0.643, -0.20), 0.12, 0.12, m["yellow"])
    for x in (-2.16, 2.16):
        box(collection, "Lifting lug", (x, -0.705, -0.39), (0.39, 0.19, 0.29), m["iron"])
        cylinder(collection, "Lug pin", (x, -0.82, -0.39), 0.066, 0.066, m["rust"], axis="Y")
    for i, x in enumerate((-0.22, -0.05, 0.12, 0.29)):
        item = box(collection, "Worn chevron", (x, -0.602, -0.42), (0.073, 0.020, 0.28), m["iron"], 0.005)
        item.rotation_euler.y = radians(30 - i % 2 * 3)


def crane_truss(collection, m):
    box(collection, "Upper box chord", (0, -0.07, -0.108), (5.5, 1.22, 0.216), m["orange"])
    box(collection, "Rear web", (0, 0.38, -0.73), (5.37, 0.19, 1.17), m["iron"])
    box(collection, "Lower flange", (0, -0.09, -1.36), (5.5, 1.12, 0.23), m["orange"])
    for i in range(7):
        x = -2.65 + i * (5.30 / 6)
        member(collection, "Welded upright", (x, -0.57, -1.30), (x, -0.57, -0.16), 0.17, 0.15, m["orange"])
        if i:
            start, end = (x - 5.3 / 6, x) if i % 2 else (x, x - 5.3 / 6)
            member(collection, "Diagonal truss brace", (start, -0.54, -1.28), (end, -0.54, -0.17),
                   0.105, 0.126, m["orange"])
        for z in (-0.13, -1.36):
            cylinder(collection, "Blackened weld bolt", (x, -0.71, z), 0.048, 0.07, m["iron"], 8, "Y")


def switchgear(collection, m):
    box(collection, "Salvaged switchgear shell", (0, -0.03, -0.75), (5.5, 1.30, 1.5), m["rust"], 0.07)
    box(collection, "Folded cabinet roof", (0, -0.03, -0.067), (5.48, 1.36, 0.134), m["green"], 0.027)
    for x in (-1.73, 0, 1.73):
        box(collection, "Door recessed gasket", (x, -0.69, -0.75), (1.67, 0.067, 1.24), m["iron"])
        box(collection, "Flaking enamel door", (x, -0.733, -0.74), (1.59, 0.076, 1.17), m["green"], 0.035)
        box(collection, "Recessed pull handle", (x + 0.51, -0.79, -0.62), (0.11, 0.10, 0.25), m["iron"])
        for z in (-0.23, -1.18):
            box(collection, "Hinge", (x - 0.77, -0.77, z), (0.12, 0.10, 0.15), m["rust"])
    for x in (-1.76, 1.70):
        for i in range(5):
            box(collection, "Vent slot", (x, -0.774, -0.82 - i * 0.081), (0.53, 0.023, 0.036), m["iron"], 0.008)
    for material, scale, y in ((m["iron"], 1.0, -0.777), (m["yellow"], 0.78, -0.783)):
        mesh(collection, "Warning triangle", [(x * scale, y, -0.61 + z * scale)
             for x, z in ((-0.18, -0.16), (0.18, -0.16), (0, 0.17))], [(0, 1, 2)], material)
    member(collection, "Voltage mark", (0.035, -0.79, -0.54), (-0.052, -0.79, -0.66), 0.025, 0.012,
           m["iron"])
    member(collection, "Voltage mark", (-0.052, -0.79, -0.66), (0.041, -0.79, -0.65), 0.025, 0.012,
           m["iron"])
    member(collection, "Voltage mark", (0.041, -0.79, -0.65), (-0.028, -0.79, -0.72), 0.025, 0.012,
           m["iron"])


def drum_pallet(collection, m):
    box(collection, "Loading deck", (0, -0.03, -0.11), (5.5, 1.30, 0.22), m["steel"], 0.029)
    box(collection, "Pallet base", (0, -0.005, -1.37), (5.5, 1.25, 0.23), m["steel"])
    box(collection, "Rack backing", (0, 0.47, -0.76), (5.42, 0.20, 1.2), m["iron"])
    for i, x in enumerate((-2.01, -0.71, 0.62, 1.95)):
        paint = m["red"] if i != 2 else m["green"]
        cylinder(collection, "Sealed salvage drum", (x, -0.07, -0.77), 0.50, 1.08, paint, 24)
        cylinder(collection, "Faded barrel band", (x, -0.07, -0.77), 0.503, 0.25, m["pale"], 24)
        for z in (-1.25, -1.05, -0.49, -0.25):
            cylinder(collection, "Rolled stiffening hoop", (x, -0.07, z), 0.524, 0.055, paint, 24)
    for x in (-2.59, 2.59):
        box(collection, "Pallet corner", (x, -0.55, -0.75), (0.27, 0.24, 1.40), m["steel"])


def camera(scene, name, center, eye, scale):
    item = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    scene.collection.objects.link(item)
    item.location = eye
    item.rotation_euler = (Vector(center) - item.location).to_track_quat("-Z", "Y").to_euler()
    item.data.type = "ORTHO"
    item.data.ortho_scale = scale
    item.data.clip_end = 250
    scene.camera = item
    return item


def light(scene, name, kind, eye, target, energy, tint, size):
    data = bpy.data.lights.new(name, kind)
    data.color = tint
    data.energy = energy
    if kind == "SUN":
        data.angle = size
    else:
        data.shape = "DISK"
        data.size = size
    item = bpy.data.objects.new(name, data)
    scene.collection.objects.link(item)
    item.location = eye
    item.rotation_euler = (Vector(target) - item.location).to_track_quat("-Z", "Y").to_euler()


def setup(scene, width, height, *, transparent=False):
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = transparent
    scene.render.image_settings.file_format = "WEBP"
    scene.render.image_settings.color_mode = "RGBA" if transparent else "RGB"
    scene.render.image_settings.quality = 93
    scene.world = bpy.data.worlds.new(f"{scene.name} sky")
    scene.world.use_nodes = True
    background = next(node for node in scene.world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Color"].default_value = (0.42, 0.65, 0.92, 1)
    background.inputs["Strength"].default_value = 0.37
    scene.view_settings.view_transform = "Standard"
    scene.unit_settings.system = "METRIC"
    scene.eevee.taa_render_samples = 48


def render(scene, path):
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    scene.render.filepath = "//"


def sky_material():
    material = bpy.data.materials.new("Blue sky and thin downwind cloud")
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    coords = nodes.new("ShaderNodeTexCoord")
    components = nodes.new("ShaderNodeSeparateXYZ")
    stretch = nodes.new("ShaderNodeVectorMath")
    stretch.operation = "MULTIPLY"
    stretch.inputs[1].default_value = (4.8, 1, 10.5)
    clouds = nodes.new("ShaderNodeTexNoise")
    clouds.inputs["Scale"].default_value = 2.3
    clouds.inputs["Detail"].default_value = 4.2
    clouds.inputs["Roughness"].default_value = 0.53
    clouds.inputs["Distortion"].default_value = 0.55
    threshold = nodes.new("ShaderNodeMapRange")
    threshold.clamp = True
    threshold.interpolation_type = "SMOOTHERSTEP"
    threshold.inputs["From Min"].default_value = 0.50
    threshold.inputs["From Max"].default_value = 0.68
    cloud_height = nodes.new("ShaderNodeMapRange")
    cloud_height.clamp = True
    cloud_height.inputs["From Min"].default_value = 0.24
    cloud_height.inputs["From Max"].default_value = 0.42
    density = nodes.new("ShaderNodeMath")
    density.operation = "MULTIPLY"
    palette = ramp(nodes, ((0.10, "e3e5d5"), (0.23, "97d4ee"), (0.35, "43a9e9"), (0.54, "1b69be")))
    mix = nodes.new("ShaderNodeMixRGB")
    mix.inputs[2].default_value = color("eef3ee")
    links.new(coords.outputs["Generated"], components.inputs[0])
    links.new(coords.outputs["Generated"], stretch.inputs[0])
    links.new(stretch.outputs["Vector"], clouds.inputs["Vector"])
    links.new(clouds.outputs["Fac"], threshold.inputs[0])
    links.new(components.outputs["Z"], palette.inputs[0])
    links.new(palette.outputs[0], mix.inputs[1])
    links.new(components.outputs["Z"], cloud_height.inputs[0])
    links.new(cloud_height.outputs[0], density.inputs[0])
    links.new(threshold.outputs[0], density.inputs[1])
    links.new(density.outputs[0], mix.inputs[0])
    links.new(mix.outputs[0], emission.inputs["Color"])
    links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def ridge(collection, name, at, height, seed, material):
    rng = random.Random(seed)
    left, y = at
    peaks = [height * factor + rng.uniform(-0.36, 0.36) for factor in
             (0.45, 0.54, 0.59, 0.76, 0.71, 0.9, 0.98, 0.79, 0.65, 0.72, 0.61, 0.53, 0.49, 0.43)]
    rows, detail = 10, 5
    n = (len(peaks) - 1) * detail + 1
    verts = []
    for row in range(rows):
        t = row / (rows - 1)
        for i in range(n):
            index = min(i // detail, len(peaks) - 2)
            fraction = i / detail - index
            peak = peaks[index] * (1 - fraction) + peaks[index + 1] * fraction
            cut = (sin(i * 0.47 + t * 4 + seed) * 0.36 + sin(i * 0.9 - t * 8) * 0.20) * t
            verts.append((left + i * 3.1 / detail + rng.uniform(-0.18, 0.18),
                          y + t * 8.4 + rng.uniform(-0.29, 0.29),
                          -5 + (peak + 5) * t + cut + rng.uniform(-0.15, 0.15)))
    faces = []
    for row in range(rows - 1):
        for i in range(n - 1):
            a = row * n + i
            faces.extend(((a, a + 1, a + n), (a + n, a + 1, a + n + 1)))
    mesh(collection, name, verts, faces, material)


def backdrop(m):
    scene = bpy.data.scenes.new("Dock and Quarry Backdrops")
    setup(scene, 1440, 810)
    collection = scene.collection
    mesh(collection, "Sky plate", [(-60, 75, -11), (60, 75, -11), (60, 75, 37), (-60, 75, 37)],
         [(0, 1, 2, 3)], sky_material())
    ridge(collection, "Far blue coastal ridgeline", (-23, 39), 5.8, 78, m["far"])
    ridge(collection, "Quarry terraces", (-19, 20), 2.9, 19, m["hill"])
    ridge(collection, "Gravel tailings", (-20, 9), 0.0, 7, m["gravel"])
    for floor in (1.3, 6.0, 10.7, 15.4, 20.1):
        box(collection, "Unfinished concrete floor", (12.7, 8.1, floor), (8.8, 4.3, 0.66), m["distant"])
        for x in (9.1, 15.6):
            for y in (6.8, 9.5):
                box(collection, "Concrete pier", (x, y, floor + 2.18), (0.76, 0.91, 4.65), m["distant"])
        for x in (9.15, 11.35, 13.55, 15.75):
            cylinder(collection, "Dark formwork pocket", (x, 5.948, floor + 0.05), 0.08, 0.033,
                     m["hill"], 8, "Y")
        if floor in (6.0, 15.4):
            member(collection, "Dark braced interior", (14.7, 7.6, floor), (10.3, 8.7, floor + 4.4),
                   0.25, 0.29, m["hill"])
    x, y = -5.0, 10.0
    for a in (-0.43, 0.43):
        for b in (-0.43, 0.43):
            box(collection, "Crane mast upright", (x + a, y + b, 7.4), (0.16, 0.16, 21.2), m["crane"])
    for i in range(17):
        z = -3.0 + i * 1.23
        for b in (-0.43, 0.43):
            member(collection, "Crane mast rung", (x - 0.43, y + b, z), (x + 0.43, y + b, z),
                   0.077, 0.074, m["crane"])
            direction = (-1) ** i
            member(collection, "Crane mast brace", (x - direction * 0.43, y + b, z),
                   (x + direction * 0.43, y + b, z + 1.23), 0.088, 0.081, m["crane"])
    for i in range(10):
        start = x - 3 + i * 1.36
        member(collection, "Jib lower chord", (start, y, 17.7), (start + 1.36, y, 17.7), 0.13, 0.28, m["crane"])
        member(collection, "Jib upper chord", (start, y, 18.48), (start + 1.36, y, 18.48), 0.12, 0.22, m["crane"])
        member(collection, "Jib web", (start, y, 17.7), (start + 1.36, y, 18.48), 0.085, 0.10, m["crane"])
    member(collection, "Hoist cable", (3.65, y - 0.17, 17.7), (3.65, y - 0.17, 9.9), 0.038, 0.038, m["hill"])
    box(collection, "Hoist block", (3.65, y - 0.17, 9.83), (0.29, 0.19, 0.35), m["crane"])
    for i, (x, z) in enumerate(((-9.6, -0.94), (-8.7, -1.18), (-7.9, -1.4), (5.2, -0.91), (6.1, -0.7))):
        drum = m["red"] if i % 2 else m["steel"]
        cylinder(collection, "Abandoned yard drum", (x, 9.1, z), 0.34, 0.87, drum)
        cylinder(collection, "Faded yard drum band", (x, 9.1, z), 0.342, 0.16, m["pale"])
    for i, (x, z) in enumerate(((-11.6, -1.4), (-3.3, -1.77), (-1.2, -1.72), (7.7, -1.15))):
        item = box(collection, "Discarded concrete foundation", (x, 8.2 + i * 0.55, z),
                   (2.0, 1.25, 0.58), m["distant"], 0.13)
        item.rotation_euler.y = radians(-12 + i * 6)
    light(scene, "Dry coastal sunlight", "SUN", (-14, -9, 23), (0, 9, 1), 1.38, (1, 0.90, 0.78), 0.14)
    light(scene, "Sky fill", "SUN", (5, -16, 14), (0, 9, 2), 0.62, (0.68, 0.82, 1), 0.30)
    camera(scene, "Dock angle", (0, 8, 7.7), (0, -42, 10.2), 28.6)
    render(scene, ASSETS / "dock.webp")
    camera(scene, "Quarry angle", (6.8, 9, 10.8), (3.9, -44, 15.5), 31)
    render(scene, ASSETS / "quarry.webp")


def build():
    ASSETS.mkdir(parents=True, exist_ok=True)
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    studio = bpy.context.scene
    studio.name = "Construction Kit"
    m = {
        "concrete": surface("Chipped board-formed concrete", ((0.22, "515450"), (0.46, "838781"),
                            (0.62, "a6a799"), (0.81, "c5c0ac")), scale=6, stretch=(1.7, 1, 0.44)),
        "stone": surface("Exposed concrete aggregate", ((0.23, "333b36"), (0.74, "788178")), scale=23),
        "yellow": surface("Ochre enamel worn to rust", ((0.31, "765031"), (0.41, "b68a3c"),
                          (0.48, "c59c43"), (0.70, "e7c357")), scale=4.1, stretch=(1.3, 1, 0.62), metal=0.26),
        "orange": surface("Oxide orange crane paint", ((0.30, "633d2c"), (0.46, "a55331"),
                          (0.59, "c86e40"), (0.76, "e09358")), scale=7, stretch=(1.2, 1, 0.65), metal=0.33),
        "iron": surface("Blackened iron", ((0.25, "292e2d"), (0.76, "5c5c51")), metal=0.5),
        "rust": surface("Exposed rust", ((0.25, "442f27"), (0.76, "966045")), scale=9, metal=0.43),
        "green": surface("Flaking grey green enamel", ((0.33, "694631"), (0.47, "867461"),
                         (0.54, "919587"), (0.70, "b5b3a0")), scale=4.4, stretch=(2.55, 1, 0.32), metal=0.26),
        "steel": surface("Worn galvanized steel", ((0.30, "434c4b"), (0.74, "87958e")), scale=7, metal=0.46),
        "red": surface("Old oxide red paint", ((0.28, "694233"), (0.52, "9b4b3d"), (0.74, "be6c50")),
                       scale=10, stretch=(1.7, 1, 0.42), metal=0.3),
        "pale": surface("Dirty pale enamel", ((0.32, "826e5a"), (0.72, "d5cdbc")), scale=14, metal=0.24),
        "far": surface("Haze on distant rock", ((0.31, "738fa3"), (0.72, "9fadb4")), scale=0.76, relief=0.06),
        "hill": surface("Quarry gravel and slate", ((0.29, "65787d"), (0.76, "a4aba3")), scale=1.3, relief=0.10),
        "gravel": surface("Coastal rubble", ((0.28, "7d817a"), (0.76, "b8b2a0")), scale=2.1, relief=0.12),
        "crane": surface("Weathered ochre crane", ((0.31, "a86743"), (0.75, "dca372")), scale=5, metal=0.2),
        "distant": surface("Concrete shell in coastal haze", ((0.26, "666d68"), (0.77, "a4a79b")),
                           scale=4, stretch=(1, 1, 0.42)),
    }
    models = {}
    builders = (lambda c: concrete(c, m, slab=True), lambda c: concrete(c, m, slab=False),
                lambda c: skip_bin(c, m), lambda c: crane_truss(c, m),
                lambda c: switchgear(c, m), lambda c: drum_pallet(c, m))
    names = ("Spalled concrete", "Precast cantilever", "Ochre salvage skip",
             "Orange crane truss", "Weathered switchgear", "Drum loading pallet")
    for key, name, builder in zip(CONSTRUCTION_MODELS, names, builders, strict=True):
        collection = bpy.data.collections.new(name)
        collection.asset_mark()
        collection["game_asset"] = key
        collection["contact_plane"] = "Front rim: X ±2.75 m, Y -0.68 m, Z 0 m"
        builder(collection)
        models[key] = collection

    plates = bpy.data.scenes.new("Game Sprite Projection")
    setup(plates, 720, 300, transparent=True)
    depth, tilt = 18, radians(12)
    up = Vector((0, sin(tilt), cos(tilt)))
    rim_height = (plates.render.resolution_y / 2 - CONSTRUCTION_PROFILE[1]) / 100
    target = Vector((0, -0.68, 0)) - rim_height * up
    camera(plates, "Game orthographic camera", target,
           target + Vector((0, -depth * cos(tilt), depth * sin(tilt))), 7.2)
    light(plates, "Outdoor key", "AREA", (-3, -4, 6), (0, 0, -0.4), 680, (1, 0.87, 0.73), 5.5)
    light(plates, "Sky bounce", "AREA", (4, -3, 4), (0, 0, -0.5), 345, (0.66, 0.82, 1), 5.7)
    light(plates, "Edge light", "AREA", (0, 2, 5), (0, 0, -0.1), 420, (1, 0.93, 0.80), 4.6)
    for name, collection in models.items():
        plates.collection.children.link(collection)
        render(plates, ASSETS / f"{name}.webp")
        plates.collection.children.unlink(collection)
    plates.collection.children.link(next(iter(models.values())))

    setup(studio, 1440, 590)
    world_shader = next(node for node in studio.world.node_tree.nodes if node.type == "BACKGROUND")
    world_shader.inputs["Color"].default_value = (0.14, 0.19, 0.25, 1)
    for index, collection in enumerate(models.values()):
        instance = bpy.data.objects.new(collection.name, None)
        studio.collection.objects.link(instance)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        instance.location = ((index % 3 - 1) * 6.7, 0, 2.45 - index // 3 * 3.62)
    camera(studio, "Kit overview", (0, 0, -0.25), (3.4, -29, 9.1), 21.3)
    light(studio, "Catalog key", "SUN", (-6, -8, 9), (0, 0, 0), 2.25, (1, 0.91, 0.82), 0.16)
    light(studio, "Catalog fill", "SUN", (5, -8, 6), (0, 0, 0), 1.12, (0.67, 0.82, 1), 0.34)
    backdrop(m)
    studio.render.image_settings.file_format = "PNG"
    render(studio, PREVIEWS / "construction-kit.png")
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.color_type = "MATERIAL"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "docs" / "cliff-construction.blend"), compress=True)


if __name__ == "__main__":
    build()
