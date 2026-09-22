"""FastAPI backend: serves the frontend, filesystem browsing, USD scene-graph /
properties queries, and a WebSocket-streamed RTX viewport backed by ovrtx/ovstage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import file_browser, native_dialog
from .render_session import RenderSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ovrtx_server")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
SAMPLE_FILES_DIR = os.path.join(os.path.dirname(APP_DIR), "sample files")

app = FastAPI(title="ovrtx USD Viewer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

session = RenderSession()

# Remembers the last folder opened (file's parent, or the folder itself) so the
# native Explorer dialog reopens where the user left off.
last_dir = SAMPLE_FILES_DIR if os.path.isdir(SAMPLE_FILES_DIR) else ""


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Native Windows "Open File" / "Select Folder" dialogs
# ---------------------------------------------------------------------------
@app.post("/api/dialog/file")
async def dialog_file():
    try:
        path = await asyncio.to_thread(native_dialog.pick_file, last_dir)
    except Exception as exc:  # noqa: BLE001 - surface dialog/subprocess failures to the UI
        log.exception("Native file dialog failed")
        return JSONResponse({"detail": f"Le sélecteur de fichier Windows a échoué : {exc}"}, status_code=500)
    return {"path": path}


@app.post("/api/dialog/folder")
async def dialog_folder():
    try:
        path = await asyncio.to_thread(native_dialog.pick_folder, last_dir)
    except Exception as exc:  # noqa: BLE001 - surface dialog/subprocess failures to the UI
        log.exception("Native folder dialog failed")
        return JSONResponse({"detail": f"Le sélecteur de dossier Windows a échoué : {exc}"}, status_code=500)
    return {"path": path}


# ---------------------------------------------------------------------------
# Opening a scene
# ---------------------------------------------------------------------------
class OpenRequest(BaseModel):
    path: str
    chosen: str | None = None
    width: int = 960
    height: int = 540


@app.post("/api/open")
async def open_scene(req: OpenRequest):
    target = req.chosen or req.path
    try:
        resolved, candidates = file_browser.resolve_open_target(target)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    if resolved is None:
        return {
            "need_choice": True,
            "candidates": [{"name": c.name, "path": c.path} for c in candidates],
        }

    try:
        await asyncio.to_thread(session.open_usd, resolved, req.width, req.height)
    except Exception as exc:  # noqa: BLE001 - surface renderer errors to the UI
        log.exception("Failed to open %s", resolved)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    global last_dir
    last_dir = os.path.dirname(resolved)

    return {
        "opened_path": resolved,
        "anim_unsafe": session.anim_unsafe,
    }


# ---------------------------------------------------------------------------
# Scene graph
# ---------------------------------------------------------------------------
@app.get("/api/tree/children")
async def tree_children(path: str = "/"):
    try:
        children = await asyncio.to_thread(session.get_children, path)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return children


@app.get("/api/prim")
async def prim_info(path: str):
    try:
        info = await asyncio.to_thread(session.get_prim_info, path)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return info


class TransformRequest(BaseModel):
    path: str
    translate: list[float]
    rotate: list[float]


@app.post("/api/prim/transform")
async def set_transform(req: TransformRequest):
    if len(req.translate) != 3 or len(req.rotate) != 3:
        return JSONResponse({"detail": "translate/rotate must each have 3 components"}, status_code=400)
    try:
        await asyncio.to_thread(session.set_prim_transform, req.path, req.translate, req.rotate)
    except Exception as exc:  # noqa: BLE001 - surface renderer errors to the UI
        log.exception("Failed to set transform on %s", req.path)
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Viewport WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/viewport")
async def ws_viewport(ws: WebSocket):
    await ws.accept()
    if not session.is_open:
        await ws.send_text(json.dumps({"type": "error", "message": "Aucune scène ouverte."}))
        await ws.close()
        return

    stop = asyncio.Event()

    async def receiver():
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await asyncio.to_thread(session.handle_control, msg)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    async def render_loop():
        import time

        last_fps_emit = time.monotonic()
        frames = 0
        last_anim_sent = None
        try:
            while not stop.is_set():
                jpeg_bytes, pick_result = await asyncio.to_thread(session.render_frame)
                if jpeg_bytes is None:
                    await asyncio.sleep(0.05)
                    continue
                await ws.send_bytes(jpeg_bytes)
                if pick_result is not None:
                    await ws.send_text(json.dumps({
                        "type": "pick_result",
                        "path": pick_result["path"],
                        "axis": pick_result["axis"],
                    }))
                anim_state = (round(session.anim_time, 3), session.anim_playing, session.anim_loop)
                if anim_state != last_anim_sent:
                    await ws.send_text(json.dumps({
                        "type": "anim",
                        "time": session.anim_time,
                        "start": session.anim_start,
                        "duration": session.anim_duration,
                        "playing": session.anim_playing,
                        "loop": session.anim_loop,
                        "unsafe": session.anim_unsafe,
                    }))
                    last_anim_sent = anim_state
                frames += 1
                now = time.monotonic()
                if now - last_fps_emit > 1.0:
                    await ws.send_text(json.dumps({
                        "type": "stats",
                        "fps": frames / (now - last_fps_emit),
                        "width": session.width,
                        "height": session.height,
                    }))
                    frames = 0
                    last_fps_emit = now
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    recv_task = asyncio.create_task(receiver())
    render_task = asyncio.create_task(render_loop())
    try:
        await stop.wait()
    finally:
        recv_task.cancel()
        render_task.cancel()
        for t in (recv_task, render_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
