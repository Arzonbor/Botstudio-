"""Процедурные 3D-персонажи: Blender «рисует» героя по описанию из сценария (scenario.infer_avatar).
Человек, животное (кот, собака, заяц, медведь, лиса, волк, мышь, свинья, лев, лягушка), робот.
Скелет — иерархия пустышек (жёсткие части), без авто-весов: быстро и без сюрпризов при рендере."""
import math

from bl_common import box, cone, cyl, empty, sphere, torus

C = lambda v: tuple(v[:3])
WHITE, BLACK = (0.97, 0.97, 0.97), (0.03, 0.03, 0.04)
MOUTH = (0.35, 0.05, 0.07)

# вид -> (уши, морда, хвост)
ANIMAL = {
    "cat": ("pointy", "small", "long"), "dog": ("floppy", "long", "thick"), "rabbit": ("tall", "small", "puff"),
    "bear": ("round", "medium", "stub"), "fox": ("pointy_tall", "long", "bushy"), "wolf": ("pointy", "long", "bushy"),
    "mouse": ("round_big", "small", "long"), "pig": ("floppy_small", "pig", "curl"),
    "lion": ("round", "medium", "tuft"), "frog": ("none", "wide", "none"),
}


class Actor:
    """Один герой сцены: набор пустышек для анимации + служебные данные."""

    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.root = self.hips = self.torso = self.head = None
        self.arm = {}
        self.elbow = {}
        self.leg = {}
        self.eye = {}
        self.brow = {}
        self.mouth = None
        self.tail = None
        self.base_z = 0.0
        self.home = (0.0, 0.0)
        self.yaw = 0.0
        self.scale = 1.0


def _hat(a, spec, head, hair):
    hat = spec["hat"]
    if hat == "none":
        return
    col = {"tophat": (0.08, 0.08, 0.1), "cap": (0.15, 0.25, 0.6), "crown": (0.95, 0.78, 0.15),
           "wizard": (0.25, 0.2, 0.6), "chef": WHITE, "santa": (0.8, 0.05, 0.05), "pirate": (0.08, 0.08, 0.1),
           "helmet": (0.6, 0.62, 0.66), "hardhat": (0.97, 0.75, 0.1)}.get(hat, (0.5, 0.5, 0.5))
    if hat == "tophat":
        cyl("brim", (0, 0, .45), (.34, .34, .03), col, head)
        cyl("hat", (0, 0, .6), (.2, .2, .3), col, head)
    elif hat == "cap":
        sphere("cap", (0, 0.01, .36), (.29, .28, .17), col, head)
        box("visor", (0, -.27, .38), (.22, .2, .025), col, head)
    elif hat == "crown":
        cyl("crown", (0, 0, .5), (.2, .2, .08), col, head, rough=.3)
        for i in range(5):
            t = i / 5 * math.tau
            cone("spike", (math.cos(t) * .19, math.sin(t) * .19, .6), (.04, .04, .1), col, head, rough=.3)
    elif hat == "wizard":
        cone("brim", (0, 0, .43), (.42, .42, .03), col, head, verts=24)
        cone("wiz", (0, 0, .75), (.26, .26, .6), col, head, verts=24)
        sphere("star", (0, -.1, .6), (.04, .01, .04), (1, .9, .2), head, emit=2)
    elif hat == "chef":
        cyl("chefb", (0, 0, .48), (.24, .24, .1), col, head)
        sphere("chef", (0, 0, .64), (.3, .3, .2), col, head)
    elif hat == "santa":
        cone("santa", (0, 0.02, .62), (.28, .28, .4), col, head, top=.05)
        sphere("pom", (0, 0.02, .85), (.07, .07, .07), WHITE, head)
        cyl("fur", (0, 0, .45), (.3, .3, .05), WHITE, head)
    elif hat == "pirate":
        sphere("pirate", (0, 0, .42), (.4, .3, .09), col, head)
        sphere("skull", (0, -.2, .43), (.05, .01, .05), WHITE, head)
    elif hat in ("helmet", "hardhat"):
        sphere("helm", (0, 0.01, .3), (.3, .29, .26), col, head, rough=.3)
        if hat == "hardhat":
            box("hbrim", (0, -.2, .36), (.4, .2, .02), col, head)


def _hair(spec, head, col):
    st = spec["hair_style"]
    if st == "bald":
        return
    sphere("hair", (0, .045, .27), (.292, .27, .27), col, head, rough=.8)
    if st in ("short", "curly", "bun", "ponytail", "braids", "long"):
        sphere("fringe", (0, -.14, .43), (.24, .12, .09), col, head, rough=.8)
    if st == "curly":
        for i in range(10):
            t = i / 10 * math.tau
            sphere("curl", (math.cos(t) * .24, .04 + math.sin(t) * .1, .36 + .05 * (i % 2)), (.09, .09, .09), col, head)
    elif st == "long":
        sphere("back", (0, .14, 0.02), (.3, .13, .4), col, head, rough=.8)
    elif st == "bun":
        sphere("bun", (0, .08, .56), (.11, .11, .11), col, head)
    elif st == "ponytail":
        sphere("pony", (0, .27, .3), (.07, .07, .07), col, head)
        cone("tail", (0, .3, .1), (.09, .09, .4), col, head, rot=(math.radians(10), 0, 0), top=.02)
    elif st == "braids":
        for s in (-1, 1):
            for k in range(3):
                sphere("braid", (s * .25, .04, .12 - k * .13), (.065, .065, .075), col, head)
            sphere("bow", (s * .25, 0, -.28), (.06, .04, .05), (.9, .2, .35), head)


def _beard(spec, head, col):
    b = spec["beard"]
    if b == "short":
        sphere("beard", (0, -.13, .02), (.2, .14, .14), col, head, rough=.9)
    elif b == "long":
        sphere("beard", (0, -.12, .0), (.21, .14, .15), col, head, rough=.9)
        cone("beardl", (0, -.13, -.2), (.17, .1, .38), col, head, rot=(math.pi, 0, 0), top=.02)
    if b in ("short", "long", "mustache"):
        sphere("must", (0, -.245, .135), (.11, .03, .028), col, head, rough=.9)


def _glasses(head):
    for s in (-1, 1):
        torus("glass", (s * .1, -.262, .24), .065, .01, (.05, .05, .05), head, rot=(math.pi / 2, 0, 0))
    box("bridge", (0, -.262, .245), (.07, .012, .012), (.05, .05, .05), head)


def build(name, spec, pose_x=0.0, yaw=0.0):
    """Строит героя. Нога стоит на z=0, глаза смотрят в -Y (на камеру). Возвращает Actor."""
    a = Actor(name, spec)
    animal = spec["species"] != "human"
    robot = spec.get("robot")
    skin = (0.7, 0.72, 0.78) if robot else C(spec.get("fur") or spec["skin"])
    body_fur = C(spec["fur"]) if animal else skin
    hair = C(spec["hair_color"])
    top, bottom, shoes = C(spec["top"]), C(spec["bottom"]), C(spec["shoes"])
    metal = 0.8 if robot else 0.0
    a.scale = float(spec.get("height", 1.0))
    a.home, a.yaw = (pose_x, 0.0), yaw

    a.root = empty(name + "_root", (pose_x, 0, 0), None, (0, 0, yaw))
    a.root.scale = (a.scale,) * 3
    a.hips = empty(name + "_hips", (0, 0, .85), a.root)
    a.torso = empty(name + "_torso", (0, 0, .0), a.hips)

    # туловище и одежда
    dress = spec["outfit"] == "dress" and not animal
    suit = spec["outfit"] in ("suit", "coat")
    tcol = top
    sphere("chest", (0, 0, .28), (.23, .15, .3), tcol if not (animal and spec["outfit"] == "casual" and False) else body_fur,
           a.torso, rough=.7, metal=metal)
    sphere("belly", (0, 0, .02), (.21, .14, .2), tcol if not dress else tcol, a.torso, rough=.7)
    if dress:
        cone("skirt", (0, 0, -.22), (.36, .28, .52), tcol, a.hips, top=.2, verts=28)
    elif not animal:
        for s in (-1, 1):
            cyl("pant", (s * .115, 0, -.34), (.085, .085, .5), bottom, a.hips)
    if suit:
        box("lapel", (0, -.145, .3), (.04, .02, .4), WHITE, a.torso)
        sphere("tie", (0, -.15, .24), (.03, .01, .1), (.7, .1, .12), a.torso)
    if spec["outfit"] == "coat" and spec["top"][0] > .9:
        for k in range(3):
            sphere("btn", (0, -.15, .38 - k * .13), (.018, .01, .018), (.3, .3, .3), a.torso)
    if spec.get("accessory") == "stethoscope":
        torus("steth", (0, -.14, .42), .1, .012, (.15, .15, .15), a.torso, rot=(math.pi / 2, 0, 0))
    if spec.get("wings"):
        for s in (-1, 1):
            sphere("wing", (s * .3, .17, .45), (.22, .02, .3), (.8, .9, 1), a.torso, emit=.6,
                   rot=(0, s * .3, s * .5))

    # ноги
    for s, side in ((-1, "L"), (1, "R")):
        pv = empty(f"{name}_leg{side}", (s * .115, 0, -.02), a.hips)
        cyl("leg", (0, 0, -.38), (.075, .075, .72), body_fur if animal else (bottom if not dress else skin), pv)
        sphere("shoe", (0, -.05, -.76), (.09, .15, .075), shoes if not animal else body_fur, pv)
        a.leg[side] = pv

    # руки: плечо -> локоть
    for s, side in ((-1, "L"), (1, "R")):
        sh = empty(f"{name}_arm{side}", (s * .27, 0, .5), a.torso, (0, -s * .12, 0))
        cyl("uarm", (0, 0, -.16), (.06, .06, .33), tcol if not robot else skin, sh)
        el = empty(f"{name}_elb{side}", (0, 0, -.33), sh)
        cyl("farm", (0, 0, -.16), (.052, .052, .31), body_fur if animal else skin, el)
        sphere("hand", (0, 0, -.34), (.07, .06, .075), body_fur if animal else skin, el)
        a.arm[side], a.elbow[side] = sh, el

    # шея и голова
    neck = empty(name + "_neck", (0, 0, .62), a.torso)
    cyl("neck", (0, 0, .0), (.07, .07, .12), skin, neck)
    a.head = empty(name + "_head", (0, 0, .1), neck)
    h = a.head
    sphere("skull", (0, 0, .2), (.27, .25, .29), skin, h, rough=.55, metal=metal)
    if spec["species"] == "lion":
        sphere("mane", (0, .06, .2), (.38, .22, .38), (.55, .3, .1), h, rough=.9)

    # лицо
    snout_kind = ANIMAL.get(spec["species"], ("", "", ""))[1] if animal else ""
    my, mz = -.228, .09
    if animal:
        sn = {"small": (.1, .09, .075), "medium": (.13, .12, .09), "long": (.11, .17, .08),
              "pig": (.14, .1, .1), "wide": (.2, .1, .06)}.get(snout_kind, (.1, .1, .08))
        sphere("snout", (0, -.2, .1), sn, (.96, .88, .8) if spec["species"] in ("dog", "cat", "fox", "wolf", "mouse") else skin, h)
        sphere("nose", (0, -.2 - sn[1] * .9, .14), (.04, .03, .03), (.08, .05, .05) if spec["species"] != "pig" else (.85, .45, .5), h, rough=.3)
        my, mz = -.2 - sn[1] * .75, .06
    else:
        sphere("nose", (0, -.255, .17), (.035, .04, .035), skin if not robot else (.5, .5, .55), h)
        if not robot:
            for s in (-1, 1):
                sphere("cheek", (s * .15, -.2, .1), (.05, .02, .035), (min(1, skin[0] + .05), skin[1] * .75, skin[2] * .75), h)
    a.mouth = empty(name + "_mouth", (0, my, mz), h)
    sphere("mouth", (0, 0, 0), (.065, .022, .012), MOUTH, a.mouth, rough=.4)
    sphere("tongue", (0, -.004, -.004), (.04, .014, .006), (.85, .3, .35), a.mouth)
    eye_c = C(spec["eye_color"])
    ez = .27 if spec["species"] != "frog" else .43
    for s, side in ((-1, "L"), (1, "R")):
        e = empty(f"{name}_eye{side}", (s * .1, -.2 if spec["species"] != "frog" else -.12, ez), h)
        sphere("eyeball", (0, 0, 0), (.078, .07, .085), WHITE, e, rough=.2)
        sphere("iris", (0, -.055, 0), (.045, .02, .052), eye_c, e, rough=.2)
        sphere("pupil", (0, -.07, 0), (.022, .01, .028), BLACK, e, rough=.1)
        sphere("glint", (s * -.012, -.08, .018), (.009, .005, .009), WHITE, e, emit=3)
        a.eye[side] = e
        br = empty(f"{name}_brow{side}", (s * .1, -.225, ez + .1), h)
        if not (animal and spec["species"] in ("frog",)):
            box("brow", (0, 0, 0), (.1, .022, .02), hair if not animal else tuple(c * .6 for c in body_fur), br, rough=.9)
        a.brow[side] = br

    # волосы / борода / шляпа / очки
    if not animal:
        _hair(spec, h, hair)
        _beard(spec, h, hair)
    _hat(a, spec, h, hair)
    if spec.get("accessory") == "glasses":
        _glasses(h)
    elif spec.get("accessory") == "eyepatch":
        sphere("patch", (.1, -.235, .27), (.09, .02, .09), BLACK, h)
    if robot:
        cyl("ant", (0, 0, .58), (.012, .012, .15), (.4, .4, .45), h)
        sphere("antb", (0, 0, .67), (.04, .04, .04), (1, .2, .2), h, emit=3)

    # уши, хвост животных
    if animal:
        kind, _, tail = ANIMAL[spec["species"]]
        inner = tuple(min(1, c * 1.15 + .15) for c in body_fur)
        for s in (-1, 1):
            if kind in ("pointy", "pointy_tall"):
                k = 1.5 if kind == "pointy_tall" else 1.0
                cone("ear", (s * .17, .02, .46), (.1, .05, .18 * k), skin, h, rot=(0, s * .25, 0), top=.01)
                cone("earin", (s * .17, -.02, .45), (.06, .03, .12 * k), inner, h, rot=(0, s * .25, 0), top=.01)
            elif kind == "floppy":
                sphere("ear", (s * .27, .03, .3), (.06, .1, .17), tuple(c * .7 for c in body_fur), h, rot=(0, s * .25, 0))
            elif kind == "floppy_small":
                cone("ear", (s * .17, -.03, .46), (.09, .03, .1), skin, h, rot=(.5, s * .4, 0), top=.01)
            elif kind == "tall":
                sphere("ear", (s * .1, .03, .66), (.055, .03, .24), skin, h, rot=(0, s * .12, 0))
                sphere("earin", (s * .1, -.0, .66), (.03, .012, .18), inner, h, rot=(0, s * .12, 0))
            elif kind in ("round", "round_big"):
                k = 1.6 if kind == "round_big" else 1.0
                sphere("ear", (s * .2, .03, .45), (.09 * k, .04, .09 * k), skin, h)
                sphere("earin", (s * .2, -.005, .45), (.055 * k, .02, .055 * k), inner, h)
        td = {"long": (0, .2, -.03, .03, .35), "thick": (0, .2, .04, .05, .3), "puff": (0, .2, -.02, .09, .09),
              "stub": (0, .17, -.02, .06, .08), "bushy": (0, .22, .02, .09, .38), "curl": (0, .17, .0, .035, .1),
              "tuft": (0, .22, -.02, .035, .35)}.get(tail)
        if td:
            a.tail = empty(name + "_tail", (0, .17, -.05), a.hips)
            if tail in ("puff", "stub", "curl"):
                sphere("tail", (0, .06, 0), (td[3], td[3], td[3]), WHITE if tail == "puff" else skin, a.tail)
            else:
                cone("tail", (0, .1, .12), (td[3], td[3], td[4]), skin, a.tail, rot=(-.9, 0, 0), top=.012)
                if tail == "bushy":
                    sphere("tip", (0, .28, .3), (.07, .07, .07), WHITE, a.tail)
    return a
