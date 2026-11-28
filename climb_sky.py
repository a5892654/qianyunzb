"""Perspective-rendered 3D vistas and procedural clouds for the climbing camera."""

import logging
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from climb import HEIGHT, WIDTH, clamp
from climb_course import BASE_Y, altitude_at, scenery_height


RASTER_WIDTH, RASTER_HEIGHT = 800, 450
FOCAL_LENGTH = 603
LOOK_DOWN = math.radians(3)
SUN = np.array((-.48, -.59, .65), dtype=np.float32)
CLOUD_ELEVATIONS = (1080, 2250, 5430, 6640)
LOGGER = logging.getLogger(__name__)


def _ease(value):
    value = clamp(value, 0, 1)
    return value * value * (3 - 2 * value)


def _project(vertices, camera_x, camera_z):
    x, y, z = vertices.T
    y = y + 22
    z = z - camera_z
    cosine, sine = math.cos(LOOK_DOWN), math.sin(LOOK_DOWN)
    depth = y * cosine - z * sine
    up = y * sine + z * cosine
    scale = FOCAL_LENGTH / np.maximum(depth, 1)
    pixels = np.column_stack((RASTER_WIDTH / 2 + (x - camera_x) * scale,
                              RASTER_HEIGHT / 2 - up * scale))
    return pixels, depth


class _CpuWorld:
    def __init__(self, data):
        self.vertices = data["vertices"]
        self.triangles = data["triangles"]
        self.foundation = data["foundation"]
        faces = self.vertices[self.triangles]
        self.fog = np.clip(1 - np.exp(-np.maximum(0, faces[:, :, 1].mean(axis=1) + 2) * .007),
                           0, .90)[:, None]
        shade = .64 + .44 * np.maximum(data["normals"] @ SUN, 0)[:, None]
        self.color = data["corner_colors"].mean(axis=1) * shade * (1 - self.fog)
        vertical = np.linspace(0, 1, RASTER_HEIGHT, dtype=np.float32)[:, None, None]
        self.sky_mix = np.minimum(1, vertical * 1.46) ** 1.23

    def draw(self, camera_x, elevation, zenith, horizon):
        points, depths = _project(self.vertices, camera_x, elevation)
        points = np.rint(points * 8).astype(np.int32)[self.triangles]
        u, v = points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]
        front = np.flatnonzero(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0] < 0)
        front_points = points[front]
        low, high = front_points.min(axis=1), front_points.max(axis=1)
        inside = ((high[:, 0] >= 0) & (low[:, 0] < RASTER_WIDTH * 8)
                  & (high[:, 1] >= 0) & (low[:, 1] < RASTER_HEIGHT * 8))
        visible = front[inside]
        face_depth = depths[self.triangles[visible]]
        in_front = face_depth.min(axis=1) > 1
        visible, face_depth = visible[in_front], face_depth[in_front]
        # The foundation is continuous under the terrain. Draw the remaining faces by camera depth.
        order = -face_depth.mean(axis=1) - self.foundation[visible] * 1000
        head, rim = np.asarray(zenith) * 255, np.asarray(horizon) * 255
        gradient = head + (rim - head) * self.sky_mix
        frame = np.ascontiguousarray(np.broadcast_to(gradient, (RASTER_HEIGHT, RASTER_WIDTH, 3)), dtype=np.uint8)
        sorted_visible = visible[np.argsort(order, kind="stable")]
        colors = np.clip(self.color[sorted_visible] + self.fog[sorted_visible] * rim, 0, 255).astype(np.uint8)
        for polygon, color in zip(points[sorted_visible], colors.tolist()):
            cv2.fillConvexPoly(frame, polygon, color, cv2.LINE_AA, shift=3)
        return Image.fromarray(cv2.resize(frame, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR))


def _cloud_strip(seed, size):
    rng = np.random.default_rng(seed)
    width, height = size
    field = np.zeros((height, width), np.float32)
    for rows, columns, strength in ((4, 12, 0.54), (9, 28, 0.28), (19, 65, 0.13), (40, 130, 0.05)):
        noise = rng.random((rows, columns), dtype=np.float32)
        # Wrap the horizontal samples so moving a tile across its own edge has no hard seam.
        tiled = np.tile(noise, (1, 3))
        field += cv2.resize(tiled, (width * 3, height), interpolation=cv2.INTER_CUBIC)[:, width:width * 2] * strength
    distance = np.abs(np.linspace(-1, 1, height, dtype=np.float32))[:, None]
    density = np.clip((field - 0.30 - 0.47 * distance ** 1.25) * 3.0, 0, 1)
    density *= np.clip((1 - distance) * 4, 0, 1) ** 2
    shades = np.clip(field * 0.78 + 0.27 - distance * 0.06, 0, 1)[..., None]
    colors = np.array((121, 149, 178)) + shades * np.array((133, 103, 74))
    texture = np.dstack((colors, density * 232)).astype(np.uint8)
    return Image.fromarray(texture)


class ClimbSky:
    def __init__(self, terrain, *, prefer_gpu=True):
        with np.load(Path(terrain) / "distant" / "world.npz", allow_pickle=False) as data:
            self.world = None
            if prefer_gpu:
                try:
                    from climb_sky_gpu import VistaGPU

                    geometry = tuple(data[key] for key in ("vertices", "triangles", "corner_normals",
                                                           "corner_colors", "style"))
                    lens = (2 * FOCAL_LENGTH / RASTER_WIDTH, 2 * FOCAL_LENGTH / RASTER_HEIGHT)
                    self.world = VistaGPU((WIDTH, HEIGHT), lens, LOOK_DOWN, geometry)
                except Exception as error:
                    LOGGER.info("OpenGL scenery unavailable, using the software renderer: %s", error)
            if self.world is None:
                self.world = _CpuWorld(data)
        self.clouds = (_cloud_strip(161, (1600, 280)), _cloud_strip(519, (1728, 220)))
        self.sky_clouds = _cloud_strip(421, (1970, 196))

    @staticmethod
    def camera_altitude(cy):
        return altitude_at(BASE_Y + cy)

    @staticmethod
    def _sky_colors(altitude):
        thin_air = _ease(altitude / 6500)
        sunrise = _ease((altitude - 6570) / 1900)
        zenith = tuple((base + (cold - base) * thin_air + warm * sunrise) / 255
                       for base, cold, warm in zip((32, 104, 178), (36, 69, 126), (7, -9, -8)))
        horizon = tuple((base + (cold - base) * thin_air + warm * sunrise) / 255
                        for base, cold, warm in zip((176, 210, 221), (161, 186, 205), (43, -9, -32)))
        return zenith, horizon

    @staticmethod
    def _blow_clouds(frame, cloud, x, y, strength):
        if strength <= 0 or y >= HEIGHT or y + cloud.height <= 0:
            return
        left = round(x) % cloud.width
        mask = cloud.getchannel("A").point([round(value * strength) for value in range(256)])
        frame.paste(cloud, (-left, y), mask)
        if cloud.width - left < WIDTH:
            frame.paste(cloud, (cloud.width - left, y), mask)

    def render(self, cx, cy, clock):
        altitude = self.camera_altitude(cy)
        frame = self.world.draw(cx * .019, scenery_height(altitude), *self._sky_colors(altitude))
        upper_cloud_y = round(-80 + min(97, altitude * .022))
        self._blow_clouds(frame, self.sky_clouds, cx * .09 + clock * 3, upper_cloud_y,
                          0.42 - 0.19 * _ease(altitude / 7000))
        for index, elevation in enumerate(CLOUD_ELEVATIONS):
            delta = altitude - elevation
            cloud = self.clouds[index % len(self.clouds)]
            y = round(HEIGHT * 0.52 - cloud.height / 2 + delta * 0.56)
            strength = 0.34 + 0.43 * _ease(1 - abs(delta) / 485)
            self._blow_clouds(frame, cloud, cx * 0.34 + clock * (6 + index * 2), y, strength)
        sea = _ease((altitude - 6760) / 640)
        if sea:
            y = round(HEIGHT * 0.67 + (altitude - 6760) * 0.036)
            self._blow_clouds(frame, self.clouds[1], clock * 6 + cx * 0.12, y, sea * 0.87)
            self._blow_clouds(frame, self.clouds[0], -clock * 10 + cx * 0.24 + 640, y + 44, sea * 0.94)
        return frame

    @classmethod
    def cloud_fog(cls, camera_y):
        altitude = cls.camera_altitude(camera_y)
        distance = min(abs(altitude - level) for level in CLOUD_ELEVATIONS)
        return _ease(1 - distance / 190)
