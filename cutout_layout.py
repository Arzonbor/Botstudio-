"""Раскладка «куклы» из отдельных PNG-частей (формат архива EHSON_full: assets/head.png, torso.png, arm_*.png,
leg_*.png, eye_closed_*.png, mouth_*.png). Единицы — как в Build_Character.py из архива; Blender потом масштабирует.
Без bpy: используется и ботом (превью), и Blender-воркером."""
import json
from pathlib import Path

# имя файла -> (центр x, центр z, высота, пивот, глубина-слой)
PARTS = {
    "leg_left": (-0.34, -1.95, 2.45, "leg_L", 0.00),
    "leg_right": (0.34, -1.95, 2.45, "leg_R", 0.02),
    "torso": (0.0, 0.72, 2.35, "body", 0.04),
    "arm_left": (-0.86, 0.88, 2.55, "arm_L", 0.06),
    "arm_right": (0.86, 0.88, 2.55, "arm_R", 0.08),
    "head": (0.0, 2.75, 2.25, "head", 0.10),
}
PIVOTS = {"body": (0.0, 0.2), "head": (0.0, 1.95), "arm_L": (-0.48, 1.55), "arm_R": (0.48, 1.55),
          "leg_L": (-0.30, -0.6), "leg_R": (0.30, -0.6)}
MOUTHS = ("neutral", "smile", "open_small", "open", "teeth", "wide")
FACE = {                      # доли картинки головы (x слева, y сверху)
    "eye_L": [0.322, 0.548], "eye_R": [0.538, 0.542], "mouth": [0.443, 0.818],
    "eye_scale": 0.40, "mouth_scale": 0.70,       # множители размера частей лица относительно пикселей головы
}
FOOT_Z = -3.175               # низ ног
TOP_Z = 3.875                 # макушка
REQUIRED = ("head", "torso")
WORLD_HEIGHT = 1.95           # во сколько «метров» вырастает кукла в сцене


def load_layout(parts_dir: Path) -> dict:
    """FACE с возможным переопределением из layout.json рядом с частями."""
    face = dict(FACE)
    f = Path(parts_dir) / "layout.json"
    if f.exists():
        try:
            for k, v in json.loads(f.read_text()).items():
                if k in face:
                    face[k] = v
        except Exception:
            pass
    return face


def head_point(fx: float, fy: float, face_h_units: float = 2.25, w_over_h: float = 350 / 280):
    """Доля картинки головы -> (x, z) в единицах раскладки."""
    cx, cz, h, _, _ = PARTS["head"]
    return cx + (fx - 0.5) * h * w_over_h, cz + (0.5 - fy) * h


def compose_preview(parts: dict, face: dict | None = None, ppu: int = 100):
    """Собирает картинку-превью куклы (PIL). parts: {имя: путь}. Возвращает RGBA-изображение."""
    from PIL import Image
    face = face or FACE
    x0, x1, z0, z1 = -1.9, 1.9, FOOT_Z - 0.1, TOP_Z + 0.1
    W, H = int((x1 - x0) * ppu), int((z1 - z0) * ppu)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    order = sorted((k for k in PARTS if k in parts), key=lambda k: PARTS[k][4])
    for k in order:
        cx, cz, h, _, _ = PARTS[k]
        im = Image.open(parts[k]).convert("RGBA")
        w = h * im.width / im.height
        im = im.resize((max(1, int(w * ppu)), max(1, int(h * ppu))))
        canvas.alpha_composite(im, (int((cx - w / 2 - x0) * ppu), int((z1 - (cz + h / 2)) * ppu)))
    return canvas


def head_crop(canvas, ppu: int = 100):
    """Голова и плечи для миниатюры."""
    z1 = TOP_Z + 0.1
    top, bottom = 0, int((z1 - 1.2) * ppu)
    return canvas.crop((0, top, canvas.width, bottom))
