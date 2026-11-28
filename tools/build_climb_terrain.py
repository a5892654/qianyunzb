"""Build the original Blender terrain kit and render its orthographic game layers."""

import math
from pathlib import Path
import random
import sys

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "climb" / "blender"
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from climb_course import BASE_Y, CHAPTERS, CONSTRUCTION_PROFILE, LANDMARKS, LEDGES
from climb_landmarks import architecture_faces, ledge_faces, ledge_outline
from climb_objects import CLIMB_OBJECTS


def material(name, dark, light, *, grain=8.0, roughness=0.8, metal=0.0):
    result = bpy.data.materials.new(name)
    result.diffuse_color = (*light, 1)
    result.use_nodes = True
    nodes = result.node_tree.nodes
    shader = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metal
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = grain
    noise.inputs["Detail"].default_value = 5.0
    noise.inputs["Roughness"].default_value = 0.73
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.16
    ramp.color_ramp.elements[0].color = (*dark, 1)
    ramp.color_ramp.elements[1].position = 0.86
    ramp.color_ramp.elements[1].color = (*light, 1)
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.33
    bump.inputs["Distance"].default_value = 0.19
    links = result.node_tree.links
    links.new(noise.outputs["Fac"], ramp.inputs[0])
    links.new(ramp.outputs[0], shader.inputs["Base Color"])
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs[0], shader.inputs["Normal"])
    return result


def rock_mesh(name, center, stone, ledge_top, seed, scale_z=1.0):
    rng = random.Random(seed)
    outline = ((-2.75, -0.34), (-2.31, -0.78), (-0.87, -0.83), (0.76, -0.80),
               (2.42, -0.58), (2.72, -0.05), (2.55, 0.60), (1.34, 0.82),
               (-0.54, 0.75), (-2.22, 0.58))
    vertices = [(x * radius + x_shift + rng.uniform(-0.08, 0.08),
                 y * radius + rng.uniform(-0.08, 0.08),
                 (z + rng.uniform(-0.09, 0.09)) * scale_z)
                for z, radius, x_shift in ((-2.32, 0.65, 0.07), (-1.40, 0.83, -0.17),
                                           (-0.63, 1.02, 0.07), (-0.12, 1.00, 0.0),
                                           (0.0, 0.92, 0.03))
                for x, y in outline]
    n = len(outline)
    faces = [tuple(reversed(range(n)))]
    for row in range(4):
        for i in range(n):
            a, b = row * n + i, row * n + (i + 1) % n
            faces.extend(((a, b, b + n), (a, b + n, a + n)))
    faces.append(tuple(range(4 * n, 5 * n)))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    item = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(item)
    item.location.x = center
    mesh.materials.append(stone)
    mesh.materials.append(ledge_top)
    mesh.polygons[-1].material_index = 1
    return [item]


def block(name, center, size, surface, rotation=0.0, bevel=0.07):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center, rotation=(0, rotation, 0))
    item = bpy.context.object
    item.name = name
    item.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    item.data.materials.append(surface)
    shape = item.modifiers.new("Worn edges", "BEVEL")
    shape.width = bevel
    shape.segments = 2
    return item


def timber(center, wood, fittings, scaffold=False):
    title = "Scaffold" if scaffold else "Timber"
    items = [block(title + " deck", (center, 0, -0.18), (5.5, 1.28, 0.36), wood, bevel=0.09),
             block(title + " underbeam", (center, -0.36, -0.61), (5.13, 0.34, 0.53), wood)]
    for side in (-1, 1):
        items.append(block(title + " brace", (center + side * 1.28, -0.28, -1.0),
                           (2.1, 0.3, 0.24), wood, side * 0.56))
        items.append(block(title + " collar", (center + side * 2.08, -0.25, -0.38),
                           (0.26, 1.16, 0.7), fittings))
    return items


def steel_beam(center, surface, bolts):
    items = []
    for z, width, depth, thickness in ((-0.13, 5.48, 1.25, 0.26), (-0.55, 5.33, 0.26, 0.74),
                                      (-1.03, 5.42, 1.12, 0.22)):
        items.append(block("Rusted beam", (center, 0, z), (width, depth, thickness), surface))
    for side in (-1, 1):
        items.append(block("Bolted end plate", (center + side * 2.12, -0.18, -0.55),
                           (0.56, 0.27, 0.75), surface))
        for z in (-0.32, -0.76):
            bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.09, depth=0.10,
                                               location=(center + side * 2.12, -0.36, z),
                                               rotation=(math.pi / 2, 0, 0))
            rivet = bpy.context.object
            rivet.name = "Beam rivet"
            rivet.data.materials.append(bolts)
            items.append(rivet)
    return items


def aim(item, target):
    item.rotation_euler = (Vector(target) - item.location).to_track_quat("-Z", "Y").to_euler()


def architecture_mesh(name, painted, plane, palette, solid=None):
    vertices, faces, inks = [], [], []

    def add(poly, depth):
        offset = len(vertices)
        vertices.extend((x / 100, depth, (BASE_Y - y) / 100) for x, y in poly)
        return tuple(range(offset, offset + len(poly)))

    if solid is not None:
        front = add(solid, -0.30)
        back = add(solid, 0.45)
        faces.extend((front, back[::-1]))
        faces.extend((front[i - 1], back[i - 1], back[i], front[i]) for i in range(len(front)))
        inks.extend([painted[0][0]] * (2 + len(front)))
    for index, (color, poly) in enumerate(painted):
        faces.append(add(poly, plane - index * 0.0005))
        inks.append(color)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    for color in dict.fromkeys(inks):
        if color not in palette:
            label = "".join(f"{channel:02X}" for channel in color)
            color_material = bpy.data.materials.new(f"Landmark {label}")
            color_material.diffuse_color = (*(channel / 255 for channel in color), 1)
            palette[color] = color_material
        mesh.materials.append(palette[color])
    slots = {material.name: index for index, material in enumerate(mesh.materials)}
    for face, color in zip(mesh.polygons, inks):
        face.material_index = slots[palette[color].name]
    mesh.update()
    return bpy.data.objects.new(name, mesh)


def assemble_course(kits, key, fill):
    scene = bpy.data.scenes.new("Cliff Course")
    scene.unit_settings.system = "METRIC"
    scene.world = bpy.context.scene.world
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    variants = {}
    for index, (name, objects) in enumerate(kits.items()):
        kit = bpy.data.collections.new(f"Terrain · {name}")
        kit.instance_offset = (index * 10, 0, 0)
        for item in objects:
            kit.objects.link(item)
        variants[name] = kit
    for filename in ("cliff-construction.blend", "cliff-objects.blend"):
        with bpy.data.libraries.load(str(ROOT / "docs" / filename)) as (source, target):
            target.collections = source.collections
        variants.update({kit["game_asset"]: kit for kit in target.collections if "game_asset" in kit})
    chapters = []
    for index, spec in enumerate(CHAPTERS, start=1):
        collection = bpy.data.collections.new(f"{index:02d} {spec.name}")
        scene.collection.children.link(collection)
        chapters.append(collection)
    palette = {}
    for landmark in LANDMARKS:
        architecture = architecture_mesh(landmark.name, architecture_faces(landmark), 2.8, palette)
        chapters[LEDGES[landmark.first].chapter].objects.link(architecture)
    for index, ledge in enumerate(LEDGES):
        if ledge.piece:
            item = architecture_mesh(f"{index:04d} {ledge.landmark} · {ledge.piece}", ledge_faces(ledge),
                                     -0.304, palette, ledge_outline(ledge))
            item["landmark"] = ledge.landmark
            item["piece"] = ledge.piece
        else:
            name = ledge.model or ledge.material
            item = bpy.data.objects.new(f"{index:04d} {name}", None)
            item.instance_type = "COLLECTION"
            item.instance_collection = variants[name]
            item.location = (ledge.center / 100, 0, (BASE_Y - ledge.y) / 100)
            prop = CLIMB_OBJECTS.get(name)
            if prop:
                span = math.hypot(ledge.width, ledge.slope) / (100 * prop.width)
                item.scale = (span, span, ledge.underside(0) / (100 * prop.height))
            elif ledge.model:
                span = math.hypot(ledge.width, ledge.slope)
                depth = ledge.underside(ledge.y) - ledge.y
                item.scale = (span / CONSTRUCTION_PROFILE[2], 1, depth / 150)
            else:
                item.scale = (ledge.width / 550, 0.83, ledge.depth / 238)
            item.rotation_euler.y = math.atan2(ledge.slope, ledge.width)
        item["terrain"] = ledge.terrain
        item["slick"] = ledge.slick
        chapters[ledge.chapter].objects.link(item)
    camera = bpy.data.objects.new("Opening shot", bpy.data.cameras.new("Opening shot"))
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 12.8
    camera.location = (6.15, -18, 5.65)
    aim(camera, (6.15, 0, 2.23))
    scene.collection.objects.link(camera)
    scene.camera = camera
    for lamp in (key, fill):
        copy = lamp.copy()
        copy.location.x += 5.5
        aim(copy, (5.5, 0, 2.0))
        scene.collection.objects.link(copy)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D" and area.spaces.active.region_3d:
                area.spaces.active.region_3d.view_perspective = "CAMERA"
    return scene


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    sandstone = material("Sandstone", (0.09, 0.047, 0.024), (0.45, 0.30, 0.145))
    sand_top = material("Sandstone sunlit rim", (0.19, 0.10, 0.047), (0.58, 0.40, 0.21))
    basalt = material("Charcoal basalt", (0.027, 0.033, 0.032), (0.22, 0.24, 0.22))
    moss = material("Moss on basalt", (0.074, 0.075, 0.020), (0.27, 0.32, 0.080))
    chalk = material("Layered limestone", (0.095, 0.11, 0.10), (0.42, 0.43, 0.36))
    granite = material("Fractured granite", (0.095, 0.089, 0.084), (0.43, 0.42, 0.41))
    snow = material("Hard snow", (0.32, 0.42, 0.53), (0.85, 0.91, 0.98))
    wood = material("Weathered cedar", (0.081, 0.037, 0.012), (0.32, 0.19, 0.085), grain=14)
    pale_wood = material("Weathered scaffold", (0.087, 0.059, 0.04), (0.30, 0.23, 0.16), grain=14)
    iron = material("Aged iron", (0.046, 0.054, 0.051), (0.22, 0.22, 0.19), metal=0.68)
    rust = material("Oxidized steel", (0.075, 0.027, 0.011), (0.35, 0.128, 0.056), metal=0.37)
    kits = {
        "sandstone": rock_mesh("Coastal sandstone shelf", 0, sandstone, sand_top, 29),
        "basalt": rock_mesh("Basalt with moss lip", 10, basalt, moss, 117),
        "wood": timber(20, wood, iron),
        "limestone": rock_mesh("Limestone overhang", 30, chalk, chalk, 83),
        "scaffold": timber(40, pale_wood, rust, True),
        "steel": steel_beam(50, rust, iron),
        "snow": rock_mesh("Snow capped granite", 60, granite, snow, 91),
        "granite": rock_mesh("Fractured granite spine", 70, granite, granite, 104),
    }
    world = bpy.data.worlds.new("Soft grey sky")
    scene.world = world
    world.use_nodes = True
    background = next(node for node in world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Color"].default_value = (0.31, 0.37, 0.43, 1)
    background.inputs["Strength"].default_value = 0.48
    bpy.ops.object.camera_add(location=(0, -12, 4.0))
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 6.5
    scene.camera = camera
    bpy.ops.object.light_add(type="AREA", location=(-3, -4, 6))
    key = bpy.context.object
    key.data.energy = 830
    key.data.shape = "DISK"
    key.data.size = 5.0
    key.data.color = (1.0, 0.83, 0.68)
    bpy.ops.object.light_add(type="AREA", location=(3, -2, 2))
    fill = bpy.context.object
    fill.data.energy = 285
    fill.data.size = 4.0
    fill.data.color = (0.72, 0.83, 1.0)
    scene.render.film_transparent = True
    scene.render.resolution_x = 768
    scene.render.resolution_y = 456
    scene.render.resolution_percentage = 100
    scene.render.image_settings.color_mode = "RGBA"
    for i, (name, items) in enumerate(kits.items()):
        for other in kits.values():
            for item in other:
                item.hide_render = other is not items
        x = i * 10
        camera.location.x, key.location.x, fill.location.x = x, x - 3, x + 3
        for item in (camera, key, fill):
            aim(item, (x, 0, -0.87))
        scene.render.filepath = str(OUTPUT / f"{name}.png")
        bpy.ops.render.render(write_still=True)
    scene.render.filepath = "//"
    for items in kits.values():
        for item in items:
            item.hide_render = False
    camera.location.x, key.location.x, fill.location.x = 0, -3, 3
    for item in (camera, key, fill):
        aim(item, (0, 0, -0.87))
    course = assemble_course(kits, key, fill)
    if bpy.context.window:
        bpy.context.window.scene = course
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "cliff-course.blend"), compress=True)


if __name__ == "__main__":
    build()
