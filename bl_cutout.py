"""Кукла из PNG-частей (архив EHSON_full и подобные) в Blender: плоскости-детали на пустышках-шарнирах.
Совместима с bl_anim: те же empties (root/hips/torso/head/arm/leg/eye/mouth), плюс apply_extra —
смена рта по громкости, моргание, плоские повороты рук/ног (в плоскости кадра)."""
import math

import bpy

import cutout_layout as L
from bl_common import NOVIS, empty, reg


def _material(img, bright):
    m = bpy.data.materials.new("cut")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.interpolation = "Linear"
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = bright
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], em.inputs["Color"])
    nt.links.new(tex.outputs["Alpha"], mix.inputs[0])
    nt.links.new(tr.outputs["BSDF"], mix.inputs[1])
    nt.links.new(em.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED"), ("shadow_method", "NONE"),
                      ("use_backface_culling", False)):
        try:
            setattr(m, attr, val)
        except Exception:
            pass
    return m


def _plane(name, path, parent, local_xz, height, depth, bright, scale_mul=1.0):
    img = bpy.data.images.load(path, check_existing=True)
    ratio = img.size[0] / max(1, img.size[1])
    bpy.ops.mesh.primitive_plane_add(size=1)
    o = bpy.context.object
    o.name = name
    o.rotation_euler = (math.pi / 2, 0, 0)
    o.scale = (height * ratio * scale_mul, height * scale_mul, 1)
    o.location = (local_xz[0], -depth, local_xz[1])
    o.parent = parent
    o.data.materials.append(_material(img, bright))
    return reg(o)


class CutoutActor:
    kind = "cutout"

    def __init__(self, name):
        self.name = name
        self.root = self.hips = self.torso = self.head = self.mouth = None
        self.arm, self.elbow, self.leg, self.eye, self.brow = {}, {}, {}, {}, {}
        self.tail = None
        self.home = (0.0, 0.0)
        self.yaw = 0.0
        self.scale = 1.0
        self.S = 1.0
        self.hips_base = 0.0
        self.head_base = (0.0, 0.0)
        self.mouth_planes, self.lids = {}, {}
        self.novis_objs = []

    # ключи, специфичные для плоской куклы (вызывается после bl_anim.apply)
    def apply_extra(self, fr, P):
        from bl_anim import key
        S, m, v = self.S, P["mood"], P["open"]
        happy = m in ("joy", "laugh")
        if v < 1.5:
            want = "smile" if happy else "neutral"
        elif happy:
            want = "wide" if v >= 5 else "teeth"
        elif m in ("surprise", "fear") or v < 3:
            want = "open_small"
        else:
            want = "open" if v < 5 else "wide"
        order = {"smile": ["smile", "neutral"], "neutral": ["neutral"], "teeth": ["teeth", "wide", "open"],
                 "wide": ["wide", "open", "teeth"], "open_small": ["open_small", "open"], "open": ["open", "wide"]}[want]
        name = next((k for k in order if k in self.mouth_planes), next(iter(self.mouth_planes), None))
        for k, o in self.mouth_planes.items():
            o.hide_render = k != name
            o.keyframe_insert("hide_render", frame=fr)
        closed = max(0.0, min(1.0, (1.0 - P["eye_sz"]) / 0.94)) if P["eye_sz"] < 0.9 else 0.0
        for s, o in self.lids.items():
            sx, sy, sz = self.lid_scale[s]
            key(o, "scale", fr, (sx, sy, sz * max(0.001, closed)))
        for s in ("L", "R"):
            rx, ry, _ = P["arm"][s]
            sg = 1 if s == "R" else -1
            key(self.arm[s], "rotation_euler", fr, (0, max(-3.0, min(3.0, ry + sg * rx * 0.9)), 0))
            key(self.leg[s], "rotation_euler", fr, (0, P["legs"][s] * 0.6, 0))
        hx, hy, hz = P["head"]
        key(self.head, "rotation_euler", fr, (0, hy + hz * 0.15, 0))
        key(self.head, "location", fr, (self.head_base[0] + hz * 0.25, 0, self.head_base[1] - hx * 0.35))
        key(self.torso, "rotation_euler", fr, (0, P["torso"][2] * 0.4, 0))
        key(self.hips, "location", fr, (0, 0, self.hips_base + (P["hips_z"] - 0.85) / S))


def build(name, parts: dict, x: float, bright: float = 1.0):
    """parts — {имя части: путь к PNG}. Ступни стоят на z=0, лицом к камере (-Y)."""
    a = CutoutActor(name)
    S = L.WORLD_HEIGHT / (L.TOP_Z - L.FOOT_Z)
    a.S, a.home = S, (x, 0.0)
    face = L.load_layout(__import__("pathlib").Path(next(iter(parts.values()))).parent)
    a.root = empty(name + "_root", (x, 0, 0))
    fig = empty(name + "_fig", (0, 0, -L.FOOT_Z * S), a.root)
    fig.scale = (S, S, S)
    D = 6.0                                           # слой глубины (в единицах куклы)
    a.hips = empty(name + "_hips", (0, 0, 0), fig)
    bx, bz = L.PIVOTS["body"]
    a.torso = empty(name + "_torso", (bx, 0, bz), a.hips)

    def piv(key_, parent, parent_xz):
        px, pz = L.PIVOTS[key_]
        e = empty(f"{name}_{key_}", (px - parent_xz[0], 0, pz - parent_xz[1]), parent)
        return e, (px, pz)

    def part(k, parent, parent_xz):
        if k not in parts:
            return
        cx, cz, h, _, layer = L.PARTS[k]
        _plane(f"{name}_{k}", parts[k], parent, (cx - parent_xz[0], cz - parent_xz[1]), h, layer * D, bright)

    for side, k in (("L", "leg_left"), ("R", "leg_right")):
        e, xz = piv(f"leg_{side}", a.hips, (0, 0))
        a.leg[side] = e
        part(k, e, xz)
    part("torso", a.torso, (bx, bz))
    for side, k in (("L", "arm_left"), ("R", "arm_right")):
        e, xz = piv(f"arm_{side}", a.torso, (bx, bz))
        a.arm[side] = e
        a.elbow[side] = empty(f"{name}_elb{side}", (0, 0, 0), e)
        part(k, e, xz)
    hpv, hxz = piv("head", a.torso, (bx, bz))
    a.head = hpv
    a.head_base = (hpv.location[0], hpv.location[2])
    a.hips_base = 0.0
    part("head", hpv, hxz)

    # лицо: части лежат на голове (центр головы -> доли картинки)
    cx, cz, hh, _, hl = L.PARTS["head"]
    ratio = 350 / 280
    head_img = bpy.data.images.load(parts["head"], check_existing=True)
    ratio = head_img.size[0] / max(1, head_img.size[1])
    px_unit = hh / head_img.size[1]                   # единиц раскладки на пиксель головы

    def at(fx, fy):
        return (cx + (fx - .5) * hh * ratio - hxz[0], cz + (.5 - fy) * hh - hxz[1])

    mk = face["mouth_scale"]
    mx, mz = at(*face["mouth"])
    for mn in L.MOUTHS:
        p = parts.get(f"mouth_{mn}")
        if not p:
            continue
        mi = bpy.data.images.load(p, check_existing=True)
        o = _plane(f"{name}_mouth_{mn}", p, hpv, (mx, mz), mi.size[1] * px_unit * mk, (hl + 0.04) * D, bright)
        o.hide_render = mn != "neutral"
        a.mouth_planes[mn] = o
        NOVIS.add(o.name)
        a.novis_objs.append(o)
    a.lid_scale = {}
    ek = face["eye_scale"]
    for side, fkey in (("L", "eye_L"), ("R", "eye_R")):
        p = parts.get("eye_closed_left" if side == "L" else "eye_closed_right")
        if not p:
            continue
        ei = bpy.data.images.load(p, check_existing=True)
        ex, ez = at(*face[fkey])
        o = _plane(f"{name}_lid{side}", p, hpv, (ex, ez), ei.size[1] * px_unit * ek, (hl + 0.05) * D, bright)
        a.lid_scale[side] = tuple(o.scale)
        o.scale = (o.scale[0], o.scale[1], 0.001)
        a.lids[side] = o
    # пустышки-«заглушки» для общего аниматора (эти ключи в кукле ни на что не влияют)
    a.mouth = empty(name + "_mouth", (0, 0, 0), hpv)
    for side in ("L", "R"):
        a.eye[side] = empty(f"{name}_eye{side}", (0, 0, 0), hpv)
        a.brow[side] = empty(f"{name}_brow{side}", (0, 0, 0), hpv)
    return a
