"""
Запускается внутри Blender:
  blender -b -P blender_worker.py -- --scene scene.json --out result.mp4 [--preview]

Что строит Blender:
  * героев — если картинка не загружена, сам «рисует» 3D-персонажа по описанию из сценария (scene.json: cast);
    загруженные 2D-листы тоже работают, их можно смешивать с 3D-героями;
  * декорации по месту действия (лес, комната, город, пляж, космос…), небо и свет по времени суток;
  * кино-камеру: общий план, средний план на говорящем, крупный план на эмоциях, плавный наезд;
  * анимацию: дыхание, взгляд друг на друга, моргание, брови, жесты, липсинк по громкости, выход героев в кадр.
Рендер: Eevee (RENDER_ENGINE=cycles — Cycles), «на двойках» (каждый 2-й кадр, как в 2D-мультфильмах),
звук сводится ffmpeg вместе с тихой фоновой музыкой.
Переменные: RENDER_W/RENDER_H (по умолчанию 1280x720), RENDER_SAMPLES, RENDER_MUSIC=0 (без музыки).
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy                      # noqa: E402
from mathutils import Vector    # noqa: E402

import bl_anim                  # noqa: E402
import bl_chars                 # noqa: E402
import bl_custom                # noqa: E402
import bl_cutout                # noqa: E402
import bl_sets                  # noqa: E402
import bl_sprite                # noqa: E402
from bl_common import REG, key_visibility, start_scene  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
arg = lambda k: argv[argv.index(k) + 1]
SCENE = json.loads(Path(arg("--scene")).read_text())
OUT = Path(arg("--out"))
FRAMES = Path(arg("--frames")) if "--frames" in argv else OUT.parent / "frames"
PREVIEW = "--preview" in argv
FPS = 24
STEP = 3 if PREVIEW else 2
RES = (640, 360) if PREVIEW else (int(os.environ.get("RENDER_W", 1280)), int(os.environ.get("RENDER_H", 720)))
SAMPLES = 8 if PREVIEW else int(os.environ.get("RENDER_SAMPLES", 24))
INTRO, GAP, OUTRO = 24, 8, 18


def even(n: int) -> int:
    n = int(n)
    return n + (n % STEP)


def setup_render():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    want = os.environ.get("RENDER_ENGINE", "eevee").lower()
    names = ["CYCLES"] if want == "cycles" else ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
    for n in names:
        try:
            sc.render.engine = n
            break
        except TypeError:
            continue
    ee = sc.eevee
    for attr, val in (("taa_render_samples", SAMPLES), ("use_raytracing", False), ("use_shadows", True),
                      ("use_gtao", True), ("use_soft_shadows", True), ("use_bloom", False)):
        if hasattr(ee, attr):
            try:
                setattr(ee, attr, val)
            except Exception:
                pass
    if sc.render.engine == "CYCLES":
        sc.cycles.samples = SAMPLES
        sc.cycles.use_denoising = True
        sc.cycles.device = "CPU"
    sc.render.fps = max(1, FPS // STEP)           # видео пишется «на двойках», ffmpeg потом вернёт 24 к/с
    sc.render.resolution_x, sc.render.resolution_y = RES
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = False
    sc.render.use_motion_blur = False
    try:
        sc.view_settings.view_transform = "Standard"
        sc.view_settings.look = "None"
    except Exception:
        pass
    w = bpy.data.worlds.new("world")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.55, 0.6, 0.7, 1)
    bg.inputs[1].default_value = 0.7
    sc.world = w
    sc.frame_step = STEP
    return sc


def prune_frames() -> None:
    """Перед продолжением удаляем пустые и последние (возможно недописанные) кадры."""
    files = sorted(FRAMES.glob("f_*.png"), key=lambda p: p.stat().st_mtime)
    for p in files:
        if p.stat().st_size == 0:
            p.unlink()
    files = sorted(FRAMES.glob("f_*.png"), key=lambda p: p.stat().st_mtime)
    for p in files[-2:]:
        p.unlink()


def positions(n: int) -> list[float]:
    gap = 2.5 if n <= 3 else 2.2
    return [(i - (n - 1) / 2) * gap for i in range(n)]


def attach_to_stage(a, stage):
    """Герой становится ребёнком stage: позиции считаются в системе сцены (центр, пол, поворот)."""
    objs = []
    if getattr(a, "root", None) is not None:
        objs.append(a.root)
    else:                                                   # 2D-лист: плоскость/скелет/лица
        objs += [a.arm if a.arm is not None else a.ob]
        objs += list((a.sprites or {}).values()) + [o for o in (a.mouth, *a.lids) if o is not None]
    for o in objs:
        if o is not None and o.parent is None:
            o.parent = stage


def make_cam(sc, name, loc, target, lens, push, f0, f1, stage=None):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    cd.clip_end = 400
    co = bpy.data.objects.new(name, cd)
    sc.collection.objects.link(co)
    loc, target = Vector(loc), Vector(target)
    d = (target - loc)
    co.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    if stage is not None:
        co.parent = stage
    co.location = loc
    co.keyframe_insert("location", frame=f0 + 1)
    co.location = loc + d.normalized() * push
    co.keyframe_insert("location", frame=f1)
    m = sc.timeline_markers.new(name, frame=f0 + 1)
    m.camera = co


def shot_for(kind, a, actors, n_actors):
    """Положение камеры для плана. Возвращает (loc, target, lens, push)."""
    if kind == "wide" or a is None:
        dist = 5.4 + 1.25 * n_actors
        return (0, -dist, 1.7), (0, 0, 1.1), 35, 0.5
    h = 1.75 * getattr(a, "scale", 1.0)
    x = a.home[0]
    side = -1 if x >= 0 else 1                   # камера чуть со стороны собеседника: «через плечо»
    if kind == "close":
        return (x + side * 0.25, -2.6, h * 0.9), (x, 0, h * 0.88), 55, 0.35
    return (x * 0.85 + side * 0.5, -4.0, h * 0.78), (x, 0, h * 0.68), 45, 0.4


def mix_audio(parts, total_sec, frames_dir: Path, out: Path):
    """Сводит реплики (+ тихая музыка) и пересобирает видео в 24 к/с с плавным появлением/затуханием."""
    music = os.environ.get("RENDER_MUSIC", "1") != "0"
    fade = f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, total_sec - 0.8):.2f}:d=0.8"
    for with_music, norm in ((music, True), (music, False), (False, True), (False, False)):
        cmd = ["ffmpeg", "-y", "-framerate", str(max(1, FPS // STEP)), "-pattern_type", "glob",
               "-i", str(frames_dir / "f_*.png")]
        for p, _ in parts:
            cmd += ["-i", p]
        if with_music:
            for f in (220.0, 261.63, 329.63):
                cmd += ["-f", "lavfi", "-t", f"{total_sec:.2f}", "-i", f"sine=frequency={f}:sample_rate=44100"]
        n = len(parts)
        filt = ""
        for i, (_, t) in enumerate(parts):
            ms = int(t * 1000)
            filt += f"[{i + 1}]aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}];"
        labels = "".join(f"[a{i}]" for i in range(n))
        norm_opt = ":normalize=0" if norm else ""
        filt += f"{labels}amix=inputs={n}:duration=longest{norm_opt}[voice];"
        if not norm:
            filt += f"[voice]volume={min(n, 4)}[voice];"
        if with_music:
            filt += (f"[{n + 1}][{n + 2}][{n + 3}]amix=inputs=3:normalize=0,tremolo=f=0.25:d=0.4,"
                     f"lowpass=f=900,volume=0.035,aformat=channel_layouts=stereo[mus];"
                     f"[voice][mus]amix=inputs=2:duration=first:normalize=0[mix];")
        else:
            filt += "[voice]anull[mix];"
        filt += f"[mix]loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.3,afade=t=out:st={max(0.0, total_sec - 0.8):.2f}:d=0.8[aout]"
        cmd += ["-filter_complex", filt, "-map", "0:v", "-map", "[aout]",
                "-vf", f"fps={FPS},{fade}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-t", f"{total_sec:.2f}",
                "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and out.exists():
            return
        print("ffmpeg variant failed:", r.stderr[-400:])
    raise RuntimeError("ffmpeg не смог собрать звук и видео")


def main():
    print("STAGE=building", flush=True)
    sc = setup_render()
    cast = SCENE.get("cast", {})
    cursor = 0
    audio_parts = []
    for s_i, scene in enumerate(SCENE["scenes"]):
        start_scene()
        s_start = cursor + 1
        env = scene.get("env") or {"place": "park", "time": "day", "weather": "clear"}
        order = list(dict.fromkeys(l["who"] for l in scene["lines"] if not l.get("narrator")))
        xs = positions(len(order))
        stage = None
        custom = scene.get("custom") or {}
        if custom.get("script"):
            try:
                stage = bl_custom.run(custom["script"], len(order), custom.get("stage"))
            except Exception as e:                       # сцена-скрипт не заработала — рисуем обычную декорацию
                print(f"WARN=Сцена «{custom.get('name', '')}» не запустилась ({e}); использую стандартную декорацию", flush=True)
                start_scene()
                stage = None
        if stage is None:
            bl_sets.build_set(env, s_i)
        bright = {"night": 0.6, "evening": 0.85}.get(env.get("time"), 1.0) if stage is None else 1.0
        actors = {}
        for who, x in zip(order, xs):
            lines = [l for l in scene["lines"] if l["who"] == who]
            img = next((l.get("image") for l in lines if l.get("image")), None)
            yaw = 0.0 if len(order) == 1 else (0.3 if x < 0 else -0.3)
            cut = next((l.get("cutout") for l in lines if l.get("cutout")), None)
            if cut:
                a = bl_cutout.build(who, cut, x, bright)
            elif img:
                meta = next((l.get("meta") for l in lines if l.get("meta")), {})
                a = bl_sprite.build(who, img, meta, x)
            else:
                a = bl_chars.build(who, cast.get(who) or {"species": "human", "gender": "m", "age": "adult", "height": 1.0,
                                                         "skin": [.9, .7, .55], "eye_color": [.2, .15, .1], "hair_color": [.2, .12, .06],
                                                         "hair_style": "short", "beard": "none", "hat": "none", "accessory": "none",
                                                         "outfit": "casual", "top": [.2, .45, .8], "bottom": [.2, .2, .3],
                                                         "shoes": [.1, .08, .06]}, x, yaw)
            actors[who] = a
            if stage is not None:
                attach_to_stage(a, stage)
        n_act = len(actors)
        d3 = {k: v for k, v in actors.items() if getattr(v, "kind", "3d") != "sprite"}
        sprites = [v for v in actors.values() if getattr(v, "kind", "") == "sprite"]

        def span(f0, n, speaker=None, line=None, amp=None, walk=0):
            ctx = {"mood": (line or {}).get("mood", "neutral"), "energy": (line or {}).get("energy", 0.3),
                   "actions": (line or {}).get("actions", [])}
            bl_anim.animate_span(d3, f0, n, speaker if speaker in d3.values() else None, ctx, amp or [], walk)
            for sp in sprites:
                is_sp = sp is speaker
                bl_sprite.animate_span(sp, f0, n, amp or [], ctx["mood"], ctx["energy"], is_sp)

        shots = []                                   # (kind, speaker, f0, f1)

        def add_shot(kind, a, f0, f1):
            if shots and shots[-1][0] == kind and shots[-1][1] is a:
                shots[-1] = (kind, a, shots[-1][2], f1)
            else:
                shots.append((kind, a, f0, f1))

        # вход героев в кадр, общий план
        intro = even(INTRO) if n_act else even(12)
        span(cursor, intro, walk=even(INTRO - 4) if n_act else 0)
        add_shot("wide", None, cursor, cursor + intro)
        cursor += intro

        for li, ln in enumerate(scene["lines"]):
            frames = even(int(ln["duration"] * FPS) + GAP)
            sp = actors.get(ln["who"]) if not ln.get("narrator") else None
            amp = bl_anim.amplitude(ln["audio"])
            span(cursor, frames, sp, ln, amp)
            if sp is None or n_act == 0:
                kind = "wide"
            elif ln.get("mood") in ("surprise", "fear", "sad", "angry") or ln.get("energy", 0) > 0.9:
                kind = "close"
            elif n_act > 1 and li % 4 == 3:
                kind = "wide"
            else:
                kind = "medium"
            add_shot(kind, sp, cursor, cursor + frames)
            audio_parts.append((ln["audio"], cursor / FPS))
            cursor += frames

        outro = even(OUTRO)
        span(cursor, outro)
        add_shot("wide", None, cursor, cursor + outro)
        cursor += outro
        s_end = cursor

        for i, (kind, a, f0, f1) in enumerate(shots):
            loc, tgt, lens, push = shot_for(kind, a, actors, n_act)
            make_cam(sc, f"cam{s_i}_{i}", loc, tgt, lens, push, f0, f1, stage)
        key_visibility(list(REG), s_start, s_end)
        for sp in actors.values():                       # объекты с собственной видимостью: скрыть до и после сцены
            for o in list((getattr(sp, "sprites", None) or {}).values()) + list(getattr(sp, "novis_objs", [])):
                o.hide_render = True
                if s_start > 1:
                    o.keyframe_insert("hide_render", frame=s_start - 1)
                o.keyframe_insert("hide_render", frame=s_end + 1)

    sc.frame_start, sc.frame_end = 1, max(cursor, 24)
    total_sec = sc.frame_end / FPS
    print(f"TOTAL_FRAMES={sc.frame_end}", flush=True)

    FRAMES.mkdir(parents=True, exist_ok=True)
    prune_frames()
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.image_settings.compression = 15
    sc.render.use_overwrite = False                    # готовые кадры пропускаются: так работает «Продолжить»
    sc.render.use_placeholder = False
    sc.render.filepath = str(FRAMES / "f_######")
    print("STAGE=rendering", flush=True)
    bpy.ops.render.render(animation=True)
    print("STAGE=encoding", flush=True)
    mix_audio(audio_parts, total_sec, FRAMES, OUT)

main()
