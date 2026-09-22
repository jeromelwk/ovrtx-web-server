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
from .ha_bridge import ConnectionSettings, HomeAssistantBridge
from .render_session import RenderSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ovrtx_server")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
SAMPLE_FILES_DIR = os.path.join(os.path.dirname(APP_DIR), "sample files")

app = FastAPI(title="ovrtx USD Viewer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

session = RenderSession()
ha_bridge = HomeAssistantBridge(session)

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

    # Mirrors omni.home.assistant's behavior on a stage-open event: mappings
    # saved earlier for this same scene path are restored automatically.
    await asyncio.to_thread(ha_bridge.load_mappings_for_scene, resolved)

    return {
        "opened_path": resolved,
        "anim_unsafe": session.anim_unsafe,
        "ha_mapping_count": len(ha_bridge.mappings),
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
# Home Assistant MQTT bridge (see app/ha_bridge.py)
# ---------------------------------------------------------------------------
class HAConnectionRequest(BaseModel):
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls_enabled: bool = False
    topic_filter: str = "#"
    qos: int = 0


class HAMappingRequest(BaseModel):
    prim_path: str
    topic_prefix: str
    kind: str = "light"
    max_intensity: float = 31000.0
    max_exposure: float = 0.0


class HAMappingRemoveRequest(BaseModel):
    topic_prefix: str


def _ha_conn_from_request(req: HAConnectionRequest) -> ConnectionSettings:
    return ConnectionSettings(
        host=req.host.strip(),
        port=req.port,
        username=req.username.strip(),
        password=req.password,
        tls_enabled=req.tls_enabled,
        topic_filter=req.topic_filter.strip() or "#",
        qos=req.qos,
    )


@app.get("/api/ha/connection")
async def ha_get_connection():
    c = ha_bridge.conn
    return {
        "host": c.host, "port": c.port, "username": c.username, "password": c.password,
        "tls_enabled": c.tls_enabled, "topic_filter": c.topic_filter, "qos": c.qos,
    }


@app.post("/api/ha/connection")
async def ha_save_connection(req: HAConnectionRequest):
    ha_bridge.save_connection(_ha_conn_from_request(req))
    return {"ok": True}


@app.post("/api/ha/connect")
async def ha_connect(req: HAConnectionRequest):
    ha_bridge.save_connection(_ha_conn_from_request(req))
    try:
        await asyncio.to_thread(ha_bridge.connect)
    except Exception as exc:  # noqa: BLE001 - surface connection failures to the UI
        log.exception("Failed to connect to MQTT broker")
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return {"ok": True}


@app.post("/api/ha/disconnect")
async def ha_disconnect():
    await asyncio.to_thread(ha_bridge.disconnect)
    return {"ok": True}


@app.get("/api/ha/status")
async def ha_status():
    return ha_bridge.status()


@app.get("/api/ha/mappings")
async def ha_list_mappings():
    return ha_bridge.to_list()


@app.post("/api/ha/mappings")
async def ha_add_mapping(req: HAMappingRequest):
    if not req.prim_path or not req.topic_prefix:
        return JSONResponse({"detail": "prim_path et topic_prefix sont requis"}, status_code=400)
    ha_bridge.add_mapping(req.prim_path, req.topic_prefix, req.kind, req.max_intensity, req.max_exposure)
    return {"ok": True}


@app.post("/api/ha/mappings/remove")
async def ha_remove_mapping(req: HAMappingRemoveRequest):
    ha_bridge.remove_mapping(req.topic_prefix)
    return {"ok": True}


@app.post("/api/ha/mappings/detect")
async def ha_detect_mappings():
    if not session.is_open:
        return JSONResponse({"detail": "Aucune scène ouverte"}, status_code=400)
    count = await asyncio.to_thread(ha_bridge.detect_from_scene)
    return {"count": count, "mappings": ha_bridge.to_list()}


@app.post("/api/ha/mappings/save")
async def ha_save_mappings():
    if not session.source_path:
        return JSONResponse({"detail": "Aucune scène ouverte"}, status_code=400)
    ha_bridge.save_mappings_for_scene(session.source_path)
    return {"ok": True}


@app.post("/api/ha/mappings/load")
async def ha_load_mappings():
    if not session.source_path:
        return JSONResponse({"detail": "Aucune scène ouverte"}, status_code=400)
    ha_bridge.load_mappings_for_scene(session.source_path)
    return {"mappings": ha_bridge.to_list()}


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
