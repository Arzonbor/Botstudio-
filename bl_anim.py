"""Анимация героев: дыхание, взгляд, моргание, брови, жесты, липсинк по громкости, эмоции, действия."""
import array
import math
import wave
import zlib

FPS = 24
STEP = 2            # ключи ставим через кадр (рендерим «на двойках»)

# настроение -> (наклон головы, наклон корпуса, поворот бровей, сдвиг бровей, ширина рта, открытость рта, прищур глаз)
MOODS = {
    "neutral": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0),
    "joy": (-0.04, 0.0, 0.10, 0.012, 1.35, 0.08, 0.92),
    "laugh": (-0.12, -0.05, 0.12, 0.015, 1.5, 0.35, 0.75),
    "sad": (0.20, 0.10, 0.38, -0.004, 0.72, 0.0, 0.8),
    "angry": (0.06, 0.10, -0.42, -0.02, 0.9, 0.02, 0.72),
    "surprise": (-0.08, -0.08, 0.0, 0.045, 0.8, 0.30, 1.2),
    "fear": (0.02, -0.10, 0.28, 0.035, 0.85, 0.22, 1.18),
    "think": (0.05, 0.02, 0.15, 0.015, 0.9, 0.0, 0.95),
    "ask": (0.03, 0.0, 0.0, 0.02, 0.95, 0.0, 1.05),
}


def amplitude(wav_path: str):
    """Громкость по кадрам (0..1), сглаженная, для липсинка."""
    try:
        with wave.open(wav_path) as w:
            sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
        a = array.array("h", raw)
        if ch == 2:
            a = a[::2]
    except Exception:
        return [0.0]
    step = max(1, sr // FPS)
    vals = [sum(abs(x) for x in a[i:i + step]) / step for i in range(0, len(a), step)] or [0.0]
    peak = sorted(vals)[int(len(vals) * 0.95)] or max(vals) or 1
    out, prev = [], 0.0
    for v in vals:
        x = min(1.0, v / peak * 1.25)
        x = x if x > 0.08 else 0.0                 # шум = закрытый рот
        prev = max(x, prev * 0.55)                 # быстрое открытие, мягкое закрытие
        out.append(prev)
    return out


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _ease(x):
    x = _clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def key(o, prop, frame, value):
    setattr(o, prop, value)
    o.keyframe_insert(prop, frame=frame)


def pose(a, t, ctx):
    """Вычисляет позу героя в момент t (сек). ctx: speaking, amp, mood, energy, actions, p, look, walk."""
    ph = (zlib.crc32(a.name.encode()) % 1000) / 160.0
    sp, amp, en, mood = ctx["speaking"], ctx["amp"], ctx["energy"], ctx["mood"]
    acts, p = ctx["actions"], ctx["p"]
    m = MOODS.get(mood, MOODS["neutral"])
    g = _clamp(0.25 + amp, 0, 1) if sp else 0.0           # сила жестикуляции
    P = {}

    breath = math.sin(t * 2.3 + ph)
    sway = math.sin(t * 0.8 + ph)
    hop = 0.0
    if mood in ("joy", "laugh") and en > 0.6:
        hop = max(0.0, math.sin(t * 7 + ph)) * 0.025 * en
    root_dz, root_dx, root_yaw = 0.0, 0.0, 0.0

    # голова
    head_rx = m[0] + (math.sin(t * 4.2 + ph) * 0.05 * amp if sp else 0.0) + (math.sin(t * 1.1 + ph) * 0.015)
    head_rz = _clamp(ctx["look"], -0.7, 0.7) + sway * 0.04
    head_ry = math.sin(t * 0.7 + ph) * 0.03 + (0.10 if mood in ("think", "ask") else 0.0)
    torso_rx = m[1] + breath * 0.012
    torso_rz = -head_rz * 0.25 + (math.sin(t * 2.7 + ph) * 0.04 * g)

    # руки
    def idle_arm(side):
        s = 1 if side == "R" else -1
        return (math.sin(t * 1.4 + ph + s) * 0.03, -s * (0.12 + breath * 0.01), -0.12 + breath * 0.02)

    arm = {"L": idle_arm("L"), "R": idle_arm("R")}
    if g > 0:
        for side, s, k, ph2 in (("R", 1, 1.0, 0.0), ("L", -1, 0.7, 2.1)):
            w1 = 0.5 + 0.5 * math.sin(t * 3.1 + ph + ph2)
            w2 = 0.5 + 0.5 * math.sin(t * 2.3 + ph + ph2 * 1.7)
            arm[side] = (-(0.15 + 0.9 * en * w2) * g * k, -s * (0.12 + 0.55 * en * w1 * g * k),
                         -0.35 - 0.7 * en * w2 * g * k)
    if mood == "angry" and not acts:
        for side in ("L", "R"):
            arm[side] = (-0.2, arm[side][1] * 0.5, -1.5)           # руки в боки/кулаки
    if mood == "sad" and not sp:
        for side in ("L", "R"):
            arm[side] = (0.05, arm[side][1] * 0.6, -0.08)
    if mood in ("fear", "surprise") and sp:
        for side in ("L", "R"):
            arm[side] = (-0.6, arm[side][1] * 0.8, -1.2)

    legs = {"L": 0.0, "R": 0.0}

    # действия
    if "wave" in acts and p < 0.7:
        e = _ease(p / 0.15)
        arm["R"] = (arm["R"][0] * (1 - e) - 0.1 * e, arm["R"][1] * (1 - e) + (-2.7) * e,
                    arm["R"][2] * (1 - e) + (-0.3 + 0.55 * math.sin(t * 13)) * e)
    if "point" in acts:
        e = _ease(p / 0.2)
        arm["R"] = (arm["R"][0] * (1 - e) - 1.45 * e, arm["R"][1] * (1 - e) - 0.25 * e, arm["R"][2] * (1 - e) - 0.05 * e)
    if "nod" in acts:
        head_rx += 0.22 * abs(math.sin(p * math.pi * 3)) * (1 if p < 0.7 else 0)
    if "shake" in acts and p < 0.6:
        head_rz += 0.32 * math.sin(t * 10)
    if "bow" in acts:
        torso_rx += 0.8 * math.sin(_clamp(p * 1.4, 0, 1) * math.pi)
        head_rx += 0.2 * math.sin(_clamp(p * 1.4, 0, 1) * math.pi)
    if "jump" in acts and p < 0.55:
        root_dz += 0.45 * math.sin(p / 0.55 * math.pi)
        arm["L"] = (-0.2, 0.9, -0.4)
        arm["R"] = (-0.2, -0.9, -0.4)
        legs = {"L": 0.5, "R": -0.3}
    if "dance" in acts:
        sw = math.sin(t * 6 + ph)
        root_dz += abs(sw) * 0.06
        root_yaw += sw * 0.25
        arm["L"] = (-0.2, 0.9 + 0.5 * sw, -0.6)
        arm["R"] = (-0.2, -0.9 + 0.5 * sw, -0.6)
        legs = {"L": 0.3 * sw, "R": -0.3 * sw}
        torso_rz += 0.15 * sw

    # вход в кадр (ходьба)
    if ctx["walk"] < 1.0:
        w = ctx["walk"]
        root_dx = ctx["walk_dir"] * 3.2 * (1 - _ease(w))
        sw = math.sin(t * 11)
        legs = {"L": 0.55 * sw, "R": -0.55 * sw}
        arm["L"] = (0.5 * -sw, arm["L"][1], -0.2)
        arm["R"] = (0.5 * sw, arm["R"][1], -0.2)
        root_dz += abs(sw) * 0.03
        head_rz = 0.0

    # моргание и взгляд
    cyc = (t + ph) % 3.3
    blink = 1.0 - 0.92 * math.sin(cyc / 0.15 * math.pi) if cyc < 0.15 else 1.0
    eye_sz = m[6] * blink

    # рот: открытие по громкости + эмоция
    wob = 0.75 + 0.25 * math.sin(t * 17 + ph)
    mouth_w = m[4] * (1 - 0.18 * amp * wob)
    mouth_h = 1.0 + (amp * 7.5 * wob if sp else 0.0) + m[5] * 6
    if mood == "laugh" and not sp:
        mouth_h += 2 + 2 * math.sin(t * 14)

    P.update(head=(head_rx, head_ry, head_rz), torso=(torso_rx, 0.0, torso_rz), arm=arm, legs=legs,
             hips=(0.0, 0.0, 0.0), hips_z=0.85 + hop + breath * 0.004,
             root=(a.home[0] + root_dx, 0.0, root_dz), root_yaw=a.yaw + root_yaw + (0.0),
             brow=(m[2], m[3]), eye_sz=eye_sz, mouth=(mouth_w, mouth_h), mood=mood, open=mouth_h)
    return P


def apply(a, fr, P):
    key(a.head, "rotation_euler", fr, P["head"])
    key(a.torso, "rotation_euler", fr, P["torso"])
    if getattr(a, "kind", "3d") != "cutout":
        key(a.hips, "location", fr, (0, 0, P["hips_z"]))
    key(a.root, "location", fr, P["root"])
    key(a.root, "rotation_euler", fr, (0, 0, P["root_yaw"]))
    for s in ("L", "R"):
        x, y, el = P["arm"][s]
        key(a.arm[s], "rotation_euler", fr, (x, y, 0))
        key(a.elbow[s], "rotation_euler", fr, (el, 0, 0))
        key(a.leg[s], "rotation_euler", fr, (P["legs"][s], 0, 0))
        key(a.eye[s], "scale", fr, (1, 1, max(0.06, P["eye_sz"])))
        rot, dz = P["brow"]
        sgn = 1 if s == "R" else -1
        key(a.brow[s], "rotation_euler", fr, (0, sgn * rot, 0))
        key(a.brow[s], "delta_location", fr, (0, 0, dz))
    key(a.mouth, "scale", fr, (P["mouth"][0], 1.0, P["mouth"][1]))
    if hasattr(a, "apply_extra"):
        a.apply_extra(fr, P)


def animate_span(actors, f0, n, speaker, ctx_line, amp, walk_frames=0):
    """Расставляет ключи для всех героев на отрезке [f0, f0+n) кадров.
    speaker — Actor или None (рассказчик/пауза); ctx_line — {mood, energy, actions}."""
    names = list(actors)
    for f in range(0, n, STEP):
        fr = f0 + f + 1
        t = fr / FPS
        p = f / max(1, n)
        a_now = amp[min(f // 1, len(amp) - 1)] if (speaker is not None and amp) else 0.0
        for a in actors.values():
            is_sp = a is speaker
            if speaker is None or len(names) == 1:
                target = None
            else:
                target = speaker if not is_sp else next((o for o in actors.values() if o is not a), None)
            look = 0.0
            if target is not None:
                look = math.atan2(target.home[0] - a.home[0], 2.6) - a.yaw
            ctx = {"speaking": is_sp, "amp": a_now if is_sp else 0.0,
                   "mood": ctx_line["mood"] if is_sp else ("neutral" if ctx_line["mood"] not in ("sad", "joy", "laugh", "surprise")
                                                           else {"joy": "joy", "laugh": "joy", "sad": "sad", "surprise": "surprise"}[ctx_line["mood"]]),
                   "energy": ctx_line["energy"] if is_sp else 0.3,
                   "actions": set(ctx_line["actions"]) if is_sp else set(),
                   "p": p, "look": look,
                   "walk": min(1.0, (f + 1) / walk_frames) if walk_frames else 1.0,
                   "walk_dir": 1.0 if a.home[0] >= 0 else -1.0}
            apply(a, fr, pose(a, t, ctx))
