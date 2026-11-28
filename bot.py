"""
TajStudio — Telegram-бот + Mini App: сценарий -> мультфильм (Blender) -> видео в чат.

Всё, что умеет бот в чате, умеет и Mini App (общая логика, HTTP API внизу файла):
  • загрузка персонажа: имя + предпросмотр, прогресс в процентах (и в чате, и в Mini App)
  • удаление персонажей
  • озвучка на выбор: Piper (бесплатно, на сервере) или ElevenLabs
  • предпросмотр / рендер со статусом в процентах, кнопка «Скачать» (видео придёт в чат)

Запуск:  python bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
import firebase_admin
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from firebase_admin import credentials, db

import packs
import scenario
import tts
from prepare_character import prepare

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cartoon")

# ───────────────────────── настройки (env на Bothost) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
FIREBASE_CRED_JSON = os.environ["FIREBASE_CRED_JSON"]      # содержимое service-account.json
FIREBASE_DB_URL = os.environ["FIREBASE_DB_URL"]
YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")
ELEVEN_VOICES = json.loads(os.environ.get("ELEVEN_VOICES", "{}"))  # старый формат {"Маша": "voice_id"}
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WORK = Path(os.environ.get("WORK_DIR", "/tmp/cartoon"))
WORK.mkdir(parents=True, exist_ok=True)
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")   # https-ссылка на Mini App (домен Bothost, где отдаётся index.html)
MAX_RENDERS = int(os.environ.get("MAX_RENDERS", "1"))   # сколько рендеров одновременно
MAX_UPLOAD = 25 * 1024 * 1024
MAX_ARCHIVE = 120 * 1024 * 1024                          # zip с готовым персонажем / сценой
TG_LIMIT = 49 * 1024 * 1024                              # Telegram: боты шлют файлы до 50 МБ
FPS = 24
MENU_TITLE = "TajStudio"


async def set_menu_button(bot: Bot, chat_id: int | None = None) -> None:
    """Кнопка TajStudio слева от поля ввода — открывает мини-апп."""
    if not MINIAPP_URL:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text=MENU_TITLE, web_app=WebAppInfo(url=MINIAPP_URL)))
    except Exception:
        log.exception("set_chat_menu_button failed")


firebase_admin.initialize_app(
    credentials.Certificate(json.loads(FIREBASE_CRED_JSON)),
    {"databaseURL": FIREBASE_DB_URL},
)

CHARACTER_PROMPT = (
    "Нарисуй этого человека как 2D-персонажа в стиле классической студии Дисней: "
    "полный рост, прямо, лицом в камеру, руки слегка в стороны, ноги расставлены, "
    "рот закрыт, глаза открыты, нейтральное выражение, чистый однотонный фон без теней, "
    "чёткий контур, высокое разрешение, PNG."
)

STYLES = {
    "classic": "Классика Disney (мягкие контуры, тёплая палитра)",
    "modern": "Современный Disney (объёмный свет, глубокие цвета)",
    "storybook": "Книжная иллюстрация (акварельные фоны)",
}
STYLE_ALIASES = {"water": "storybook", "noir": "modern", "rose": "classic"}   # стили Mini App -> стили Blender
ENGINE_TITLES = {"piper": "Piper (бесплатно, на сервере)", "eleven": "ElevenLabs"}


class Flow(StatesGroup):
    waiting_character = State()
    waiting_name = State()
    waiting_script = State()
    choosing_style = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Новый мультфильм", callback_data="new")],
        [InlineKeyboardButton(text="🧍 Мои персонажи", callback_data="chars")],
        [InlineKeyboardButton(text="🎙 Озвучка", callback_data="voice")],
        [InlineKeyboardButton(text="📁 Мои проекты", callback_data="projects")],
        [InlineKeyboardButton(text="📋 Промпт для ChatGPT", callback_data="prompt")],
    ])


router = Router()
BG_TASKS: set = set()
JOBS: dict[str, dict] = {}          # живой прогресс: "uid/c/имя" (персонаж), "uid/p/id" (проект)
RENDER_SEM = asyncio.Semaphore(MAX_RENDERS)


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


def job_set(key: str, **kw) -> None:
    JOBS.setdefault(key, {"ts": time.time()}).update(kw, upd=time.time())


def bar(pct: int) -> str:
    n = max(0, min(10, round(pct / 10)))
    return "▰" * n + "▱" * (10 - n) + f" {pct}%"


def cid(name: str) -> str:
    """Короткий id персонажа для callback_data (лимит Telegram 64 байта)."""
    return hashlib.md5(name.encode()).hexdigest()[:8]


def clean_name(raw: str) -> str:
    name = re.sub(r"[.$#\[\]/\\:\n\r\t]", " ", raw or "")      # запрещено в ключах Firebase / именах файлов / сценарии
    return re.sub(r"\s+", " ", name).strip()[:30]


def find_char(pool: dict, who: str):
    """Ищет персонажа без учёта регистра. Возвращает (имя, запись) или (None, None)."""
    if who in pool:
        return who, pool[who]
    low = who.lower()
    for n, v in pool.items():
        if n.lower() == low:
            return n, v
    return None, None


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:
        pass        # «message is not modified», лимит правок и т.п. — не критично


# ───────────────────────────── Firebase ─────────────────────────────
def fb(path: str):
    return db.reference(path)


async def fb_get(path):
    return await asyncio.to_thread(lambda: fb(path).get())


async def fb_set(path, value):
    await asyncio.to_thread(lambda: fb(path).set(value))


async def fb_update(path, value):
    await asyncio.to_thread(lambda: fb(path).update(value))


async def fb_delete(path):
    await asyncio.to_thread(lambda: fb(path).delete())


# ───────────────────────────── Яндекс Диск ─────────────────────────────
YD = "https://cloud-api.yandex.net/v1/disk/resources"


async def yadisk_upload(local: Path, remote: str) -> str | None:
    """Грузит файл на Яндекс Диск, возвращает путь. Без токена — молча пропускает."""
    if not YADISK_TOKEN:
        return None
    async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
        cur = ""
        for p in remote.strip("/").split("/")[:-1]:
            cur += "/" + p
            await s.put(YD, params={"path": cur})
        async with s.get(f"{YD}/upload", params={"path": remote, "overwrite": "true"}) as r:
            url = (await r.json()).get("href")
        if not url:
            return None
        async with s.put(url, data=local.read_bytes()):
            pass
    return remote


async def yadisk_download(remote: str, local: Path) -> bool:
    if not YADISK_TOKEN or not remote:
        return False
    local.parent.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
        async with s.get(f"{YD}/download", params={"path": remote}) as r:
            href = (await r.json()).get("href")
        if not href:
            return False
        async with s.get(href) as r:
            if r.status != 200:
                return False
            local.write_bytes(await r.read())
    return True


async def yadisk_delete(remote: str | None) -> None:
    if not YADISK_TOKEN or not remote:
        return
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
            await s.delete(YD, params={"path": remote, "permanently": "true"})
    except Exception:
        log.exception("yadisk delete failed")


# ───────────────────────────── сценарий ─────────────────────────────
def parse_script(text: str) -> tuple[list[dict], dict]:
    """Сценарий -> (сцены, описания персонажей). Формат и подсказки — в scenario.py."""
    return scenario.parse(text)


# ───────────────────────────── настройки озвучки ─────────────────────────────
async def get_tts_settings(uid: str) -> dict:
    s = await fb_get(f"clients/{uid}/settings") or {}
    engine = s.get("engine") if s.get("engine") in ("piper", "eleven") else "piper"
    return {"engine": engine, "voice": tts.resolve_voice(engine, s.get("voice", ""))}


async def save_tts_settings(uid: str, engine: str, voice: str) -> None:
    await fb_update(f"clients/{uid}/settings", {"engine": engine, "voice": voice})


# ───────────────────────────── персонажи ─────────────────────────────
def files_ok(ch: dict) -> bool:
    if ch.get("kind") == "cutout":
        return bool(ch.get("parts")) and all(Path(p).exists() for p in ch["parts"].values())
    paths = [ch.get("file")] + list((ch.get("meta") or {}).get("faces", []))
    return all(p and Path(p).exists() for p in paths)


async def ensure_character(uid: str, name: str, ch: dict) -> dict:
    """Если после перезапуска сервера файлы персонажа пропали — восстанавливает с Яндекс Диска."""
    if files_ok(ch):
        return ch
    if ch.get("kind") == "cutout":
        zp = WORK / uid / "characters" / f"{name}.zip"
        if not (await yadisk_download(ch.get("disk"), zp)):
            raise RuntimeError(f"Файлы персонажа «{name}» потеряны. Загрузите архив заново.")
        info = await asyncio.to_thread(packs.build_cutout, zp, zp.parent / name)
        return {**ch, "file": info["body"], "thumb": info["thumb"], "parts": info["parts"]}
    orig = WORK / uid / "characters" / f"{name}.png"
    if not (await yadisk_download(ch.get("disk"), orig)):
        raise RuntimeError(f"Файлы персонажа «{name}» потеряны. Загрузите его заново.")
    meta = await asyncio.to_thread(prepare, orig, orig.parent / name)
    return {**ch, "file": meta["body"], "thumb": meta.get("thumb"), "meta": meta}


async def create_character(uid: str, name: str, orig: Path, to_library: bool = False) -> bool:
    """Обработка персонажа с прогрессом в JOBS. Не бросает исключений: итог — в JOBS[key]['status'].
    orig — картинка-лист (png/jpg) или zip с готовой куклой (EHSON_full и подобные)."""
    key = f"{uid}/c/{name}"
    job_set(key, kind="char", name=name, pct=5, text="Файл получен", status="processing", error="")
    try:
        old = await fb_get(f"clients/{uid}/characters/{name}") or {}
        shutil.rmtree(orig.parent / name, ignore_errors=True)
        cb = lambda p, t="": job_set(key, pct=10 + int(p * 0.75), text=t)       # 10..85 %
        is_zip = orig.suffix.lower() == ".zip"
        if is_zip:
            info = await asyncio.to_thread(packs.build_cutout, orig, orig.parent / name, cb)
        else:
            meta = await asyncio.to_thread(prepare, orig, orig.parent / name, cb)
        job_set(key, pct=88, text="Сохраняю на Яндекс Диск")
        try:
            remote = await yadisk_upload(orig, f"/CartoonStudio/{uid}/characters/{name}{orig.suffix.lower()}")
        except Exception:
            log.exception("yadisk upload failed")
            remote = None
        job_set(key, pct=96, text="Записываю в базу")
        if is_zip:
            rec = {"id": cid(name), "kind": "cutout", "file": info["body"], "thumb": info["thumb"], "disk": remote,
                   "parts": info["parts"], "mouths": info["mouths"], "has_blink": info["has_blink"],
                   "voice": old.get("voice", ""), "created": int(time.time())}
        else:
            rec = {"id": cid(name), "file": meta["body"], "thumb": meta.get("thumb"), "disk": remote,
                   "voice": old.get("voice", ""), "meta": meta, "created": int(time.time())}
        await fb_set(f"clients/{uid}/characters/{name}", rec)
        if to_library:
            await fb_set(f"library/characters/{name}", {**rec, "owner": uid})
        job_set(key, pct=100, text="Готово", status="done")
        return True
    except packs.PackError as e:
        job_set(key, status="error", error=str(e)[:300], text="Ошибка")
        return False
    except Exception as e:
        log.exception("create_character failed")
        msg = "Не удалось прочитать картинку. Пришлите PNG/JPG." if "cannot identify" in str(e) else str(e)[:300]
        job_set(key, status="error", error=msg, text="Ошибка")
        return False


async def delete_character(uid: str, name: str, library: bool = False) -> bool:
    JOBS.pop(f"{uid}/c/{name}", None)
    if library:
        if int(uid) not in ADMIN_IDS:
            return False
        await fb_delete(f"library/characters/{name}")
        return True
    rec = await fb_get(f"clients/{uid}/characters/{name}")
    await fb_delete(f"clients/{uid}/characters/{name}")
    if int(uid) in ADMIN_IDS:
        lib = await fb_get(f"library/characters/{name}")
        if lib and lib.get("owner") == uid:
            await fb_delete(f"library/characters/{name}")
    cdir = WORK / uid / "characters"
    shutil.rmtree(cdir / name, ignore_errors=True)
    (cdir / f"{name}.png").unlink(missing_ok=True)
    (cdir / f"{name}.zip").unlink(missing_ok=True)
    if rec:
        await yadisk_delete(rec.get("disk"))
    return True


# ───────────────────────────── готовые сцены (Blender-скрипты) ─────────────────────────────
async def ensure_scene_file(uid: str, ref: dict) -> str | None:
    """Путь к скрипту сцены; если файл пропал после перезапуска — достаём с Яндекс Диска."""
    p = Path(ref.get("script") or "/nonexistent")
    if p.exists():
        return str(p)
    dst = WORK / uid / "scenes" / cid(ref["name"]) / "scene_script.py"
    if await yadisk_download(ref.get("disk"), dst):
        return str(dst)
    raise RuntimeError(f"Файл сцены «{ref['name']}» потерян. Загрузите архив сцены заново.")


async def create_scene(uid: str, name: str, zpath: Path, to_library: bool = False) -> bool:
    key = f"{uid}/s/{name}"
    job_set(key, kind="scene", name=name, pct=10, text="Распаковываю и проверяю скрипт", status="processing", error="")
    try:
        out = WORK / uid / "scenes" / cid(name)
        shutil.rmtree(out, ignore_errors=True)
        info = await asyncio.to_thread(packs.build_scene, zpath, out, name)
        job_set(key, pct=70, text="Сохраняю на Яндекс Диск")
        try:
            remote = await yadisk_upload(Path(info["script"]), f"/CartoonStudio/{uid}/scenes/{cid(name)}.py")
        except Exception:
            log.exception("yadisk upload failed")
            remote = None
        rec = {"id": cid(name), "script": info["script"], "title": info.get("title", ""), "disk": remote,
               "created": int(time.time())}
        await fb_set(f"clients/{uid}/scenes/{name}", rec)
        if to_library:
            await fb_set(f"library/scenes/{name}", {**rec, "owner": uid})
        job_set(key, pct=100, text="Готово", status="done")
        return True
    except (packs.PackError, Exception) as e:
        if not isinstance(e, packs.PackError):
            log.exception("create_scene failed")
        job_set(key, status="error", error=str(e)[:300], text="Ошибка")
        return False


async def delete_scene(uid: str, name: str, library: bool = False) -> bool:
    JOBS.pop(f"{uid}/s/{name}", None)
    if library:
        if int(uid) not in ADMIN_IDS:
            return False
        await fb_delete(f"library/scenes/{name}")
        return True
    rec = await fb_get(f"clients/{uid}/scenes/{name}")
    await fb_delete(f"clients/{uid}/scenes/{name}")
    if int(uid) in ADMIN_IDS:
        lib = await fb_get(f"library/scenes/{name}")
        if lib and lib.get("owner") == uid:
            await fb_delete(f"library/scenes/{name}")
    shutil.rmtree(WORK / uid / "scenes" / cid(name), ignore_errors=True)
    if rec:
        await yadisk_delete(rec.get("disk"))
    return True


# ───────────────────────────── рендер ─────────────────────────────
FRA_RE = re.compile(r"Fra:(\d+)")
TOT_RE = re.compile(r"TOTAL_FRAMES=(\d+)")
STAGE_RE = re.compile(r"STAGE=(\w+)")
WARN_RE = re.compile(r"WARN=(.+)")

RENDER_URL = os.environ.get("RENDER_URL", "").rstrip("/")     # адрес сервера рендера на VPS (instabox.sh)
RENDER_TOKEN = os.environ.get("RENDER_TOKEN", "")

# этапы: (код, заголовок, с какого %, до какого %)
STAGES = [("queued", "В очереди", 0, 1), ("tts", "Озвучка реплик", 1, 20), ("building", "Blender строит сцену", 20, 30),
          ("rendering", "Рендер кадров", 30, 90), ("encoding", "Склейка видео и звука", 90, 95),
          ("upload", "Сохранение", 95, 99), ("done", "Готово", 100, 100)]
STAGE_RANGE = {s[0]: (s[2], s[3]) for s in STAGES}


class RenderFailed(RuntimeError):
    pass


def stage_set(key: str, stage: str, frac: float = 0.0, text: str = "", **kw) -> None:
    lo, hi = STAGE_RANGE.get(stage, (0, 0))
    title = next((s[1] for s in STAGES if s[0] == stage), stage)
    job_set(key, stage=stage, pct=lo + int((hi - lo) * max(0.0, min(1.0, frac))), text=text or title, **kw)


def apply_remote_status(key: str, st: dict) -> None:
    """Статус сервера рендера -> живой статус проекта."""
    s = st.get("status")
    frames, done = int(st.get("frames") or 0), int(st.get("done") or 0)
    if s == "queued":
        return stage_set(key, "queued", 0, "Ждёт очереди на сервере рендера")
    if s == "building":
        return stage_set(key, "building", 0.5, "Blender загружает сцену, строит героев и декорации")
    if s == "rendering":
        return stage_set(key, "rendering", done / max(1, frames), f"Рендер кадров: {done} из {frames}",
                         frames_done=done, frames_total=frames, eta=st.get("eta"), log=st.get("tail") or [],
                         warn=st.get("warn", ""))
    if s == "encoding":
        return stage_set(key, "encoding", 0.5, "Склейка видео и звука", frames_done=frames, frames_total=frames, eta=None)


def pack_scene_zip(scene: dict, zpath: Path) -> dict:
    """Все файлы сцены (звук, картинки, части кукол, скрипты сцен) -> один zip с относительными путями."""
    import zipfile
    names: dict[str, str] = {}

    def add(z, src: str, folder: str) -> str:
        if src not in names:
            names[src] = f"{folder}/{len(names)}{Path(src).suffix}"
            z.write(src, names[src])
        return names[src]

    scene = json.loads(json.dumps(scene))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for sc in scene["scenes"]:
            cust = sc.get("custom") or {}
            if cust.get("script"):
                cust["script"] = add(z, cust["script"], "s")
            for ln in sc["lines"]:
                for k, folder in (("audio", "a"), ("image", "i")):
                    if ln.get(k):
                        ln[k] = add(z, ln[k], folder)
                meta = ln.get("meta") or {}
                if meta.get("faces"):
                    meta["faces"] = [add(z, p, "f") for p in meta["faces"]]
                if ln.get("cutout"):
                    first = next(iter(ln["cutout"].values()))
                    if first not in names:                           # папка куклы целиком (части + layout.json)
                        d = f"c/{len(names)}"
                        for k2, v in ln["cutout"].items():
                            names[v] = f"{d}/{k2}.png"
                            z.write(v, names[v])
                        lay = Path(first).parent / "layout.json"
                        if lay.exists():
                            z.write(lay, f"{d}/layout.json")
                    ln["cutout"] = {k2: names[v] for k2, v in ln["cutout"].items()}
        z.writestr("scene.json", json.dumps(scene, ensure_ascii=False))
    return scene


async def run_remote(key: str, path: str, pdir: Path, scene: dict, out_mp4: Path, preview: bool, total: int) -> None:
    """Рендер на VPS: отправляем zip, следим за этапами, умеем продолжить с готовых кадров и отменить."""
    hdr = {"Authorization": f"Bearer {RENDER_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
    rid = JOBS.get(key, {}).get("rid")
    async with aiohttp.ClientSession(headers=hdr, timeout=timeout) as s:
        try:
            if rid:                                              # продолжение: сервер хранит уже готовые кадры
                async with s.post(f"{RENDER_URL}/resume/{rid}") as r:
                    if r.status != 200:
                        rid = None
                    else:
                        job_set(key, text="Продолжаю с готовых кадров")
            if not rid:
                stage_set(key, "queued", 0, "Отправляю сцену на сервер рендера")
                zpath = pdir / "render_in.zip"
                await asyncio.to_thread(pack_scene_zip, scene, zpath)
                with open(zpath, "rb") as f:
                    async with s.post(f"{RENDER_URL}/render", params={"preview": int(preview), "total": total},
                                      data=f) as r:
                        if r.status != 200:
                            raise RenderFailed(f"Сервер рендера: {r.status} {(await r.text())[:300]}")
                        rid = (await r.json())["id"]
                zpath.unlink(missing_ok=True)
            job_set(key, rid=rid)
            await fb_update(path, {"rid": rid})
            fails = 0
            while True:
                await asyncio.sleep(2)
                try:
                    async with s.get(f"{RENDER_URL}/status/{rid}") as r:
                        st = await r.json()
                    fails = 0
                except Exception:
                    fails += 1
                    job_set(key, text="Нет связи с сервером рендера, пробую снова…")
                    if fails > 30:
                        raise RenderFailed("Сервер рендера не отвечает")
                    continue
                if st["status"] in ("error", "cancelled"):
                    job_set(key, log=st.get("tail") or [])
                    raise RenderFailed(st.get("error") or "Рендер остановлен на сервере")
                if st["status"] == "done":
                    break
                apply_remote_status(key, st)
            stage_set(key, "upload", 0, "Скачиваю готовое видео")
            async with s.get(f"{RENDER_URL}/result/{rid}") as r:
                if r.status != 200:
                    raise RenderFailed("Не удалось скачать видео с сервера рендера")
                with open(out_mp4, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
        except asyncio.CancelledError:
            if rid:
                try:
                    async with aiohttp.ClientSession(headers=hdr, timeout=aiohttp.ClientTimeout(total=10)) as s2:
                        await s2.post(f"{RENDER_URL}/cancel/{rid}")
                except Exception:
                    log.warning("remote cancel failed")
            raise


async def run_local(key: str, pdir: Path, scene_json: Path, out_mp4: Path, preview: bool, total: int) -> None:
    """Blender на этой же машине (если RENDER_URL не задан). Кадры лежат в pdir/frames — можно продолжить."""
    worker = Path(__file__).with_name("blender_worker.py")
    frames_dir = pdir / "frames"
    cmd = [BLENDER_BIN, "-b", "-P", str(worker), "--", "--scene", str(scene_json), "--out", str(out_mp4),
           "--frames", str(frames_dir)]
    if preview:
        cmd.append("--preview")
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL", *cmd]
    out_mp4.unlink(missing_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    tail: deque = deque(maxlen=25)
    state = {"total": total, "t0": None, "d0": 0, "stage": "building"}
    step = 3 if preview else 2

    def count() -> int:
        try:
            return sum(1 for _ in frames_dir.glob("f_*.png"))
        except Exception:
            return 0

    async def ticker():
        while True:
            await asyncio.sleep(2)
            if state["stage"] != "rendering":
                continue
            frames = max(1, -(-state["total"] // step))
            done = min(frames, count())
            eta = None
            if state["t0"] and time.time() - state["t0"] > 8 and done > state["d0"]:
                eta = int((frames - done) / ((done - state["d0"]) / (time.time() - state["t0"])))
            stage_set(key, "rendering", done / frames, f"Рендер кадров: {done} из {frames}", frames_done=done,
                      frames_total=frames, eta=eta, log=list(tail)[-6:])

    tk = asyncio.create_task(ticker())
    try:
        stage_set(key, "building", 0.3, "Blender загружает сцену, строит героев и декорации")
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").rstrip()
            if not line:
                continue
            if not line.startswith("Fra:"):
                tail.append(line[:200])
            if (m := TOT_RE.search(line)):
                state["total"] = int(m.group(1))
            if (m := WARN_RE.search(line)):
                job_set(key, warn=m.group(1)[:300])
            if (m := STAGE_RE.search(line)):
                st = m.group(1)
                if st in ("building", "rendering", "encoding"):
                    state["stage"] = st
                    if st == "rendering":
                        state["t0"], state["d0"] = time.time(), count()
                    elif st == "encoding":
                        stage_set(key, "encoding", 0.5, "Склейка видео и звука")
        await proc.wait()
    except asyncio.CancelledError:
        try:
            os.killpg(proc.pid, 15)
            try:
                await asyncio.wait_for(proc.wait(), 4)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        raise
    finally:
        tk.cancel()
    if proc.returncode != 0 or not out_mp4.exists():
        job_set(key, log=list(tail)[-6:])
        raise RenderFailed("Blender error:\n" + "\n".join(tail)[-1500:])
    shutil.rmtree(frames_dir, ignore_errors=True)


async def tts_cached(text: str, voice: str, wav: Path) -> float:
    """Озвучка с кэшем: при повторе рендера готовые реплики не синтезируются заново."""
    meta = wav.with_suffix(".json")
    try:
        m = json.loads(meta.read_text())
        if m.get("text") == text and m.get("voice") == voice and wav.exists() and wav.stat().st_size > 100:
            return float(m["dur"])
    except Exception:
        pass
    dur = await tts.tts_line(text, voice, wav)
    meta.write_text(json.dumps({"text": text, "voice": voice, "dur": dur}))
    return dur


def resolve_scene_pool(setting: str, pool: dict):
    """Сцена из библиотеки: имя сцены встречается в описании места (« [Сцена: Улица] » или «@Улица»)."""
    low = (setting or "").lower()
    best = None
    for n, v in pool.items():
        if n.lower() in low and (best is None or len(n) > len(best[0])):
            best = (n, v)
    return best


async def start_render(uid: str, scenes: list[dict], style: str, preview: bool, engine: str,
                       voices: dict | None = None, notes: dict | None = None) -> tuple[str, asyncio.Task, str]:
    """Проверяет входные данные и запускает рендер в фоне. Возвращает (id проекта, задача, название).
    Ошибки входных данных — RuntimeError с понятным текстом."""
    voices = voices or {}
    notes = notes or {}
    chars = await fb_get(f"clients/{uid}/characters") or {}
    lib = await fb_get("library/characters") or {}
    my_scenes = {**(await fb_get("library/scenes") or {}), **(await fb_get(f"clients/{uid}/scenes") or {})}
    settings = await get_tts_settings(uid)
    if engine == "piper" and not tts.piper_available():
        raise RuntimeError("Piper не установлен на сервере. Выберите ElevenLabs или добавьте piper-tts в requirements.txt.")
    if engine == "eleven" and not tts.ELEVEN_KEY:
        raise RuntimeError("Не задан ключ ElevenLabs. Выберите озвучку Piper.")

    # Герои: загруженные (лист / кукла из архива) или нарисованные Blender'ом по сценарию (3D)
    cast = scenario.build_cast(scenes, notes)
    catalog = (await tts.voice_catalog(engine)).get("voices", [])
    cyr = scenario.is_cyrillic(" ".join(ln["text"] for sc in scenes for ln in sc["lines"]))
    speakers = list(dict.fromkeys(ln["who"] for sc in scenes for ln in sc["lines"]))
    taken: list[str] = []
    plan = {}
    for who in speakers:
        narrator = any(ln.get("narrator") and ln["who"] == who for sc in scenes for ln in sc["lines"])
        real, ch = (None, None) if narrator else find_char(chars, who)
        if ch is None and not narrator:
            real, ch = find_char(lib, who)
        legacy = "eleven:" + ELEVEN_VOICES[who] if who in ELEVEN_VOICES else ""
        explicit = tts.resolve_voice(engine, voices.get(who, ""), (ch or {}).get("voice", ""), legacy, "")
        has_explicit = bool(tts.split_key(explicit)[1]) and explicit != tts.default_voice(engine)
        if has_explicit:
            voice = explicit
        elif len(speakers) == 1:
            voice = tts.resolve_voice(engine, settings["voice"] if settings["engine"] == engine else "")
        else:
            spec = {"name": who, "gender": "m"} if narrator else cast.get(who, {"name": who})
            voice = scenario.pick_voice(spec, catalog, taken, cyr) or tts.default_voice(engine)
        taken.append(voice)
        plan[who] = [real or who, ch, voice]

    scene_refs = []                                  # какая готовая сцена к какой сцене сценария
    for sc in scenes:
        hit = resolve_scene_pool(sc.get("setting", ""), my_scenes)
        scene_refs.append({"name": hit[0], **hit[1]} if hit else None)

    pid = uuid.uuid4().hex[:8]
    key = f"{uid}/p/{pid}"
    title = (scenes[0]["setting"] or "Мультфильм")[:40].capitalize()
    pdir = WORK / uid / pid
    pdir.mkdir(parents=True, exist_ok=True)
    req = {"scenes": scenes, "style": style, "preview": preview, "plan": plan, "cast": cast, "scene_refs": scene_refs,
           "title": title}
    (pdir / "request.json").write_text(json.dumps(req, ensure_ascii=False))
    job_set(key, kind="proj", pct=0, text="В очереди", status="queued", stage="queued", error="", log=[],
            started=time.time(), frames_done=0, frames_total=0, eta=None, can_resume=False, warn="")
    await fb_set(f"clients/{uid}/projects/{pid}",
                 {"status": "queued", "preview": preview, "created": int(time.time()),
                  "title": title, "engine": engine})
    return pid, launch_render(uid, pid, req), title


def launch_render(uid: str, pid: str, req: dict) -> asyncio.Task:
    t = spawn(_render_task(uid, pid, req))
    job_set(f"{uid}/p/{pid}", task=t)
    return t


async def retry_render(uid: str, pid: str) -> asyncio.Task:
    """Повтор после ошибки/отмены: продолжаем с места остановки (готовые реплики и кадры не пересчитываются)."""
    key = f"{uid}/p/{pid}"
    j = JOBS.get(key, {})
    if j.get("status") in ("queued", "rendering"):
        raise RuntimeError("Рендер уже идёт.")
    rq = WORK / uid / pid / "request.json"
    if not rq.exists():
        raise RuntimeError("Данные проекта пропали после перезапуска сервера. Запустите рендер заново.")
    if "rid" not in j:
        pr = await fb_get(f"clients/{uid}/projects/{pid}") or {}
        if pr.get("rid"):
            j["rid"] = pr["rid"]
    req = json.loads(rq.read_text())
    job_set(key, kind="proj", status="queued", stage="queued", text="Продолжаю…", error="", can_resume=False,
            started=time.time(), rid=j.get("rid"), log=[], warn="")
    await fb_update(f"clients/{uid}/projects/{pid}", {"status": "queued", "error": ""})
    return launch_render(uid, pid, req)


async def cancel_render(uid: str, pid: str) -> bool:
    j = JOBS.get(f"{uid}/p/{pid}")
    t = j.get("task") if j else None
    if not t or t.done():
        return False
    t.cancel()
    job_set(f"{uid}/p/{pid}", text="Останавливаю…")
    return True


async def _render_task(uid: str, pid: str, req: dict) -> None:
    key = f"{uid}/p/{pid}"
    pdir = WORK / uid / pid
    pdir.mkdir(parents=True, exist_ok=True)
    path = f"clients/{uid}/projects/{pid}"
    scenes, style, preview, plan, cast = req["scenes"], req["style"], req["preview"], req["plan"], req["cast"]
    try:
        async with RENDER_SEM:
            job_set(key, status="rendering")
            stage_set(key, "tts", 0, "Готовлю героев и озвучку")
            await fb_update(path, {"status": "rendering"})
            ready = {}
            for who, (real, ch, voice) in plan.items():
                if ch is not None:
                    ready[who] = await ensure_character(uid, real, ch)

            n_lines = sum(len(sc["lines"]) for sc in scenes)
            done_lines, total_frames, scene_out = 0, 0, []
            for si, sc in enumerate(scenes):
                lines_out = []
                for li, ln in enumerate(sc["lines"]):
                    wav = pdir / f"s{si}_l{li}.wav"
                    voice = plan[ln["who"]][2]
                    stage_set(key, "tts", done_lines / n_lines,
                              f"Озвучиваю {done_lines + 1}/{n_lines}: {ln['who']} ({voice.split(':', 1)[0]})")
                    dur = await tts_cached(ln["text"], voice, wav)
                    ch = ready.get(ln["who"]) or {}
                    entry = {**ln, "audio": str(wav), "duration": dur, "image": None, "meta": None}
                    if ch.get("kind") == "cutout":
                        entry["cutout"] = ch["parts"]
                    else:
                        entry.update(image=ch.get("file"), meta=ch.get("meta"))
                    lines_out.append(entry)
                    total_frames += int(dur * FPS) + 10
                    done_lines += 1
                ref = (req.get("scene_refs") or [None] * len(scenes))[si]
                sc_out = {"setting": sc["setting"], "env": sc.get("env"), "lines": lines_out}
                if ref:
                    script = await ensure_scene_file(uid, ref)
                    if script:
                        sc_out["custom"] = {"script": script, "name": ref["name"]}
                scene_out.append(sc_out)
                total_frames += 42                                   # выход героев + пауза в конце сцены
            total_frames = max(total_frames, 24)

            scene = {"style": STYLE_ALIASES.get(style, style), "cast": cast, "scenes": scene_out}
            scene_json = pdir / "scene.json"
            scene_json.write_text(json.dumps(scene, ensure_ascii=False))
            job_set(key, frames_total=-(-total_frames // (3 if preview else 2)))
            stage_set(key, "building", 0, "Подготовка сцены")
            out = pdir / "result.mp4"
            if RENDER_URL:
                await run_remote(key, path, pdir, scene, out, preview, total_frames)
            else:
                await run_local(key, pdir, scene_json, out, preview, total_frames)

        stage_set(key, "upload", 0.3, "Сохраняю видео")
        remote = None
        try:
            remote = await yadisk_upload(out, f"/CartoonStudio/{uid}/projects/{pid}/result.mp4")
        except Exception:
            log.exception("yadisk upload failed")
        await fb_update(path, {"status": "done", "file": str(out), "disk": remote,
                               "dur": int(total_frames / FPS), "finished": int(time.time()), "rid": None})
        job_set(key, pct=100, text="Готово", status="done", stage="done", task=None, can_resume=False)
    except asyncio.CancelledError:
        await fb_update(path, {"status": "cancelled", "error": ""})
        job_set(key, status="cancelled", stage="cancelled", text="Рендер остановлен", error="", task=None,
                can_resume=True)
    except Exception as e:
        log.exception("render failed")
        err = str(e)[:500]
        await fb_update(path, {"status": "error", "error": err})
        job_set(key, status="error", stage="error", error=err, text="Ошибка", task=None, can_resume=True)


async def fit_telegram(src: Path) -> Path:
    """Если видео больше лимита Telegram (50 МБ) — пережимает под лимит."""
    if src.stat().st_size <= TG_LIMIT:
        return src
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src),
            stdout=asyncio.subprocess.PIPE)
        out, _ = await probe.communicate()
    except FileNotFoundError:
        raise RuntimeError("Видео больше 50 МБ, а на сервере нет ffmpeg, чтобы его сжать.")
    dur = max(1.0, float(out.decode().strip() or 1))
    kbps = max(200, int(46 * 8192 / dur) - 96)
    dst = src.with_name("result_tg.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if kbps < 1500:
        cmd += ["-vf", "scale=-2:720"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst)]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await p.wait()
    if p.returncode != 0 or not dst.exists():
        raise RuntimeError("Видео больше 50 МБ, а сжать его не получилось (нет ffmpeg).")
    return dst


async def send_project_video(bot: Bot, uid: str, pid: str, chat_id: int, caption: str = "") -> None:
    pr = await fb_get(f"clients/{uid}/projects/{pid}")
    if not pr or pr.get("status") != "done":
        raise RuntimeError("Видео ещё не готово.")
    path = Path(pr.get("file") or WORK / uid / pid / "result.mp4")
    if not path.exists() and pr.get("disk"):
        await yadisk_download(pr["disk"], path)
    if not path.exists():
        raise RuntimeError("Файл видео не найден на сервере (он мог удалиться после перезапуска).")
    path = await fit_telegram(path)
    await bot.send_video(chat_id, FSInputFile(path), supports_streaming=True,
                         caption=caption or f"🎬 {pr.get('title', 'Мультфильм')}")


async def watch(msg: Message, key: str, task: asyncio.Task, head: str, markup=None) -> None:
    """Пока задача идёт — обновляет сообщение в чате: этап + проценты."""
    last = ""
    while not task.done():
        await asyncio.sleep(2)
        j = JOBS.get(key, {})
        eta = f" · ещё ~{j['eta'] // 60}:{j['eta'] % 60:02d}" if j.get("eta") else ""
        text = f"{head}\n{j.get('text', '')}{eta}\n{bar(int(j.get('pct', 0)))}"
        if text != last:
            last = text
            await safe_edit(msg, text, markup)


@router.callback_query(F.data.startswith("stop:"))
async def stop_cb(c: CallbackQuery):
    ok = await cancel_render(str(c.from_user.id), c.data.split(":")[1])
    await c.answer("Останавливаю…" if ok else "Рендер уже не идёт")


@router.callback_query(F.data.startswith("retry:"))
async def retry_cb(c: CallbackQuery, bot: Bot):
    uid, pid = str(c.from_user.id), c.data.split(":")[1]
    try:
        task = await retry_render(uid, pid)
    except RuntimeError as e:
        return await c.answer(str(e)[:190], show_alert=True)
    await c.answer("Продолжаю с того места, где остановились")
    head = "🎬 Продолжаю рендер"
    status = await c.message.answer(f"{head}\n{bar(0)}")
    await watch(status, f"{uid}/p/{pid}", task, head, stop_kb(pid))
    try:
        await task
    except asyncio.CancelledError:
        pass
    j = JOBS.get(f"{uid}/p/{pid}", {})
    if j.get("status") == "done":
        await safe_edit(status, f"✅ Готово\n{bar(100)}")
        try:
            await send_project_video(bot, uid, pid, c.message.chat.id)
        except Exception as e:
            await c.message.answer(f"Видео готово, но отправить не вышло: {e}")
    else:
        await safe_edit(status, "⏹ Остановлен" if j.get("status") == "cancelled" else f"❌ Ошибка: {j.get('error', '')[:600]}",
                        retry_kb(pid))


def stop_kb(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ Остановить", callback_data=f"stop:{pid}")]])


def retry_kb(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Продолжить", callback_data=f"retry:{pid}")]])


# ───────────────────────────── чат: старт, промпт ─────────────────────────────
@router.message(Command("start"))
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await set_menu_button(bot, m.chat.id)
    uid = str(m.from_user.id)
    if not await fb_get(f"clients/{uid}"):
        await fb_set(f"clients/{uid}", {"name": m.from_user.full_name, "created": int(time.time())})
    await m.answer("🎬 TajStudio. Загрузи персонажей, выбери озвучку, напиши сценарий — получишь мультфильм.\n"
                   "Всё то же самое есть в приложении — кнопка TajStudio слева от поля ввода.",
                   reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_menu())


@router.callback_query(F.data == "prompt")
async def show_prompt(c: CallbackQuery):
    await c.message.answer(
        "Скопируй, вставь в ChatGPT вместе со своим фото, результат пришли мне как файл:\n\n"
        f"<code>{CHARACTER_PROMPT}</code>", parse_mode="HTML")
    await c.answer()


# ───────────────────────────── чат: персонажи ─────────────────────────────
@router.callback_query(F.data == "chars")
async def chars_menu(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    lib = await fb_get("library/characters") or {}
    rows = [[InlineKeyboardButton(text=f"🗑 Удалить «{n}»", callback_data=f"delask:{cid(n)}")] for n in mine]
    rows.append([InlineKeyboardButton(text="➕ Загрузить персонажа", callback_data="addchar")])
    await c.message.answer(f"Твои: {', '.join(mine) or 'пока нет'}\nБиблиотека: {', '.join(lib) or 'пусто'}",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("delask:"))
async def delete_ask(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    name = next((n for n in mine if cid(n) == c.data.split(":")[1]), None)
    if not name:
        return await c.answer("Персонаж уже удалён", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, удалить", callback_data=f"deldo:{cid(name)}"),
        InlineKeyboardButton(text="Отмена", callback_data="chars")]])
    await c.message.answer(f"Удалить персонажа «{name}»? Это нельзя отменить.", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("deldo:"))
async def delete_do(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    name = next((n for n in mine if cid(n) == c.data.split(":")[1]), None)
    if name:
        await delete_character(uid, name)
    await safe_edit(c.message, f"🗑 Персонаж «{name}» удалён." if name else "Персонаж уже удалён.")
    await c.answer()


@router.callback_query(F.data == "addchar")
async def addchar(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.waiting_character)
    await c.message.answer("Пришли PNG персонажа как ФАЙЛ (скрепка → Файл, не фото — иначе сожмётся). "
                           "Потом я спрошу имя.")
    await c.answer()


@router.message(F.document | F.photo)
async def got_file(m: Message, state: FSMContext, bot: Bot):
    """Файл персонажа в любой момент: качаем, спрашиваем имя, создаём с процентами."""
    cur = await state.get_state()
    if cur in (Flow.waiting_script.state, Flow.choosing_style.state):
        return await m.answer("Сейчас жду сценарий. Для загрузки персонажа: «Мои персонажи» → «Загрузить».")
    is_zip = bool(m.document and ((m.document.file_name or "").lower().endswith(".zip")
                                  or "zip" in (m.document.mime_type or "")))
    if m.document and not is_zip and not (m.document.mime_type or "").startswith("image/"):
        return await m.answer("Пришли PNG/JPG листа персонажа или ZIP с готовым персонажем/сценой.")
    uid = str(m.from_user.id)
    tmp = WORK / uid / "incoming" / f"{uuid.uuid4().hex}{'.zip' if is_zip else '.png'}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        await bot.download(m.document or m.photo[-1], destination=tmp)
    except Exception:
        return await m.answer("Не смог скачать файл (Telegram отдаёт боту файлы до 20 МБ). Уменьши картинку.")
    kind = None
    if is_zip:
        try:
            kind = await asyncio.to_thread(packs.detect, tmp)
        except Exception:
            kind = None
        if kind is None:
            tmp.unlink(missing_ok=True)
            return await m.answer("В архиве не нашёл ни частей персонажа (head.png, torso.png…), ни скрипта сцены (.py).")
    note = "\n(Это фото, а не файл — качество ниже. Лучше слать как файл.)" if m.photo else ""
    if kind == "scene":
        note = "\n🏙 Это готовая сцена. В сценарии пиши её имя в описании места: [Сцена: Имя] или @Имя."
    elif kind == "cutout":
        note = "\n🧍 Это готовый персонаж из частей (PNG)."
    caption = clean_name(m.caption or "")
    if caption:
        return await create_in_chat(m, state, uid, caption, tmp, kind)
    await state.set_state(Flow.waiting_name)
    await state.update_data(tmp=str(tmp), kind=kind)
    await m.answer("✅ Файл получен." + note + "\n\n✏️ Как назвать? Напиши имя — "
                   "для персонажа оно же в сценарии («Имя: реплика»).")


@router.message(Flow.waiting_character, F.text)
async def need_file(m: Message):
    await m.answer("Жду файл с картинкой (скрепка → Файл). Отмена: /cancel")


@router.message(Flow.waiting_name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = clean_name(m.text)
    if not name:
        return await m.answer("Имя не подошло. Напиши короткое имя (до 30 символов), например: Маша")
    data = await state.get_data()
    tmp = Path(data.get("tmp", ""))
    if not tmp.exists():
        await state.clear()
        return await m.answer("Файл потерялся, пришли его ещё раз.", reply_markup=main_menu())
    await create_in_chat(m, state, str(m.from_user.id), name, tmp, data.get("kind"))


async def create_in_chat(m: Message, state: FSMContext, uid: str, name: str, tmp: Path, kind: str | None = None):
    await state.clear()
    if kind == "scene":
        zp = WORK / uid / "scenes" / f"{cid(name)}.zip"
        zp.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), zp)
        status = await m.answer(f"⏳ Добавляю сцену «{name}»\n{bar(0)}")
        key = f"{uid}/s/{name}"
        task = spawn(create_scene(uid, name, zp, int(uid) in ADMIN_IDS))
        await watch(status, key, task, f"⏳ Добавляю сцену «{name}»")
        if await task:
            await safe_edit(status, f"✅ Сцена «{name}» добавлена")
            await m.answer(f"В сценарии укажи: [Сцена: {name}] — и герои встанут на эту сцену.", reply_markup=main_menu())
        else:
            await safe_edit(status, f"❌ Не получилось: {JOBS[key].get('error')}")
        return
    orig = WORK / uid / "characters" / f"{name}{'.zip' if kind == 'cutout' else '.png'}"
    orig.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), orig)
    key = f"{uid}/c/{name}"
    if JOBS.get(key, {}).get("status") == "processing":
        return await m.answer(f"«{name}» уже создаётся, подожди.")
    head = f"⏳ Создаю персонажа «{name}»"
    status = await m.answer(f"{head}\n{bar(0)}")
    task = spawn(create_character(uid, name, orig, int(uid) in ADMIN_IDS))    # админ из чата -> ещё и в библиотеку
    await watch(status, key, task, head)
    if await task:
        await safe_edit(status, f"✅ Персонаж «{name}» создан\n{bar(100)}")
        await m.answer("Готовый персонаж собран из частей." if kind == "cutout" else
                       "Нашёл тело и 9 выражений лица — скелет и анимацию добавлю при рендере.",
                       reply_markup=main_menu())
    else:
        await safe_edit(status, f"❌ Не получилось создать «{name}»: {JOBS[key].get('error')}")


# ───────────────────────────── чат: озвучка ─────────────────────────────
@router.callback_query(F.data == "voice")
async def voice_menu(c: CallbackQuery):
    cur = await get_tts_settings(str(c.from_user.id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if cur["engine"] == e else "") + t, callback_data=f"eng:{e}")]
        for e, t in ENGINE_TITLES.items()])
    await c.message.answer(f"🎙 Озвучка. Сейчас: {ENGINE_TITLES[cur['engine']]}\nВыбери способ:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("eng:"))
async def pick_engine(c: CallbackQuery):
    engine = c.data.split(":")[1]
    cat = await tts.voice_catalog(engine)
    if not cat["ok"]:
        return await c.message.answer(f"⚠️ {cat['error']}")
    cur = await get_tts_settings(str(c.from_user.id))
    btns = [InlineKeyboardButton(text=("✅ " if v["key"] == cur["voice"] else "") + v["name"],
                                 callback_data=f"vs:{engine}:{i}")
            for i, v in enumerate(cat["voices"][:24])]
    kb = InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])
    await c.message.answer(f"Голоса — {ENGINE_TITLES[engine]}. Выбери голос, и я сразу озвучу пример:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("vs:"))
async def pick_voice(c: CallbackQuery, bot: Bot):
    _, engine, idx = c.data.split(":")
    cat = await tts.voice_catalog(engine)
    voices = cat["voices"]
    if not cat["ok"] or int(idx) >= len(voices):
        return await c.answer("Список голосов изменился, открой «Озвучка» заново", show_alert=True)
    v = voices[int(idx)]
    await c.answer("Готовлю голос…")
    wait = await c.message.answer(f"⏳ Озвучиваю пример голосом «{v['name']}»…" +
                                  ("\nПервый раз Piper скачивает модель — это до минуты." if not v.get("ready") else ""))
    try:
        wav = await tts.make_sample(v["key"])
        await save_tts_settings(str(c.from_user.id), engine, v["key"])
        ogg = await tts.wav_to_ogg(wav)
        cap = f"✅ Выбран голос: {v['name']} — {ENGINE_TITLES[engine]}"
        if ogg:
            await bot.send_voice(c.message.chat.id, FSInputFile(ogg), caption=cap)
        else:
            await bot.send_document(c.message.chat.id, FSInputFile(wav), caption=cap)
        await wait.delete()
    except Exception as e:
        log.exception("voice sample failed")
        await safe_edit(wait, f"❌ {str(e)[:500]}")


# ───────────────────────────── чат: новый мультфильм ─────────────────────────────
@router.callback_query(F.data == "new")
async def new_project(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.waiting_script)
    await c.message.answer(
        "Пришли сценарий текстом:\n\n<code>[Персонаж: Маша — девочка 8 лет, рыжие косички, красное платье]\n"
        "[Персонаж: Барсик — рыжий кот]\n[Сцена: лес, утро]\nМаша: (радостно, машет рукой) Привет, Барсик!\n"
        "Барсик: Мяу! Ничего себе, как тут красиво!</code>\n\n"
        "Загружать персонажей не обязательно: если имени нет в «Мои персонажи», Blender сам нарисует 3D-героя "
        "по имени и описанию в [Персонаж: …], а место и время суток возьмёт из [Сцена: …]. "
        "«Рассказчик:» — закадровый голос.",
        parse_mode="HTML")
    await c.answer()


@router.message(Flow.waiting_script, F.text)
async def got_script(m: Message, state: FSMContext):
    scenes, notes = parse_script(m.text)
    if not scenes or not any(s["lines"] for s in scenes):
        return await m.answer("Не нашёл реплик. Формат: «Имя: текст».")
    await state.update_data(scenes=scenes, notes=notes)
    await state.set_state(Flow.choosing_style)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=v, callback_data=f"style:{k}")] for k, v in STYLES.items()])
    await m.answer(f"Сцен: {len(scenes)}. Выбери стиль:", reply_markup=kb)


@router.callback_query(Flow.choosing_style, F.data.startswith("style:"))
async def style_chosen(c: CallbackQuery, state: FSMContext):
    await state.update_data(style=c.data.split(":")[1])
    cur = await get_tts_settings(str(c.from_user.id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("⭐ " if cur["engine"] == e else "") + t, callback_data=f"tts:{e}")]
        for e, t in ENGINE_TITLES.items()])
    await c.message.answer("Чем озвучить? (голос берётся из настроек «🎙 Озвучка»)", reply_markup=kb)
    await c.answer()


@router.callback_query(Flow.choosing_style, F.data.startswith("tts:"))
async def tts_chosen(c: CallbackQuery, state: FSMContext):
    await state.update_data(engine=c.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Предпросмотр (бесплатно)", callback_data="go:preview")],
        [InlineKeyboardButton(text="🎞 Полный рендер", callback_data="go:full")]])
    await c.message.answer("Начнём с предпросмотра — он быстрый и не списывает баланс.", reply_markup=kb)
    await c.answer()


@router.callback_query(Flow.choosing_style, F.data.startswith("go:"))
async def go(c: CallbackQuery, state: FSMContext, bot: Bot):
    preview = c.data.endswith("preview")
    data = await state.get_data()
    await state.clear()
    await c.answer()
    uid = str(c.from_user.id)
    engine = data.get("engine") or (await get_tts_settings(uid))["engine"]
    try:
        pid, task, title = await start_render(uid, data["scenes"], data.get("style", "classic"), preview, engine,
                                                  notes=data.get("notes"))
    except RuntimeError as e:
        return await c.message.answer(f"❌ {e}")
    started = time.time()
    head = f"🎬 «{title}» — {'предпросмотр' if preview else 'рендер'}"
    status = await c.message.answer(f"{head}\n{bar(0)}")
    await watch(status, f"{uid}/p/{pid}", task, head, stop_kb(pid))
    try:
        await task
    except asyncio.CancelledError:
        pass
    j = JOBS.get(f"{uid}/p/{pid}", {})
    if j.get("status") == "cancelled":
        return await safe_edit(status, "⏹ Рендер остановлен. Готовые кадры сохранены.", retry_kb(pid))
    if j.get("status") == "done":
        await safe_edit(status, f"✅ Готово за {int(time.time() - started)} с\n{bar(100)}")
        try:
            await send_project_video(bot, uid, pid, c.message.chat.id,
                                     f"✅ {'Предпросмотр' if preview else 'Финал'} · {title}")
        except Exception as e:
            await c.message.answer(f"Видео готово, но отправить не вышло: {e}\nСкачайте его в приложении → Проекты.")
    else:
        await safe_edit(status, f"❌ Ошибка: {j.get('error', 'неизвестная')[:600]}", retry_kb(pid))


@router.callback_query(F.data == "projects")
async def projects(c: CallbackQuery):
    pr = await fb_get(f"clients/{c.from_user.id}/projects") or {}
    if not pr:
        await c.message.answer("Проектов пока нет.")
        return await c.answer()
    rows, items = [], sorted(pr.items(), key=lambda kv: -kv[1].get("created", 0))[:15]
    names = {"done": "готов", "rendering": "рендерится", "queued": "в очереди", "error": "ошибка", "cancelled": "остановлен"}
    for k, v in items:
        st = names.get(v.get("status"), v.get("status"))
        if v.get("status") == "done":
            rows.append([InlineKeyboardButton(text=f"📥 {v.get('title', k)} · {st}", callback_data=f"send:{k}")])
    txt = "\n".join(f"• {v.get('title', k)} — {names.get(v.get('status'), v.get('status'))}" for k, v in items)
    await c.message.answer(txt + ("\n\nНажми на проект, чтобы получить видео:" if rows else ""),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    await c.answer()


@router.callback_query(F.data.startswith("send:"))
async def send_cb(c: CallbackQuery, bot: Bot):
    await c.answer("Отправляю видео…")
    try:
        await send_project_video(bot, str(c.from_user.id), c.data.split(":")[1], c.message.chat.id)
    except Exception as e:
        await c.message.answer(f"❌ {e}")


# ───────────────────────────── HTTP API для Mini App ─────────────────────────────
def verify_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram: возвращает данные пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def auth(request: web.Request) -> dict:
    u = verify_init_data(request.headers.get("X-Init-Data", ""))
    if not u:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    return u


@web.middleware
async def api_middleware(request: web.Request, handler):
    """CORS (Mini App может жить на другом домене) + JSON-ошибки."""
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        except Exception as e:
            log.exception("api error")
            resp = err(str(e)[:300], 500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _gc_jobs() -> None:
    now = time.time()
    for k in [k for k, j in JOBS.items() if j.get("status") in ("done", "error", "cancelled") and now - j.get("upd", now) > 900]:
        JOBS.pop(k, None)


async def api_me(request: web.Request) -> web.Response:
    u = auth(request)
    uid = str(u["id"])
    _gc_jobs()
    client = await fb_get(f"clients/{uid}") or {}
    lib = await fb_get("library/characters") or {}
    chars = client.get("characters") or {}
    projects = client.get("projects") or {}
    scenes_db = client.get("scenes") or {}
    lib_scenes = await fb_get("library/scenes") or {}

    characters = {}
    for n, v in chars.items():
        characters[n] = {"name": n, "voice": v.get("voice", ""), "status": "ready", "pct": 100, "text": "",
                         "error": "", "thumb": bool(v.get("thumb")), "kind": v.get("kind", "sheet")}
    for k, j in JOBS.items():                       # персонажи, которые прямо сейчас создаются / упали
        if k.startswith(uid + "/c/") and j["status"] in ("processing", "error"):
            n = j["name"]
            characters[n] = {**characters.get(n, {"voice": "", "thumb": False}), "name": n, "status": j["status"],
                             "pct": j.get("pct", 0), "text": j.get("text", ""), "error": j.get("error", "")}

    scenes_out = {n: {"name": n, "title": v.get("title", ""), "status": "ready", "pct": 100, "text": "", "error": "",
                      "lib": False} for n, v in scenes_db.items()}
    for n, v in lib_scenes.items():
        scenes_out.setdefault(n, {"name": n, "title": v.get("title", ""), "status": "ready", "pct": 100, "text": "",
                                  "error": "", "lib": True})
    for k, j in JOBS.items():
        if k.startswith(uid + "/s/") and j["status"] in ("processing", "error"):
            n = j["name"]
            scenes_out[n] = {**scenes_out.get(n, {"title": "", "lib": False}), "name": n, "status": j["status"],
                             "pct": j.get("pct", 0), "text": j.get("text", ""), "error": j.get("error", "")}

    plist = []
    for k, v in projects.items():
        j = JOBS.get(f"{uid}/p/{k}")
        status = v.get("status", "done")
        if status in ("queued", "rendering") and not j:            # бот перезапускался посреди рендера
            status = "error"
            v["error"] = "Рендер прервался (перезапуск сервера). Нажмите «Продолжить»."
            await fb_update(f"clients/{uid}/projects/{k}", {"status": "error", "error": v["error"]})
        if j and j.get("kind") == "proj":
            status = j["status"]
        live = status in ("queued", "rendering")
        j = j or {}
        resumable = status in ("error", "cancelled") and ((WORK / uid / k / "request.json").exists())
        plist.append({"id": k, "title": v.get("title") or f"Проект {k}", "status": status,
                      "mode": "preview" if v.get("preview") else "render", "created": v.get("created", 0),
                      "pct": j.get("pct", 0) if status != "done" else 100,
                      "text": j.get("text", ""), "error": j.get("error") or v.get("error", ""),
                      "stage": j.get("stage", status), "frames_done": j.get("frames_done", 0),
                      "frames_total": j.get("frames_total", 0), "eta": j.get("eta") if live else None,
                      "elapsed": int(time.time() - j["started"]) if live and j.get("started") else 0,
                      "log": (j.get("log") or [])[-5:], "warn": j.get("warn", ""),
                      "can_retry": resumable or (status in ("error", "cancelled") and bool(j.get("can_resume"))),
                      "dur": v.get("dur", 0), "engine": v.get("engine", "")})
    plist.sort(key=lambda p: -p["created"])

    return web.json_response({
        "admin": u["id"] in ADMIN_IDS,
        "characters": list(characters.values()),
        "library": [{"name": n, "thumb": bool(v.get("thumb"))} for n, v in lib.items()],
        "projects": plist,
        "scenes": list(scenes_out.values()),
        "settings": await get_tts_settings(uid),
        "tts": {"piper": tts.piper_available(), "eleven": bool(tts.ELEVEN_KEY)},
        "profile": {"plan": client.get("plan", "Без подписки"), "until": client.get("until", ""),
                    "balance": client.get("balance", 0),
                    "films": sum(1 for v in projects.values() if v.get("status") == "done" and not v.get("preview"))},
    })


async def api_voices(request: web.Request) -> web.Response:
    auth(request)
    return web.json_response(await tts.voice_catalog(request.query.get("engine", "piper")))


async def api_settings(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    engine = d.get("engine")
    if engine not in ("piper", "eleven"):
        return err("Неизвестный движок озвучки")
    await save_tts_settings(uid, engine, tts.resolve_voice(engine, d.get("voice", "")))
    return web.json_response(await get_tts_settings(uid))


async def api_sample(request: web.Request) -> web.Response:
    """Пример голоса: синтезируется именно выбранной моделью."""
    auth(request)
    d = await request.json()
    try:
        wav = await tts.make_sample(d.get("voice", ""))
    except tts.TTSError as e:
        return err(str(e), 502)
    return web.Response(body=wav.read_bytes(), content_type="audio/wav")


async def api_character_add(request: web.Request) -> web.Response:
    u = auth(request)
    uid = str(u["id"])
    name, to_lib, tmp = "", False, None
    async for part in await request.multipart():
        if part.name == "name":
            name = clean_name(await part.text())
        elif part.name == "library":
            to_lib = (await part.text()) == "1" and u["id"] in ADMIN_IDS
        elif part.name == "file":
            ext = ".zip" if (part.filename or "").lower().endswith(".zip") else ".png"
            tmp = WORK / uid / "incoming" / f"{uuid.uuid4().hex}{ext}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk():
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        tmp.unlink(missing_ok=True)
                        return err("Файл слишком большой")
                    f.write(chunk)
    if not tmp or not tmp.exists():
        return err("Не выбран файл")
    if not name:
        tmp.unlink(missing_ok=True)
        return err("Введите имя")
    if tmp.suffix == ".zip":
        kind = await asyncio.to_thread(packs.detect, tmp)
        if kind == "scene":
            return await _add_scene(uid, name, tmp, to_lib)
        if kind != "cutout":
            tmp.unlink(missing_ok=True)
            return err("В архиве нет ни частей персонажа (head.png, torso.png…), ни скрипта сцены (.py)")
    elif tmp.stat().st_size > MAX_UPLOAD:
        tmp.unlink(missing_ok=True)
        return err("Картинка больше 25 МБ")
    if JOBS.get(f"{uid}/c/{name}", {}).get("status") == "processing":
        return err("Этот персонаж уже создаётся", 409)
    orig = WORK / uid / "characters" / f"{name}{tmp.suffix}"
    orig.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), orig)
    job_set(f"{uid}/c/{name}", kind="char", name=name, pct=3, text="Файл получен", status="processing", error="")
    spawn(create_character(uid, name, orig, to_lib))
    return web.json_response({"ok": True, "name": name, "kind": "character"})


async def _add_scene(uid: str, name: str, tmp: Path, to_lib: bool) -> web.Response:
    if JOBS.get(f"{uid}/s/{name}", {}).get("status") == "processing":
        return err("Эта сцена уже добавляется", 409)
    zp = WORK / uid / "scenes" / f"{cid(name)}.zip"
    zp.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), zp)
    job_set(f"{uid}/s/{name}", kind="scene", name=name, pct=3, text="Файл получен", status="processing", error="")
    spawn(create_scene(uid, name, zp, to_lib))
    return web.json_response({"ok": True, "name": name, "kind": "scene"})


async def api_scene_delete(request: web.Request) -> web.Response:
    u = auth(request)
    d = await request.json()
    name = clean_name(d.get("name", ""))
    if not name or not await delete_scene(str(u["id"]), name, bool(d.get("library"))):
        return err("Нельзя удалить", 403)
    return web.json_response({"ok": True})


async def api_project_cancel(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    if not await cancel_render(uid, str(d.get("id", ""))):
        return err("Рендер уже не идёт", 409)
    return web.json_response({"ok": True})


async def api_project_retry(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    pid = str(d.get("id", ""))
    try:
        await retry_render(uid, pid)
    except RuntimeError as e:
        return err(str(e), 409)
    request.app["notify"](uid, pid)
    return web.json_response({"ok": True})


async def api_character_delete(request: web.Request) -> web.Response:
    u = auth(request)
    d = await request.json()
    name = clean_name(d.get("name", ""))
    if not name or not await delete_character(str(u["id"]), name, bool(d.get("library"))):
        return err("Нельзя удалить", 403)
    return web.json_response({"ok": True})


async def api_character_voice(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    name = clean_name(d.get("name", ""))
    if not await fb_get(f"clients/{uid}/characters/{name}"):
        return err("Персонаж не найден", 404)
    await fb_update(f"clients/{uid}/characters/{name}", {"voice": d.get("voice", "")})
    return web.json_response({"ok": True})


async def api_character_thumb(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    name = clean_name(request.query.get("name", ""))
    rec = await fb_get(f"clients/{uid}/characters/{name}")
    if request.query.get("lib") == "1" or not rec:
        rec = await fb_get(f"library/characters/{name}")
    thumb = Path((rec or {}).get("thumb") or "/nonexistent")
    if not thumb.exists() or WORK.resolve() not in thumb.resolve().parents:
        return err("нет превью", 404)
    return web.FileResponse(thumb, headers={"Cache-Control": "private, max-age=300"})


async def api_render(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    scenes, notes = parse_script(str(d.get("script", ""))[:20000])
    if not scenes or not any(s["lines"] for s in scenes):
        return err("Не нашёл реплик. Формат: «Имя: текст».")
    engine = d.get("engine") if d.get("engine") in ("piper", "eleven") else (await get_tts_settings(uid))["engine"]
    voices = {k: v for k, v in (d.get("voices") or {}).items() if isinstance(v, str)}
    try:
        pid, _task, _title = await start_render(uid, scenes, str(d.get("style", "classic")),
                                                d.get("mode") == "preview", engine, voices, notes)
    except RuntimeError as e:
        return err(str(e))
    request.app["notify"](uid, pid)
    return web.json_response({"ok": True, "id": pid})


async def api_project_send(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    try:
        await send_project_video(request.app["bot"], uid, str(d.get("id", "")), int(uid))
    except RuntimeError as e:
        return err(str(e))
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(Path(__file__).with_name("index.html"), headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_web(bot: Bot) -> None:
    """Отдаёт Mini App (index.html) и API по домену Bothost, порт PORT (по умолчанию 3000)."""
    app = web.Application(middlewares=[api_middleware], client_max_size=MAX_ARCHIVE + 1024 * 1024)
    app["bot"] = bot

    def notify(uid: str, pid: str) -> None:
        """Когда Mini App-рендер закончится — пришлём в чат сообщение с кнопкой «Скачать»."""
        async def _w():
            while JOBS.get(f"{uid}/p/{pid}", {}).get("status") in ("queued", "rendering"):
                await asyncio.sleep(3)
            j = JOBS.get(f"{uid}/p/{pid}", {})
            try:
                if j.get("status") == "done":
                    pr = await fb_get(f"clients/{uid}/projects/{pid}") or {}
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="📥 Скачать видео", callback_data=f"send:{pid}")]])
                    await bot.send_message(int(uid), f"✅ «{pr.get('title', 'Мультфильм')}» готов! "
                                                     "Нажмите, чтобы получить видео в этот чат.", reply_markup=kb)
                elif j.get("status") == "error":
                    await bot.send_message(int(uid), f"❌ Рендер не удался: {j.get('error', 'ошибка')[:400]}\n"
                                                     "Откройте приложение → Проекты → «Продолжить».")
            except Exception:
                log.exception("notify failed")
        spawn(_w())
    app["notify"] = notify

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/voices", api_voices)
    app.router.add_get("/api/character/thumb", api_character_thumb)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/tts/sample", api_sample)
    app.router.add_post("/api/character", api_character_add)
    app.router.add_post("/api/character/delete", api_character_delete)
    app.router.add_post("/api/character/voice", api_character_voice)
    app.router.add_post("/api/render", api_render)
    app.router.add_post("/api/project/send", api_project_send)
    app.router.add_post("/api/project/cancel", api_project_cancel)
    app.router.add_post("/api/project/retry", api_project_retry)
    app.router.add_post("/api/scene/delete", api_scene_delete)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "3000"))).start()
    log.info("mini app served on port %s", os.environ.get("PORT", "3000"))


async def main():
    bot = Bot(BOT_TOKEN)
    log.info("TajStudio: Piper %s | ElevenLabs ключ %s | Mini App: %s",
             "установлен" if tts.piper_available() else "НЕ установлен (pip install piper-tts)",
             "задан" if tts.ELEVEN_KEY else "не задан", MINIAPP_URL or "MINIAPP_URL не задан")
    await run_web(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await set_menu_button(bot)          # кнопка по умолчанию для всех чатов
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
