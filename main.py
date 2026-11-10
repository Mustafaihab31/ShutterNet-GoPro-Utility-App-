import os
import asyncio
import socket
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx

from gopro import GoProHero5, MediaItem, CameraStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shutter_net")

gopro = GoProHero5()
DOWNLOAD_DIR = os.path.abspath("./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

keep_alive_task: Optional[asyncio.Task] = None

async def stream_keepalive_loop():
    """GoPro requires periodic UDP heartbeat to maintain preview stream."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    logger.info("Starting UDP keepalive heartbeat on port 8554...")
    try:
        while True:
            sock.sendto(b"_GPHD_:0:0:2:0.000000\n", (gopro.ip, 8554))
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        logger.info("UDP keepalive loop stopped.")
    finally:
        sock.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if keep_alive_task and not keep_alive_task.done():
        keep_alive_task.cancel()
    await gopro.close()

app = FastAPI(
    title="ShutterNet - GoPro HERO 5 Desktop Backend",
    description="Local backend REST API to interact with GoPro HERO 5 Black over Wi-Fi.",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health_check():
    connected = await gopro.is_reachable()
    return {"status": "ok", "camera_reachable": connected, "camera_ip": gopro.ip}

@app.get("/api/status", response_model=CameraStatus)
async def get_status():
    """Get GoPro current state (battery, recording status, mode, etc.)."""
    return await gopro.get_status()

@app.post("/api/control/shutter")
async def trigger_shutter(action: str = Query("start", enum=["start", "stop", "toggle"])):
    """Start or stop recording / trigger photo capture."""
    if action == "start":
        ok = await gopro.start_shutter()
    elif action == "stop":
        ok = await gopro.stop_shutter()
    else:
        status = await gopro.get_status()
        if status.recording:
            ok = await gopro.stop_shutter()
        else:
            ok = await gopro.start_shutter()
            
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to execute shutter action: {action}")
    return {"success": True, "action": action}

@app.post("/api/control/mode")
async def set_camera_mode(mode: str = Query("video", enum=["video", "photo", "multishot", "timelapse"])):
    """Switch primary camera mode."""
    try:
        ok = await gopro.set_mode(mode)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to change mode")
        return {"success": True, "mode": mode}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/control/locate")
async def locate_camera(enable: bool = True):
    """Beep GoPro camera."""
    ok = await gopro.locate(enable)
    return {"success": ok}

@app.get("/api/media/folders", response_model=List[str])
async def list_media_folders():
    """List DCIM directories on SD card (e.g. 154GOPRO, 153GOPRO)."""
    return await gopro.list_media_folders()

@app.get("/api/media", response_model=List[MediaItem])
async def list_media(folder: Optional[str] = None):
    """
    List photos and videos on the SD card.
    Optionally filter by folder (e.g. folder=154GOPRO). If omitted, loads recent folder or all folders.
    """
    if not folder:
        folders = await gopro.list_media_folders()
        if folders:
            folder = folders[0] # Default to most recent directory for super fast loading
    items = await gopro.list_media_in_folder(folder) if folder else []
    return items

@app.get("/api/media/thumbnail")
async def get_thumbnail(url: str):
    """Proxy image thumbnail directly from GoPro."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                return StreamingResponse(resp.aiter_bytes(), media_type="image/jpeg")
            else:
                raise HTTPException(status_code=resp.status_code, detail="Thumbnail not available")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch thumbnail: {e}")

@app.post("/api/media/download")
async def download_media(url: str, filename: str, background_tasks: BackgroundTasks):
    """Download a file from GoPro SD card into local storage in the background."""
    local_path = os.path.join(DOWNLOAD_DIR, filename)
    
    async def _download_task(remote_url: str, dest_path: str):
        logger.info(f"Downloading {remote_url} -> {dest_path}")
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", remote_url) as response:
                with open(dest_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
        logger.info(f"Finished downloading {dest_path}")

    background_tasks.add_task(_download_task, url, local_path)
    return {"status": "download_started", "filename": filename, "destination": local_path}

@app.get("/api/downloads")
async def list_downloaded_files():
    """List files currently downloaded to the local desktop cache."""
    files = []
    for f in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(fpath):
            files.append({
                "filename": f,
                "size_bytes": os.path.getsize(fpath),
                "path": fpath
            })
    return files

@app.post("/api/stream/start")
async def start_live_stream():
    """
    Initializes UDP preview stream on HERO5.
    Stream is accessible at udp://@0.0.0.0:8554.
    """
    global keep_alive_task
    await gopro.trigger_preview_api()
    gopro.start_preview_udp()

    if keep_alive_task is None or keep_alive_task.done():
        keep_alive_task = asyncio.create_task(stream_keepalive_loop())

    return {
        "status": "stream_started",
        "protocol": "udp",
        "stream_url": "udp://@0.0.0.0:8554"
    }

@app.post("/api/stream/stop")
async def stop_live_stream():
    """Stops the UDP preview stream heartbeat."""
    global keep_alive_task
    if keep_alive_task and not keep_alive_task.done():
        keep_alive_task.cancel()
        keep_alive_task = None
    return {"status": "stream_stopped"}
