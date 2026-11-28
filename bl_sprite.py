"""Загруженные 2D-персонажи (лист из ChatGPT): плоскость с текстурой + скелет + смена лиц.
Если авто-скелет не получился — персонаж двигается целиком (покачивание, «дыхание»)."""
import math

import bpy
from mathutils import Vector

from bl_common import NOVIS, reg

FACE = {"mouth": (0.5, 0.80), "eye_l": (0.44, 0.87), "eye_r": (0.56, 0.87)}


class SpriteActor:
    kind = "sprite"

    def __init__(self, name):
        self.name = name
        self.home = (0.0, 0.0)
        self.yaw = 0.0
        self.scale = 1.0
        self.ob = self.arm = self.mouth = None
        self.lids = ()
        self.sprites = None
        self.h = self.w = 2.0


def _textured(img, name, loc, scale, rot, subdiv=False, vis_alpha=True):
    if subdiv:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=18, size=1)
    else:
        bpy.ops.mesh.primitive_plane_add(size=1)
    ob = bpy.context.object
    ob.name = name
    ob.rotation_euler = rot
    ob.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    ob.location = loc
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    b = nt.nodes["Principled BSDF"]
    nt.links.new(tex.outputs["Color"], b.inputs["Base Color"])
    nt.links.new(tex.outputs["Alpha"], b.inputs["Alpha"])
    b.inputs["Roughness"].default_value = 1.0
    for n in ("Emission Color", "Emission"):
        if n in b.inputs:
            nt.links.new(tex.outputs["Color"], b.inputs[n])
            if "Emission Strength" in b.inputs:
                b.inputs["Emission Strength"].default_value = 0.25
            break
    for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED"), ("shadow_method", "NONE")):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    ob.data.materials.append(mat)
    return reg(ob)


def _rig(a):
    ob, h, w = a.ob, a.h, a.w
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.object.armature_add(location=ob.location)
    arm = bpy.context.object
    arm.name = a.name + "_rig"
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    eb.remove(eb[0])
    z0 = ob.location.z - h / 2

    def bone(nm, head, tail, parent=None):
        b = eb.new(nm)
        b.head, b.tail = Vector(head), Vector(tail)
        if parent:
            b.parent = eb[parent]
        return b
    X = lambda f: ob.location.x + (f - 0.5) * w
    Z = lambda f: z0 + f * h
    bone("hips", (X(.5), 0, Z(.45)), (X(.5), 0, Z(.55)))
    bone("spine", (X(.5), 0, Z(.55)), (X(.5), 0, Z(.72)), "hips")
    bone("neck", (X(.5), 0, Z(.72)), (X(.5), 0, Z(.78)), "spine")
    bone("head", (X(.5), 0, Z(.78)), (X(.5), 0, Z(.97)), "neck")
    for side, s in (("L", -1), ("R", 1)):
        bone(f"arm_{side}", (X(.5 + s * .08), 0, Z(.70)), (X(.5 + s * .25), 0, Z(.55)), "spine")
        bone(f"forearm_{side}", (X(.5 + s * .25), 0, Z(.55)), (X(.5 + s * .33), 0, Z(.42)), f"arm_{side}")
        bone(f"leg_{side}", (X(.5 + s * .05), 0, Z(.45)), (X(.5 + s * .06), 0, Z(.22)), "hips")
        bone(f"shin_{side}", (X(.5 + s * .06), 0, Z(.22)), (X(.5 + s * .06), 0, Z(.02)), f"leg_{side}")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    ob.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    reg(arm)
    return arm


def build(name, png, meta, x, scale=1.0):
    """Плоскость персонажа высотой ~2 м (под размер 3D-героев) и лицо/рот."""
    a = SpriteActor(name)
    img = bpy.data.images.load(png)
    ratio = img.size[0] / max(1, img.size[1])
    a.h = 2.1 * scale
    a.w = a.h * ratio
    a.home = (x, 0.0)
    a.ob = _textured(img, name, (x, 0.0, a.h / 2), (a.w, a.h, 1), (math.pi / 2, 0, 0), subdiv=True)
    try:
        a.arm = _rig(a)
    except Exception as e:                                    # без скелета — двигаем плоскость целиком
        print("rig failed:", e)
        a.arm = None
    meta = meta or {}
    z0 = a.ob.location.z - a.h / 2
    if meta.get("faces"):
        x0, yb, x1, yt = meta["face_rect"]
        cx, cz = (x0 + x1) / 2, (yb + yt) / 2
        fw = (x1 - x0) * a.w * 1.25
        a.sprites = {}
        for idx in (0, 3, 4, 5, 8):
            if idx >= len(meta["faces"]):
                continue
            fimg = bpy.data.images.load(meta["faces"][idx])
            r = fimg.size[0] / max(1, fimg.size[1])
            o = _textured(fimg, f"face_{idx}", (x + (cx - .5) * a.w, -0.03 - idx * 0.001, z0 + cz * a.h - fw / r * 0.05),
                          (fw, fw / r, 1), (math.pi / 2, 0, 0))
            a.sprites[idx] = o
            NOVIS.add(o.name)
    else:
        def part(nm, rel, sx, sy, color):
            bpy.ops.mesh.primitive_circle_add(vertices=24, fill_type="NGON", radius=1, rotation=(math.pi / 2, 0, 0))
            o = bpy.context.object
            o.name = nm
            o.location = (x + (rel[0] - .5) * a.w, -0.02, z0 + rel[1] * a.h)
            o.scale = (sx, 1, sy)
            m = bpy.data.materials.new(nm)
            m.use_nodes = True
            m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = color
            o.data.materials.append(m)
            return reg(o)
        a.mouth = part("mouth", FACE["mouth"], .07, .005, (.25, .05, .05, 1))
        a.lids = (part("lid_l", FACE["eye_l"], .05, .0, (.9, .75, .6, 1)), part("lid_r", FACE["eye_r"], .05, .0, (.9, .75, .6, 1)))
    return a


def animate_span(a, f0, n, amp, mood, energy, speaking):
    """Дыхание, жесты, липсинк. Ключи через кадр."""
    quiet = {"joy": 3, "laugh": 3, "surprise": 5, "sad": 8}.get(mood, 0)
    pb = None
    if a.arm is not None:
        pb = a.arm.pose.bones
        for b in pb:
            b.rotation_mode = "XYZ"
    for f in range(0, n, 2):
        fr = f0 + f + 1
        t = fr / 24.0
        v = amp[min(f, len(amp) - 1)] if (speaking and amp) else 0.0
        g = (0.3 + v) * energy if speaking else 0.1
        if pb is not None:
            pb["spine"].rotation_euler = (0, math.sin(t * 2) * .02, 0)
            pb["head"].rotation_euler = (0, math.sin(t * 3) * .05 * (1 + v), 0)
            for side, ph in (("L", 0), ("R", 1.5)):
                pb[f"arm_{side}"].rotation_euler = (0, math.sin(t * 2.5 + ph) * .3 * g, 0)
            for nm in ("spine", "head", "arm_L", "arm_R"):
                pb[nm].keyframe_insert("rotation_euler", frame=fr)
        else:
            a.ob.rotation_euler = (math.pi / 2, math.sin(t * 2) * .02 * (1 + g), 0)
            a.ob.keyframe_insert("rotation_euler", frame=fr)
        if a.sprites is not None:
            show = 4 if v > .5 else (3 if v > .25 and quiet in (0, 3) else quiet)
            if show not in a.sprites:
                show = 0 if 0 in a.sprites else next(iter(a.sprites))
            for idx, o in a.sprites.items():
                o.hide_render = idx != show
                o.keyframe_insert("hide_render", frame=fr)
        elif a.mouth is not None:
            a.mouth.scale[2] = .005 + v * .045
            a.mouth.keyframe_insert("scale", frame=fr)
            blink = ((t + 1) % 3.3) < .15
            for lid in a.lids:
                lid.scale[2] = .05 if blink else 0.0
                lid.keyframe_insert("scale", frame=fr)
