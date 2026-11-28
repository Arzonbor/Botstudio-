"""Общие кирпичики для blender_worker (запускается внутри Blender): материалы, примитивы, видимость по сценам."""
import math

import bpy

REG: list = []          # объекты текущей сцены мультфильма (для включения/выключения по кадрам)
_MATS: dict = {}
NOVIS: set = set()      # имена объектов, чью видимость ведёт сама анимация (смена лиц)


def start_scene() -> None:
    REG.clear()
    NOVIS.clear()


def reg(o):
    REG.append(o)
    return o


def material(color, rough=0.6, emit=0.0, metal=0.0):
    key = (tuple(round(float(c), 3) for c in color[:3]), rough, emit, metal)
    if key in _MATS:
        return _MATS[key]
    m = bpy.data.materials.new("m")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    rgba = (float(color[0]), float(color[1]), float(color[2]), 1.0)
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = rough
    if metal:
        b.inputs["Metallic"].default_value = metal
    if emit:
        for n in ("Emission Color", "Emission"):
            if n in b.inputs:
                b.inputs[n].default_value = rgba
                break
        if "Emission Strength" in b.inputs:
            b.inputs["Emission Strength"].default_value = emit
    m.diffuse_color = rgba
    _MATS[key] = m
    return m


def _finish(o, name, loc, scale, rot, color, parent, rough, emit, smooth, metal=0.0):
    o.name = name
    o.location = loc
    o.scale = scale
    o.rotation_euler = rot
    if parent is not None:
        o.parent = parent
    o.data.materials.append(material(color, rough, emit, metal))
    if smooth:
        for p in o.data.polygons:
            p.use_smooth = True
    return reg(o)


def sphere(name, loc=(0, 0, 0), scale=(1, 1, 1), color=(1, 1, 1), parent=None, rough=0.6, emit=0.0,
           rot=(0, 0, 0), seg=20, metal=0.0):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=max(6, seg // 2), radius=1)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, rough, emit, True, metal)


def cyl(name, loc=(0, 0, 0), scale=(1, 1, 1), color=(1, 1, 1), parent=None, rough=0.6, emit=0.0,
        rot=(0, 0, 0), verts=20, smooth=True):
    """Цилиндр радиуса 1 и высоты 1 (масштаб задаёт размеры)."""
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=1, depth=1)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, rough, emit, smooth)


def cone(name, loc=(0, 0, 0), scale=(1, 1, 1), color=(1, 1, 1), parent=None, rough=0.6, emit=0.0,
         rot=(0, 0, 0), top=0.0, verts=20, smooth=True):
    """Конус: низ радиус 1, верх радиус top, высота 1."""
    bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=1, radius2=top, depth=1)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, rough, emit, smooth)


def box(name, loc=(0, 0, 0), scale=(1, 1, 1), color=(1, 1, 1), parent=None, rough=0.7, emit=0.0,
        rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, rough, emit, False)


def plane(name, loc=(0, 0, 0), scale=(1, 1, 1), color=(1, 1, 1), parent=None, rough=0.9, emit=0.0,
          rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_plane_add(size=1)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, rough, emit, False)


def torus(name, loc=(0, 0, 0), major=1.0, minor=0.1, color=(1, 1, 1), parent=None, rot=(0, 0, 0), scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=24, minor_segments=8)
    return _finish(bpy.context.object, name, loc, scale, rot, color, parent, 0.5, 0.0, True)


def empty(name, loc=(0, 0, 0), parent=None, rot=(0, 0, 0)):
    o = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = rot
    if parent is not None:
        o.parent = parent
    return reg(o)


def light(name, kind, loc, rot, energy, color=(1, 1, 1), size=1.0):
    ld = bpy.data.lights.new(name, kind)
    ld.energy = energy
    ld.color = color[:3]
    if kind == "SUN":
        ld.angle = math.radians(8)
    elif kind in ("POINT", "SPOT"):
        ld.shadow_soft_size = size
    elif kind == "AREA":
        ld.size = size
    o = bpy.data.objects.new(name, ld)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = rot
    return reg(o)


def key_visibility(objs, start: int, end: int) -> None:
    """Объекты видны в рендере только в кадрах [start, end]."""
    for o in objs:
        if o.name in NOVIS:
            continue
        o.hide_render = True
        if start > 1:
            o.keyframe_insert("hide_render", frame=start - 1)
        o.hide_render = False
        o.keyframe_insert("hide_render", frame=start)
        o.hide_render = True
        o.keyframe_insert("hide_render", frame=end + 1)


def rng(seed: int):
    """Маленький детерминированный генератор 0..1 (чтобы декорации не менялись между запусками)."""
    state = [seed * 2654435761 % 2 ** 32 or 1]

    def nxt() -> float:
        state[0] = (state[0] * 1664525 + 1013904223) % 2 ** 32
        return state[0] / 2 ** 32
    return nxt
