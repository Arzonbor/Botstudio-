"""3D-декорации по месту действия (лес, комната, город, пляж, космос…), небо и свет по времени суток."""
import math

from bl_common import box, cone, cyl, light, plane, rng, sphere

SKY = {"morning": (0.95, 0.72, 0.55), "day": (0.42, 0.7, 0.98), "evening": (0.95, 0.5, 0.32), "night": (0.02, 0.03, 0.09)}
# энергия солнца, цвет солнца, угол высоты, энергия заполняющего света
SUN = {"morning": (3.0, (1.0, 0.8, 0.6), 22, 0.9), "day": (4.2, (1.0, 0.96, 0.9), 55, 1.1),
       "evening": (3.0, (1.0, 0.6, 0.38), 12, 0.8), "night": (0.9, (0.45, 0.55, 1.0), 40, 0.5)}
GROUND = {"forest": (0.2, 0.5, 0.17), "park": (0.3, 0.62, 0.2), "village": (0.35, 0.55, 0.2), "farm": (0.4, 0.55, 0.2),
          "beach": (0.93, 0.82, 0.55), "desert": (0.9, 0.7, 0.4), "snow": (0.95, 0.97, 1.0), "mountain": (0.4, 0.5, 0.3),
          "city": (0.25, 0.25, 0.28), "space": (0.5, 0.5, 0.55), "room": (0.65, 0.45, 0.28), "kitchen": (0.85, 0.82, 0.75),
          "school": (0.7, 0.55, 0.35), "office": (0.45, 0.45, 0.5), "castle": (0.55, 0.53, 0.5)}
INDOOR = {"room", "kitchen", "school", "office", "castle"}


def _tree(r, x, y, s=1.0, snow=False, pine=False):
    cyl("trunk", (x, y, .7 * s), (.14 * s, .14 * s, 1.4 * s), (.38, .24, .12))
    if pine:
        for i in range(3):
            cone("pine", (x, y, (1.5 + i * .75) * s), ((1.1 - i * .3) * s, (1.1 - i * .3) * s, 1.1 * s),
                 (.9, .95, 1.0) if snow else (.1, .38, .2))
    else:
        for dx, dy, dz, k in ((0, 0, 2.2, 1.0), (.5, .2, 1.9, .7), (-.5, -.1, 1.95, .75)):
            sphere("leaf", (x + dx * s, y + dy * s, dz * s), (1.0 * k * s,) * 3, (.14 + .1 * k * .2, .5 + .1 * k * .2, .15))


def _house(x, y, s=1.0, col=(.85, .75, .55), roof=(.7, .25, .2)):
    box("wall", (x, y, 1.0 * s), (2.6 * s, 2.2 * s, 2.0 * s), col)
    cone("roof", (x, y, 2.55 * s), (2.1 * s, 1.8 * s, 1.1 * s), roof, rot=(0, 0, math.pi / 4), verts=4)
    box("door", (x, y - 1.11 * s, .7 * s), (.55 * s, .04, 1.4 * s), (.4, .24, .12))
    box("win", (x + .85 * s, y - 1.11 * s, 1.2 * s), (.5 * s, .04, .5 * s), (.7, .85, 1), emit=.8)


def _room(env, kitchen=False, school=False, office=False, castle=False):
    wall = (.88, .82, .7) if not castle else (.55, .52, .5)
    if school:
        wall = (.75, .85, .8)
    box("back", (0, 3.2, 2.0), (16, .2, 4.0), wall, rough=.9)
    box("left", (-7, 0, 2.0), (.2, 14, 4.0), wall, rough=.9)
    box("right", (7, 0, 2.0), (.2, 14, 4.0), wall, rough=.9)
    box("ceil", (0, 0, 4.0), (16, 14, .2), (.95, .95, .95))
    box("skirt", (0, 3.08, .12), (16, .06, .24), (.4, .3, .2))
    day = env["time"] != "night"
    box("window", (-3.2, 3.08, 2.3), (2.4, .05, 1.6), (.55, .75, 1.0) if day else (.05, .07, .2), emit=1.6 if day else .4)
    for s in (-1, 1):
        box("cur", (-3.2 + s * 1.4, 3.0, 2.3), (.35, .08, 1.9), (.75, .25, .3))
    if school:
        box("board", (2.2, 3.08, 2.1), (4, .05, 1.6), (.1, .25, .18), rough=.9)
        for i in (-1, 1):
            box("desk", (i * 3.5, 1.6, .55), (1.4, .8, .08), (.75, .55, .3))
            box("dlegs", (i * 3.5, 1.6, .27), (1.2, .6, .5), (.3, .3, .35))
    elif kitchen:
        box("counter", (3.5, 2.5, .5), (5, .9, 1.0), (.85, .85, .9))
        box("top", (3.5, 2.5, 1.02), (5.1, 1.0, .06), (.3, .3, .32))
        box("fridge", (6.2, 2.6, 1.0), (1.0, .9, 2.0), (.92, .92, .95), rough=.3)
        cyl("pot", (2.5, 2.5, 1.15), (.25, .25, .22), (.5, .5, .55))
    elif office:
        box("desk", (3.5, 1.9, .75), (3, 1.2, .08), (.55, .4, .25))
        box("monitor", (3.5, 2.1, 1.15), (.9, .06, .55), (.05, .08, .12), emit=.3)
        box("shelf", (-5.8, 2.9, 1.8), (2.5, .35, 2.6), (.5, .35, .2))
    elif castle:
        for x in (-5, 5):
            cyl("pillar", (x, 2.7, 2.0), (.45, .45, 4), (.7, .68, .65))
        box("carpet", (0, -.5, .02), (3, 9, .03), (.65, .1, .15))
        box("banner", (2.5, 3.0, 2.6), (1.0, .05, 2.2), (.6, .1, .2))
    else:
        box("rug", (0, 0.2, .02), (5, 3.4, .03), (.3, .45, .65))
        box("shelf", (4.8, 3.0, 1.2), (2.2, .4, 2.4), (.5, .35, .2))
        box("sofa", (-4.5, 1.8, .5), (2.6, 1.0, 1.0), (.55, .3, .35))
        box("table", (1, 2.1, .4), (1.4, .8, .8), (.6, .42, .25))
        sphere("lamp", (4.8, 2.6, 2.7), (.28, .28, .28), (1, .9, .6), emit=6)
        box("pic", (0.5, 3.07, 2.5), (1.2, .04, .8), (.9, .75, .3))


def _outdoors(place, env, R):
    day = env["time"] != "night"
    if place in ("forest", "park", "village", "farm", "mountain"):
        pine = place == "mountain"
        for i in range(26 if place == "forest" else 12):
            x = (R() - .5) * 46
            y = 4 + R() * 22 if abs(x) < 8 else 1.5 + R() * 24
            _tree(1, x, y, .8 + R() * .6, pine=pine)
        for i in range(30):
            sphere("flower", ((R() - .5) * 16, 1 + R() * 6, .08), (.07, .07, .07),
                   [(.95, .3, .35), (.98, .85, .2), (.95, .95, .95), (.7, .4, .9)][i % 4])
        if place in ("forest", "park"):
            box("path", (0, 1.5, .01), (4.0, 40, .02), (.65, .5, .3), rough=1)
        if place == "park":
            box("bench", (5, 3.5, .45), (1.8, .5, .1), (.5, .3, .15))
            box("benchb", (5, 3.75, .8), (1.8, .08, .5), (.5, .3, .15))
        if place == "village":
            for x, y in ((-7, 6), (6, 8), (12, 12)):
                _house(x, y, 1.2)
            box("fence", (0, 4.6, .5), (22, .08, .08), (.6, .45, .25))
        if place == "farm":
            box("barn", (-8, 10, 2.2), (5, 4, 4.4), (.7, .15, .12))
            cone("broof", (-8, 10, 5.0), (4.2, 3.2, 1.6), (.35, .12, .1), rot=(0, 0, math.pi / 4), verts=4)
            sphere("hay", (7, 6, .8), (1.4, 1.2, .9), (.9, .78, .3))
        if place == "mountain":
            for i in range(5):
                cone("mt", (-24 + i * 12, 38, 6), (9 + R() * 4, 9, 12 + R() * 6), (.45, .45, .5), verts=7)
                cone("cap", (-24 + i * 12, 38, 10.5), (3.6, 3.6, 4.6), (.97, .97, 1), verts=7)
    elif place == "beach":
        plane("sea", (0, 30, .02), (200, 50, 1), (.12, .5, .75), rough=.1, emit=.15)
        plane("foam", (0, 5.2, .015), (200, .8, 1), (.95, .98, 1), emit=.4)
        for x in (-6, 7):
            cyl("palm", (x, 7, 1.8), (.16, .16, 3.6), (.45, .3, .15), rot=(0, .15 * (1 if x > 0 else -1), 0))
            for k in range(6):
                cone("frond", (x + math.cos(k) * .9, 7 + math.sin(k) * .9, 3.7), (.3, .9, .05), (.15, .55, .2),
                     rot=(1.4, 0, k), verts=6)
        sphere("shell", (-2, 2.5, .05), (.12, .08, .05), (.95, .75, .7))
    elif place == "desert":
        for i in range(7):
            sphere("dune", (-30 + i * 10, 18 + R() * 14, -1.5), (9, 6, 3), (.92, .72, .42), rough=1)
        for x in (-5, 6, 11):
            cyl("cactus", (x, 6 + R() * 4, 1.0), (.25, .25, 2.0), (.2, .55, .25))
            cyl("arm", (x + .5, 6, 1.4), (.15, .15, .8), (.2, .55, .25))
    elif place == "snow":
        for i in range(16):
            _tree(1, (R() - .5) * 40, 3 + R() * 20, .9 + R() * .5, snow=True, pine=True)
        for k, z in enumerate((.45, 1.1, 1.65)):
            sphere("snowman", (-4.5, 3.5, z), (.55 - k * .12,) * 3, (.97, .97, 1.0))
        cone("carrot", (-4.5, 3.0, 1.7), (.05, .05, .3), (.95, .5, .1), rot=(math.pi / 2, 0, 0))
    elif place == "city":
        for i in range(14):
            x = -30 + i * 4.6
            h = 5 + R() * 12
            yb = 14 + R() * 4
            col = [(.55, .6, .7), (.7, .55, .5), (.5, .55, .6), (.75, .7, .6)][i % 4]
            box("bld", (x, yb, h / 2), (3.8, 3.5, h), col)
            for r in range(int(h // 2.2)):
                for c in (-1, 0, 1):
                    if R() > .35:
                        box("w", (x + c * 1.1, yb - 1.78, 1.5 + r * 2.0), (.55, .05, .8),
                            (1, .85, .45) if not day else (.6, .8, .95), emit=1.5 if not day else .3)
        box("road", (0, 3, .01), (80, 6, .02), (.12, .12, .14), rough=.9)
        for x in range(-30, 31, 4):
            box("dash", (x, 3, .02), (1.4, .15, .01), (.95, .95, .95))
        box("walk", (0, 7.4, .05), (80, 2.4, .1), (.62, .62, .62))
        for x in (-8, 0, 8):
            cyl("lamp", (x, 6.6, 2.2), (.07, .07, 4.4), (.2, .2, .22))
            sphere("bulb", (x, 6.6, 4.5), (.22, .22, .22), (1, .95, .7), emit=8 if not day else 1)
    elif place == "space":
        for i in range(160):
            a, b = R() * math.tau, R() * 1.1
            sphere("star", (math.cos(a) * 55, 30 + R() * 20, 3 + b * 25), (.09, .09, .09), (1, 1, 1), emit=6, seg=6)
        sphere("planet", (14, 40, 12), (8, 8, 8), (.85, .45, .25), rough=.9)
        sphere("planet2", (-18, 45, 6), (4, 4, 4), (.3, .55, .8), rough=.9)
        sphere("earth", (-6, 60, 22), (9, 9, 9), (.15, .4, .75), rough=.7)
        for i in range(8):
            sphere("crater", ((R() - .5) * 14, 2 + R() * 8, .0), (.6 + R(), .6 + R(), .15), (.4, .4, .44), rough=1)
    if place not in ("space", "beach", "city"):
        for i in range(10):
            sphere("bush", ((R() - .5) * 30, 5 + R() * 10, .4), (.7 + R() * .4,) * 3, (.12, .42, .16) if place != "snow" else (.9, .95, 1))


def build_set(env: dict, seed: int):
    """Строит декорации, небо и свет для сцены. Объекты регистрируются в bl_common.REG."""
    place, time_ = env["place"], env["time"]
    R = rng(seed + 7)
    indoor = place in INDOOR
    sky = SKY[time_] if place != "space" else (0.01, 0.01, 0.04)
    # небо: большая сфера изнутри, светится
    if not indoor:
        sphere("sky", (0, 10, 0), (70, 70, 70), sky, emit=1.0, rough=1, seg=24)
        if time_ in ("day", "morning", "evening") and place != "space":
            sphere("sun", (-25, 55, 22 if time_ == "day" else 8), (3, 3, 3), (1, .95, .7), emit=12, seg=12)
        if time_ == "night" and place != "space":
            sphere("moon", (22, 55, 22), (2.6, 2.6, 2.6), (.95, .95, .85), emit=5, seg=12)
            for i in range(50):
                sphere("star", ((R() - .5) * 90, 50, 10 + R() * 40), (.08, .08, .08), (1, 1, 1), emit=6, seg=6)
        if env.get("weather") != "snow" and time_ != "night" and place != "space":
            for i in range(6):
                x, z = (R() - .5) * 80, 14 + R() * 12
                for k in range(3):
                    sphere("cloud", (x + k * 2.2, 52, z + (k % 2)), (3.2 - k * .4, 1.4, 1.3), (1, 1, 1), emit=.6, seg=10)
    plane("ground", (0, 8, 0), (300, 300, 1), GROUND.get(place, (.3, .6, .2)), rough=.95)
    if indoor:
        _room(env, kitchen=place == "kitchen", school=place == "school", office=place == "office", castle=place == "castle")
    else:
        _outdoors(place, env, R)
    if env.get("weather") == "snow":
        for i in range(110):
            sphere("flake", ((R() - .5) * 18, -1 + R() * 10, R() * 5), (.025, .025, .025), (1, 1, 1), emit=3, seg=6)
    e, col, elev, fill = SUN[time_]
    if indoor:
        e *= .6
    light("key", "SUN", (0, 0, 10), (math.radians(90 - elev), 0, math.radians(-25)), e, col)
    light("fill", "SUN", (0, 0, 10), (math.radians(70), 0, math.radians(160)), fill, (.7, .8, 1.0) if time_ != "evening" else (1, .7, .6))
    light("rim", "POINT", (-3, 5, 4), (0, 0, 0), 250 if time_ != "night" else 150, (1, .95, .9) if time_ != "night" else (.5, .6, 1), 1.5)
    if indoor or time_ == "night":
        light("lamp", "POINT", (0, -2.5, 3.2), (0, 0, 0), 420, (1, .88, .7), 1.2)
