"""OpenGL scenery renderer. Its context is owned by a single worker thread."""

from concurrent.futures import ThreadPoolExecutor
import math

import moderngl
import numpy as np
from PIL import Image


SKY_VERTEX = """
#version 330
out vec2 uv;
void main() {
    uv = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
"""

SKY_FRAGMENT = """
#version 330
uniform vec3 zenith;
uniform vec3 horizon;
in vec2 uv;
out vec4 output_color;
void main() {
    float blend = pow(min(1.0, (1.0 - uv.y) * 1.46), 1.23);
    float glow = exp(-5.6 * length((uv - vec2(0.17, 0.31)) * vec2(.68, 1.35)));
    output_color = vec4(mix(zenith, horizon, blend) + vec3(.058, .036, .018) * glow, 1.0);
}
"""

WORLD_VERTEX = """
#version 330
uniform vec2 lens;
uniform vec2 eye;
uniform vec2 tilt;
in vec3 position;
in vec3 normal;
in vec3 tint;
in float surface_style;
out vec3 world;
out vec3 world_normal;
out vec3 color;
out float distance_y;
flat out int style;
void main() {
    world = position;
    world_normal = normal;
    color = tint;
    style = int(surface_style);
    vec3 offset = position - vec3(eye.x, -22.0, eye.y);
    distance_y = offset.y;
    float depth = offset.y * tilt.x - offset.z * tilt.y;
    float up = offset.y * tilt.y + offset.z * tilt.x;
    gl_Position = vec4(offset.x * lens.x, up * lens.y, depth * 1.0025 - 2.0025, depth);
}
"""

WORLD_FRAGMENT = """
#version 330
uniform vec3 horizon;
in vec3 world;
in vec3 world_normal;
in vec3 color;
in float distance_y;
flat in int style;
out vec4 output_color;

float hash(vec3 p) {
    p = fract(p * .3191 + vec3(.11, .79, .31));
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}
float noise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
        mix(mix(hash(i), hash(i+vec3(1,0,0)),f.x),
            mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
        mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),
            mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
void main() {
    vec3 normal = normalize(world_normal);
    vec3 light_dir = normalize(vec3(-.52, -.53, .67));
    float light = .51 + .46 * max(0.0, dot(normal, light_dir)) + .10 * max(0.0, normal.z);
    vec3 pigment = color;
    float scale = length(fwidth(world));
    float coarse = noise(world * .145);
    float medium = mix(noise(world * .60), .5, smoothstep(.8, 2.1, scale));
    float fine = mix(noise(world * 2.8), .5, smoothstep(.15, .73, scale));
    float marks = sin(world.z * 1.8 + world.y * .20 + coarse * 5.0);
    float base_fog = 1.0 - exp(-max(0.0, distance_y - 24.0) * .0059);
    vec3 fog_color = horizon;
    if (style == 1) {
        float mineral = smoothstep(.53, .80, coarse * .46 + medium * .36 + fine * .18 + .06 * marks);
        pigment = mix(pigment, pigment * vec3(1.20, 1.14, 1.03), mineral);
        pigment *= .74 + .28 * coarse + .21 * medium + .14 * fine + .04 * marks;
    } else if (style == 3) {
        light = .94;
        float water = sin(world.y * 1.7 + world.x * .044 + coarse * 8.0);
        pigment += smoothstep(.64, 1.0, water) * .063 * (1.0 - smoothstep(.25, 1.03, scale));
    } else {
        pigment *= .86 + .17 * medium + .13 * fine;
    }
    output_color = vec4(mix(pigment * light, fog_color, min(.90, base_fog)), 1.0);
}
"""


class VistaGPU:
    def __init__(self, size, lens, tilt, geometry):
        self.size = size
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="climb-scenery")
        try:
            self.worker.submit(self._open, lens, tilt, geometry).result()
        except Exception:
            self.worker.shutdown()
            raise

    def _open(self, lens, tilt, geometry):
        self.context = ctx = moderngl.create_standalone_context(require=330)
        samples = min(4, ctx.max_samples)
        self.result = ctx.simple_framebuffer(self.size, components=3)
        self.target = ctx.simple_framebuffer(self.size, components=3, samples=samples) if samples else self.result
        self.sky_shader = ctx.program(vertex_shader=SKY_VERTEX, fragment_shader=SKY_FRAGMENT)
        self.sky = ctx.vertex_array(self.sky_shader, [])
        self.world_shader = ctx.program(vertex_shader=WORLD_VERTEX, fragment_shader=WORLD_FRAGMENT)
        self.world_shader["lens"].value = lens
        self.world_shader["tilt"].value = (math.cos(tilt), math.sin(tilt))
        vertices, triangles, normals, colors, styles = geometry
        stream = np.empty((triangles.size, 10), dtype=np.float32)
        stream[:, :3] = vertices[triangles.ravel()]
        stream[:, 3:6] = normals.reshape(-1, 3)
        stream[:, 6:9] = colors.reshape(-1, 3) / 255
        stream[:, 9] = np.repeat(styles, 3)
        self.vertices = ctx.buffer(stream.tobytes())
        self.world = ctx.vertex_array(
            self.world_shader, [(self.vertices, "3f 3f 3f 1f", "position", "normal", "tint", "surface_style")],
        )

    def draw(self, camera_x, camera_z, zenith, horizon):
        return self.worker.submit(self._draw, camera_x, camera_z, zenith, horizon).result()

    def _draw(self, camera_x, camera_z, zenith, horizon):
        ctx = self.context
        self.target.use()
        self.target.clear()
        ctx.enable_only(0)
        self.sky_shader["zenith"].value = zenith
        self.sky_shader["horizon"].value = horizon
        self.sky.render(vertices=3)
        ctx.enable_only(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        self.world_shader["eye"].value = (camera_x, camera_z)
        self.world_shader["horizon"].value = horizon
        self.world.render()
        if self.target is not self.result:
            ctx.copy_framebuffer(self.result, self.target)
        return Image.frombytes("RGB", self.size, self.result.read(components=3), "raw", "RGB", 0, -1)
