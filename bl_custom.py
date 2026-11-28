"""Запуск загруженной сцены-скрипта (Build_*.py) внутри мультфильма.
Скрипт сначала проходит scene_guard (безопасные импорты, без удаления чужих объектов/рендера/сохранения),
затем выполняется; созданные им объекты включаются только на кадрах своей сцены. Настройки рендера, мира
и камеры, которые скрипт мог поменять для своей картинки, возвращаются обратно. Героев ставим на «сцену»
(stage): центр по габаритам модели, высота пола лучом вниз, поворот выбирается так, чтобы камера не
упиралась в стены."""
import math
import random
from pathlib import Path

import bpy
from mathutils import Vector

import scene_guard
from bl_common import empty, light, reg, sphere


class _Blocked:
    def __getattr__(self, n):
        return self

    def __call__(self, *a, **k):
        return {"CANCELLED"}


def _snapshot(sc):
    r, v = sc.render, sc.view_settings
    return {"engine": r.engine, "res": (r.resolution_x, r.resolution_y, r.resolution_percentage), "path": r.filepath,
            "fmt": r.image_settings.file_format, "transp": r.film_transparent, "world": sc.world,
            "view": (v.view_transform, v.look, v.exposure, v.gamma), "camera": sc.camera,
            "frames": (sc.frame_start, sc.frame_end), "fps": r.fps, "step": sc.frame_step}


def _restore(sc, s):
    r, v = sc.render, sc.view_settings
    try:
        r.engine = s["engine"]
    except Exception:
        pass
    r.resolution_x, r.resolution_y, r.resolution_percentage = s["res"]
    r.filepath, r.film_transparent = s["path"], s["transp"]
    try:
        r.image_settings.file_format = s["fmt"]
    except Exception:
        pass
    try:
        v.view_transform, v.look, v.exposure, v.gamma = s["view"]
    except Exception:
        pass
    sc.world, sc.camera = s["world"], s["camera"]
    sc.frame_start, sc.frame_end = s["frames"]
    r.fps, sc.frame_step = s["fps"], s["step"]


def _world_color(w):
    try:
        bg = w.node_tree.nodes["Background"]
        c = bg.inputs[0].default_value
        return (c[0], c[1], c[2]), float(bg.inputs[1].default_value)
    except Exception:
        return None, 0.0


def _ray_blocked(meshes, origin: Vector, target: Vector) -> int:
    d = target - origin
    dist = d.length
    d.normalize()
    n = 0
    for o in meshes:
        try:
            inv = o.matrix_world.inverted()
            lo, ld = inv @ origin, inv.to_3x3() @ d
            hit, loc, _, _ = o.ray_cast(lo, ld)
            if hit and (o.matrix_world @ loc - origin).length < dist * 0.95:
                n += 1
        except Exception:
            continue
    return n


def _ground_z(meshes, cx, cy) -> float:
    best = None
    for o in meshes:
        try:
            inv = o.matrix_world.inverted()
            lo, ld = inv @ Vector((cx, cy, 2.0)), inv.to_3x3() @ Vector((0, 0, -1))
            hit, loc, _, _ = o.ray_cast(lo, ld)
            if hit:
                z = (o.matrix_world @ loc).z
                best = z if best is None else max(best, z)
        except Exception:
            continue
    return 0.0 if best is None else best


def _bbox_center(meshes):
    xs, ys = [], []
    for o in meshes:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            xs.append(w.x)
            ys.append(w.y)
    if not xs:
        return 0.0, 0.0
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def run(script_path: str, n_actors: int, stage_override: dict | None = None):
    """Возвращает stage-пустышку (в ней живут герои и камеры) или бросает исключение."""
    sc = bpy.context.scene
    code = scene_guard.sanitize(Path(script_path).read_text(errors="ignore"), Path(script_path).name)
    before = set(bpy.data.objects.keys())
    snap = _snapshot(sc)
    random.seed(0)
    try:
        exec(code, {"__name__": "__main__", "_blocked_op": _Blocked()})
    finally:
        script_world = sc.world
        _restore(sc, snap)
    new = [o for o in bpy.data.objects if o.name not in before]
    for o in new:
        if o.type == "CAMERA":
            bpy.data.objects.remove(o)
            continue
        reg(o)
    new = [o for o in new if o.name in bpy.data.objects]
    col, strength = _world_color(script_world) if script_world and script_world is not snap["world"] else (None, 0)
    if col and strength > 0:
        sphere("sky_dome", (0, 0, 0), (180, 180, 180), col, emit=max(0.6, min(strength, 3.0)), rough=1, seg=16)
    bpy.context.view_layer.update()
    meshes = [o for o in new if o.type == "MESH" and len(o.data.vertices) > 0]
    if stage_override:
        cx, cy, theta = stage_override.get("x", 0.0), stage_override.get("y", 0.0), math.radians(stage_override.get("yaw", 0.0))
        gz = stage_override.get("z", _ground_z(meshes, cx, cy))
    else:
        cx, cy = _bbox_center(meshes)
        gz = _ground_z(meshes, cx, cy)
        D = 5.4 + 1.25 * n_actors
        best, theta = None, 0.0
        for deg in (0, 90, -90, 180):
            t = math.radians(deg)
            cam = Vector((cx, cy, gz)) + Vector((math.sin(t) * D, -math.cos(t) * D, 1.7))
            blocked = _ray_blocked(meshes, cam, Vector((cx, cy, gz + 1.1)))
            if best is None or blocked < best:
                best, theta = blocked, t
            if blocked == 0:
                break
    stage = empty("stage", (cx, cy, gz), None, (0, 0, theta))
    sk = light("stage_key", "SUN", (0, 0, 8), (math.radians(60), 0, math.radians(-20)), 1.6, (1, 0.96, 0.9))
    sk.parent = stage
    print(f"CUSTOM_SCENE stage=({cx:.1f},{cy:.1f},{gz:.2f}) yaw={math.degrees(theta):.0f} objects={len(new)}", flush=True)
    return stage
