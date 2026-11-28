"""Готовые наборы из zip-архива: «кукла» из PNG-частей (персонаж) или скрипт 3D-сцены для Blender.
Бот сам распознаёт тип архива, безопасно распаковывает и готовит превью.
    detect(zip)        -> "cutout" | "scene" | None
    build_cutout(...)  -> meta персонажа (части, превью, миниатюра)
    build_scene(...)   -> {"script": путь, "title": название}"""
import re
import zipfile
from pathlib import Path

import cutout_layout as L
import scene_guard

MAX_FILES, MAX_TOTAL = 400, 300 * 1024 * 1024


class PackError(ValueError):
    pass


def _members(z: zipfile.ZipFile):
    out = []
    for i in z.infolist():
        p = Path(i.filename)
        if i.is_dir() or p.is_absolute() or ".." in p.parts or "__MACOSX" in p.parts or p.name.startswith("."):
            continue
        out.append(i)
    return out


def safe_extract(zpath: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zpath) as z:
        mem = _members(z)
        if len(mem) > MAX_FILES or sum(i.file_size for i in mem) > MAX_TOTAL:
            raise PackError("Архив слишком большой (лимит 300 МБ и 400 файлов)")
        for i in mem:
            target = (dest / i.filename).resolve()
            if dest.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


def detect(zpath: Path) -> str | None:
    try:
        with zipfile.ZipFile(zpath) as z:
            names = [Path(i.filename).name.lower() for i in _members(z)]
    except zipfile.BadZipFile:
        return None
    if all(f"{p}.png" in names for p in L.REQUIRED):
        return "cutout"
    if any(n.endswith(".py") for n in names):
        return "scene"
    return None


def _norm_stem(p: Path) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", p.stem.lower()).strip("_")


def build_cutout(zpath: Path, out_dir: Path, progress=lambda p, t="": None) -> dict:
    """Распаковывает части куклы, собирает превью (целиком) и миниатюру (голова и плечи)."""
    from PIL import Image
    progress(10, "Распаковываю архив")
    files = safe_extract(zpath, out_dir / "src")
    by = {}
    for f in files:
        if f.suffix.lower() == ".png":
            by.setdefault(_norm_stem(f), f)
    missing = [p for p in L.REQUIRED if p not in by]
    if missing:
        raise PackError("В архиве нет обязательных частей: " + ", ".join(m + ".png" for m in missing))
    keep = set(L.PARTS) | {f"mouth_{m}" for m in L.MOUTHS} | {"eye_closed_left", "eye_closed_right"}
    parts = {}
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for i, (k, f) in enumerate(sorted((k, f) for k, f in by.items() if k in keep)):
        im = Image.open(f).convert("RGBA")          # проверка, что это настоящая картинка
        dst = parts_dir / f"{k}.png"
        im.save(dst)
        parts[k] = str(dst)
        progress(20 + int(40 * (i + 1) / len(keep)), f"Часть: {k}")
    lay = next((f for f in files if f.name.lower() == "layout.json"), None)
    if lay:
        (parts_dir / "layout.json").write_bytes(lay.read_bytes())
    progress(70, "Собираю превью")
    face = L.load_layout(parts_dir)
    canvas = L.compose_preview(parts, face)
    body = out_dir / "body.png"
    canvas.save(body)
    thumb = out_dir / "thumb.png"
    t = L.head_crop(canvas)
    t.thumbnail((320, 320))
    t.save(thumb)
    progress(90, "Готово")
    return {"kind": "cutout", "body": str(body), "thumb": str(thumb), "parts": parts,
            "mouths": [m for m in L.MOUTHS if f"mouth_{m}" in parts],
            "has_blink": "eye_closed_left" in parts and "eye_closed_right" in parts,
            "layout": str(parts_dir / "layout.json") if lay else ""}


def build_scene(zpath: Path, out_dir: Path, fallback_title: str = "") -> dict:
    """Находит .py со сценой, проверяет безопасность и сохраняет как scene.py."""
    files = safe_extract(zpath, out_dir / "src")
    pys = [f for f in files if f.suffix.lower() == ".py"]
    if not pys:
        raise PackError("В архиве нет .py-скрипта сцены")
    best = next((f for f in pys if "bpy" in f.read_text(errors="ignore")), pys[0])
    src = best.read_text(errors="ignore")
    if "bpy" not in src:
        raise PackError("Скрипт не похож на сцену Blender (нет import bpy)")
    try:
        scene_guard.check(src)
    except scene_guard.SceneScriptError as e:
        raise PackError(f"Скрипт сцены не прошёл проверку безопасности: {e}")
    dst = out_dir / "scene.py"
    dst.write_text(src)
    m = re.search(r"^#\s*(.{4,70}?)\s*(?:for Blender)?\s*$", src, re.M)
    title = fallback_title or (m.group(1) if m else best.stem)
    return {"kind": "scene", "script": str(dst), "title": title}
