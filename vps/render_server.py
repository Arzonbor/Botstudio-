"""
Сервер рендера для VPS (instabox.sh). Бот на Bothost присылает сцену (zip),
сервер запускает Blender, рендерит кадры в папку и отдаёт готовый mp4.

API (везде заголовок  Authorization: Bearer <RENDER_TOKEN>):
  POST /render?preview=0|1&total=N   тело — zip (scene.json + файлы)  ->  {"id": "..."}
  GET  /status/<id>   -> {status: queued|building|rendering|encoding|done|error|cancelled, stage, pct 0..1,
                          done, frames, eta, error, tail[], resumable}
  POST /cancel/<id>   останавливает Blender; готовые кадры остаются
  POST /resume/<id>   продолжает с готовых кадров (404 — задача уже удалена, надо слать заново)
  GET  /result/<id>   mp4
  GET  /health
"""
import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
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
KEEP_SECONDS = 24 * 3600            # недорисованное хранится сутки, чтобы можно было продолжить
MIN_FREE = 3 * 1024 ** 3

SEM = asyncio.Semaphore(MAX_PARALLEL)
JOBS: dict[str, dict] = {}
PROCS: dict[str, asyncio.subprocess.Process] = {}
FRA_RE = re.compile(r"Fra:(\d+)")
TOT_RE = re.compile(r"TOTAL_FRAMES=(\d+)")
STAGE_RE = re.compile(r"STAGE=(\w+)")
WARN_RE = re.compile(r"WARN=(.+)")
ID_RE = re.compile(r"[0-9a-f]{32}")


@web.middleware
async def auth(request: web.Request, handler):
    if request.path != "/health" and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def cleanup() -> None:
    now = time.time()
    dirs = sorted((d for d in WORK.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs:
        busy = JOBS.get(d.name, {}).get("status") in ("queued", "building", "rendering", "encoding")
        old = now - d.stat().st_mtime > KEEP_SECONDS
        low = shutil.disk_usage(WORK).free < MIN_FREE
        if not busy and (old or low):
            shutil.rmtree(d, ignore_errors=True)
            JOBS.pop(d.name, None)


def _meta_path(jid: str) -> Path:
    return WORK / jid / "job.json"


def _load_job(jid: str) -> dict | None:
    """Задача из памяти или (после перезапуска сервера) с диска — как «прервана», её можно продолжить."""
    if jid in JOBS:
        return JOBS[jid]
    if ID_RE.fullmatch(jid) and _meta_path(jid).exists():
        try:
            m = json.loads(_meta_path(jid).read_text())
        except Exception:
            return None
        if (WORK / jid / "result.mp4").exists():
            JOBS[jid] = {"status": "done", "stage": "done", "pct": 1.0, "tail": [], "total": m.get("total", 1),
                         "preview": m.get("preview", False), "run_t0": 0, "run_done0": 0}
            return JOBS[jid]
        JOBS[jid] = {"status": "error", "stage": "interrupted", "error": "Сервер рендера был перезапущен",
                     "tail": [], "total": m.get("total", 1), "preview": m.get("preview", False), "run_t0": 0, "run_done0": 0}
        return JOBS[jid]
    return None


def _frames_done(jid: str) -> int:
    try:
        return sum(1 for _ in (WORK / jid / "frames").glob("f_*.png"))
    except Exception:
        return 0


def snapshot(jid: str, job: dict) -> dict:
    step = 3 if job.get("preview") else 2
    frames = max(1, math.ceil(max(1, job.get("total", 1)) / step))
    done = frames if job["status"] == "done" else min(frames, _frames_done(jid))
    pct = job.get("pct", 0.0)
    if job["status"] in ("rendering", "encoding", "error", "cancelled") or done:
        pct = max(pct if job["status"] == "done" else 0.0, done / frames)
    eta = None
    if job["status"] == "rendering" and job.get("run_t0"):
        dt, dd = time.time() - job["run_t0"], done - job.get("run_done0", 0)
        if dt > 8 and dd > 0:
            eta = int((frames - done) / (dd / dt))
    return {"status": job["status"], "stage": job.get("stage", job["status"]), "pct": min(1.0, pct), "done": done,
            "frames": frames, "eta": eta, "error": job.get("error", ""), "tail": list(job.get("tail", []))[-6:],
            "warn": job.get("warn", ""),
            "resumable": job["status"] in ("error", "cancelled") and (WORK / jid / "scene.json").exists()}


def _abs_paths(scene: dict, jdir: Path) -> None:
    """Относительные пути из архива -> абсолютные."""
    for sc in scene["scenes"]:
        cust = sc.get("custom") or {}
        if cust.get("script"):
            cust["script"] = str(jdir / cust["script"])
        for ln in sc["lines"]:
            for k in ("audio", "image"):
                if ln.get(k):
                    ln[k] = str(jdir / ln[k])
            if ln.get("cutout"):
                ln["cutout"] = {k: str(jdir / v) for k, v in ln["cutout"].items()}
            meta = ln.get("meta") or {}
            if meta.get("faces"):
                meta["faces"] = [str(jdir / p) for p in meta["faces"]]


async def run_job(jid: str) -> None:
    job, jdir = JOBS[jid], WORK / jid
    job.update(status="queued", stage="queued", error="", tail=[], cancel=False)
    async with SEM:
        if job.get("cancel"):
            job.update(status="cancelled", stage="cancelled")
            return
        job.update(status="building", stage="building")
        try:
            scene = json.loads((jdir / "scene.json").read_text())
            _abs_paths(scene, jdir)
            (jdir / "scene_abs.json").write_text(json.dumps(scene, ensure_ascii=False))
            out = jdir / "result.mp4"
            out.unlink(missing_ok=True)
            cmd = [BLENDER_BIN, "-b", "-P", str(WORKER), "--",
                   "--scene", str(jdir / "scene_abs.json"), "--out", str(out), "--frames", str(jdir / "frames")]
            if job.get("preview"):
                cmd.append("--preview")
            if shutil.which("stdbuf"):
                cmd = ["stdbuf", "-oL", "-eL", *cmd]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            PROCS[jid] = proc
            tail: deque = deque(maxlen=25)
            async for raw in proc.stdout:
                line = raw.decode(errors="ignore").rstrip()
                if not line:
                    continue
                if not line.startswith("Fra:"):
                    tail.append(line[:200])
                    job["tail"] = list(tail)
                for rx, handler in ((TOT_RE, lambda m: job.update(total=int(m.group(1)))),
                                    (STAGE_RE, lambda m: _stage(job, jid, m.group(1))),
                                    (WARN_RE, lambda m: job.update(warn=m.group(1)[:300]))):
                    m = rx.search(line)
                    if m:
                        handler(m)
            await proc.wait()
            PROCS.pop(jid, None)
            if job.get("cancel"):
                job.update(status="cancelled", stage="cancelled")
                return
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError("Blender error:\n" + "\n".join(tail)[-1500:])
            shutil.rmtree(jdir / "frames", ignore_errors=True)       # кадры больше не нужны — освобождаем диск
            job.update(status="done", stage="done", pct=1.0)
        except Exception as e:
            PROCS.pop(jid, None)
            log.exception("job %s failed", jid)
            job.update(status="error", stage="error", error=str(e)[:1500])


def _stage(job: dict, jid: str, name: str) -> None:
    if name in ("building", "rendering", "encoding"):
        job.update(status=name, stage=name)
        if name == "rendering":
            job.update(run_t0=time.time(), run_done0=_frames_done(jid))


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
    zpath.unlink(missing_ok=True)
    preview = request.query.get("preview") == "1"
    total = int(request.query.get("total", "1"))
    _meta_path(jid).write_text(json.dumps({"preview": preview, "total": total}))
    JOBS[jid] = {"status": "queued", "stage": "queued", "pct": 0.0, "error": "", "tail": [], "total": total,
                 "preview": preview, "run_t0": 0, "run_done0": 0}
    asyncio.create_task(run_job(jid))
    return web.json_response({"id": jid})


async def status(request: web.Request) -> web.Response:
    jid = request.match_info["id"]
    j = _load_job(jid)
    if not j:
        return web.json_response({"status": "error", "stage": "gone", "pct": 0, "done": 0, "frames": 1, "tail": [],
                                  "resumable": False, "error": "задача не найдена (удалена или сервер перезапускался)"})
    return web.json_response(snapshot(jid, j))


async def cancel(request: web.Request) -> web.Response:
    jid = request.match_info["id"]
    j = _load_job(jid)
    if not j:
        return web.json_response({"error": "нет такой задачи"}, status=404)
    j["cancel"] = True
    proc = PROCS.get(jid)
    if proc and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        asyncio.get_event_loop().call_later(6, lambda: _kill9(proc))
    elif j["status"] == "queued":
        j.update(status="cancelled", stage="cancelled")
    return web.json_response({"ok": True})


def _kill9(proc) -> None:
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def resume(request: web.Request) -> web.Response:
    jid = request.match_info["id"]
    j = _load_job(jid)
    if not j or not (WORK / jid / "scene.json").exists():
        return web.json_response({"error": "задача удалена"}, status=404)
    if j["status"] in ("queued", "building", "rendering", "encoding"):
        return web.json_response({"id": jid, "already": True})
    if j["status"] == "done":
        return web.json_response({"id": jid, "done": True})
    asyncio.create_task(run_job(jid))
    return web.json_response({"id": jid})


async def result(request: web.Request) -> web.StreamResponse:
    jid = request.match_info["id"]
    f = WORK / jid / "result.mp4"
    if not ID_RE.fullmatch(jid) or not f.exists():
        return web.json_response({"error": "нет файла"}, status=404)
    return web.FileResponse(f)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    app = web.Application(middlewares=[auth], client_max_size=500 * 1024 * 1024)
    app.router.add_post("/render", render)
    app.router.add_get("/status/{id}", status)
    app.router.add_post("/cancel/{id}", cancel)
    app.router.add_post("/resume/{id}", resume)
    app.router.add_get("/result/{id}", result)
    app.router.add_get("/health", health)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
