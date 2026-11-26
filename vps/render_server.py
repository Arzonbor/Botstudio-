"""
Сервер рендера для VPS (instabox.sh). Бот на Bothost присылает сюда сцену (zip),
сервер запускает Blender и отдаёт готовый mp4.

API (везде заголовок  Authorization: Bearer <RENDER_TOKEN>):
  POST /render?preview=0|1&total=N   тело — zip (scene.json + файлы)  ->  {"id": "..."}
  GET  /status/<id>                  ->  {"status": queued|rendering|done|error, "pct": 0..1, "error": "..."}
  GET  /result/<id>                  ->  mp4
  GET  /health
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("render")

TOKEN = os.environ["RENDER_TOKEN"]
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
WORK = Path(os.environ.get("WORK_DIR", "/data/jobs"))
WORK.mkdir(parents=True, exist_ok=True)
WORKER = Path(__file__).resolve().parent.parent / "blender_worker.py"
if not WORKER.exists():
    WORKER = Path(__file__).resolve().parent / "blender_worker.py"
MAX_PARALLEL = int(os.environ.get("MAX_RENDERS", "1"))
KEEP_SECONDS = 6 * 3600

SEM = asyncio.Semaphore(MAX_PARALLEL)
JOBS: dict[str, dict] = {}
FRA_RE = re.compile(r"Fra:(\d+)")


@web.middleware
async def auth(request: web.Request, handler):
    if request.path != "/health" and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def cleanup() -> None:
    now = time.time()
    for d in WORK.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > KEEP_SECONDS:
            shutil.rmtree(d, ignore_errors=True)
            JOBS.pop(d.name, None)


async def run_job(jid: str, preview: bool, total: int) -> None:
    job, jdir = JOBS[jid], WORK / jid
    async with SEM:
        job["status"] = "rendering"
        try:
            scene = json.loads((jdir / "scene.json").read_text())
            for sc in scene["scenes"]:                      # относительные пути -> абсолютные
                for ln in sc["lines"]:
                    for k in ("audio", "image"):
                        if ln.get(k):
                            ln[k] = str(jdir / ln[k])
            (jdir / "scene_abs.json").write_text(json.dumps(scene, ensure_ascii=False))
            out = jdir / "result.mp4"
            cmd = [BLENDER_BIN, "-b", "-P", str(WORKER), "--",
                   "--scene", str(jdir / "scene_abs.json"), "--out", str(out)]
            if preview:
                cmd.append("--preview")
            if shutil.which("stdbuf"):
                cmd = ["stdbuf", "-oL", "-eL", *cmd]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            tail: deque = deque(maxlen=25)
            async for raw in proc.stdout:
                line = raw.decode(errors="ignore").rstrip()
                tail.append(line)
                m = FRA_RE.search(line)
                if m:
                    job["pct"] = min(1.0, int(m.group(1)) / max(1, total))
            await proc.wait()
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError("Blender error:\n" + "\n".join(tail)[-1500:])
            job.update(status="done", pct=1.0)
        except Exception as e:
            log.exception("job %s failed", jid)
            job.update(status="error", error=str(e)[:1500])


async def render(request: web.Request) -> web.Response:
    cleanup()
    jid = uuid.uuid4().hex
    jdir = WORK / jid
    jdir.mkdir(parents=True)
    zpath = jdir / "in.zip"
    with open(zpath, "wb") as f:
        async for chunk in request.content.iter_chunked(1 << 20):
            f.write(chunk)
    try:
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():                       # защита от выхода из папки
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError("bad path in zip")
            z.extractall(jdir)
        if not (jdir / "scene.json").exists():
            raise ValueError("нет scene.json")
    except Exception as e:
        shutil.rmtree(jdir, ignore_errors=True)
        return web.json_response({"error": f"плохой архив: {e}"}, status=400)
    preview = request.query.get("preview") == "1"
    total = int(request.query.get("total", "1"))
    JOBS[jid] = {"status": "queued", "pct": 0.0, "error": ""}
    asyncio.create_task(run_job(jid, preview, total))
    return web.json_response({"id": jid})


async def status(request: web.Request) -> web.Response:
    j = JOBS.get(request.match_info["id"])
    return web.json_response(j or {"status": "error", "error": "задача не найдена (сервер перезапускался?)"})


async def result(request: web.Request) -> web.StreamResponse:
    jid = request.match_info["id"]
    f = WORK / jid / "result.mp4"
    if not re.fullmatch(r"[0-9a-f]{32}", jid) or not f.exists():
        return web.json_response({"error": "нет файла"}, status=404)
    return web.FileResponse(f)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    app = web.Application(middlewares=[auth], client_max_size=500 * 1024 * 1024)
    app.router.add_post("/render", render)
    app.router.add_get("/status/{id}", status)
    app.router.add_get("/result/{id}", result)
    app.router.add_get("/health", health)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
