import os
import shutil
import subprocess
import threading
import time
import asyncio
import socket
import logging
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
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
    _stop_session()
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

@app.get("/")
async def serve_dashboard():
    """Serve visual Quik-replacement dashboard."""
    return FileResponse("index.html")

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

# --- In-app preview: UDP relay feeds MPEG-TS into ffmpeg, which outputs MJPEG ---
# Runs in plain threads with blocking sockets + subprocess.Popen because uvicorn on
# Windows uses the SelectorEventLoop, which has no subprocess support.

_stream: Dict[str, Any] = {
    "proc": None, "sock": None, "stop": threading.Event(),
    "threads": [], "subs": set(), "loop": None,
}
_GPHD = b"_GPHD_:0:0:2:0.000000\n"
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"

# HERO5 preview stream tuning: setting 64 = window size (7 = 1280x720),
# setting 62 = bitrate in bps. Raise bitrate only if Wi-Fi handles it cleanly.
STREAM_WINDOW = 7
STREAM_BITRATE = 4_000_000

_stream_stats: Dict[str, Any] = {"fps": 0.0, "width": 0, "height": 0, "kbps": 0.0, "active": False}


def _jpeg_dimensions(data: bytes):
    """Extract (width, height) from a JPEG's SOF marker."""
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg
    return None


def _offer_frame(queue: asyncio.Queue, frame: bytes) -> None:
    try:
        queue.put_nowait(frame)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(frame)
        except asyncio.QueueEmpty:
            pass


def _read_frames(proc: subprocess.Popen, loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
    """Splits ffmpeg's raw MJPEG stdout into JPEG frames and hands them to viewers."""
    buf = bytearray()
    window_frames = 0
    window_bytes = 0
    last_stats = time.monotonic()
    while not stop.is_set():
        chunk = proc.stdout.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            start = buf.find(_JPEG_SOI)
            if start == -1:
                buf.clear()
                break
            end = buf.find(_JPEG_EOI, start + 3)
            if end == -1:
                if start:
                    del buf[:start]
                break
            frame = bytes(buf[start:end + 2])
            del buf[:end + 2]
            window_frames += 1
            window_bytes += len(frame)
            if _stream_stats["width"] == 0:
                dims = _jpeg_dimensions(frame)
                if dims:
                    _stream_stats["width"], _stream_stats["height"] = dims
            for q in list(_stream["subs"]):
                loop.call_soon_threadsafe(_offer_frame, q, frame)
        now = time.monotonic()
        dt = now - last_stats
        if dt >= 1.0:
            _stream_stats["fps"] = round(window_frames / dt, 1)
            _stream_stats["kbps"] = round(window_bytes * 8 / dt / 1000)
            window_frames = 0
            window_bytes = 0
            last_stats = now


def _stream_worker(loop: asyncio.AbstractEventLoop) -> None:
    """
    Owns UDP port 8554: sends the _GPHD_ heartbeat from that socket (so the camera
    streams the MPEG-TS feed back to it) and pipes every received datagram into ffmpeg.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.error("ffmpeg is not installed or not on PATH; in-app preview disabled")
        return

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32768",
        "-f", "mpegts", "-i", "pipe:0",
        "-q:v", "3",
        "-fps_mode", "passthrough",
        "-f", "image2pipe", "-codec:v", "mjpeg", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as e:
        logger.error(f"Failed to launch ffmpeg: {e}")
        return
    _stream["proc"] = proc

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    except OSError:
        pass
    try:
        sock.bind(("", 8554))
    except OSError as e:
        logger.error(f"Could not bind UDP 8554 for preview: {e}")
        proc.terminate()
        return
    sock.settimeout(0.2)
    _stream["sock"] = sock

    # Camera-side stream quality: 720p window + higher bitrate (persists on camera).
    for path in (
        f"/gp/gpControl/setting/64/{STREAM_WINDOW}",
        f"/gp/gpControl/setting/62/{STREAM_BITRATE}",
    ):
        try:
            httpx.get(f"{gopro.control_base_url}{path}", timeout=5.0)
        except Exception as e:
            logger.warning(f"Stream setting {path} failed: {e}")

    # Point the camera's stream at *this* socket: trigger gpStream fresh so the
    # camera re-targets the preview to us (not to whatever heartbeat went before).
    try:
        httpx.get(
            f"{gopro.control_base_url}/gp/gpControl/execute?p1=gpStream&a1=proto_v2&c1=restart",
            timeout=5.0,
        )
    except Exception as e:
        logger.warning(f"gpStream trigger failed (continuing anyway): {e}")

    reader = threading.Thread(
        target=_read_frames, args=(proc, loop, _stream["stop"]), daemon=True
    )
    reader.start()
    _stream["threads"] = [reader]
    logger.info("in-app preview session started (UDP 8554 -> ffmpeg)")

    last_beat = 0.0
    last_stats = 0.0
    pkt_count = 0
    byte_count = 0
    try:
        while not _stream["stop"].is_set() and proc.poll() is None:
            now = time.monotonic()
            if now - last_beat >= 1.0:
                try:
                    sock.sendto(_GPHD, (gopro.ip, 8554))
                except OSError:
                    pass
                last_beat = now
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(b"_GPHD_"):
                continue
            pkt_count += 1
            byte_count += len(data)
            if now - last_stats >= 5.0:
                logger.info(f"UDP relay stats: {pkt_count} packets, {byte_count} bytes in last 5s")
                pkt_count = 0
                byte_count = 0
                last_stats = now
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("in-app preview session stopped")


def _stop_session() -> None:
    _stream["stop"].set()
    if _stream["sock"]:
        try:
            _stream["sock"].close()
        except OSError:
            pass
    if _stream["proc"] and _stream["proc"].poll() is None:
        try:
            _stream["proc"].terminate()
        except OSError:
            pass
    for t in _stream["threads"]:
        t.join(timeout=2)
    _stream["proc"] = None
    _stream["sock"] = None
    _stream["threads"] = []
    _stream["loop"] = None
    _stream_stats.update(fps=0.0, width=0, height=0, kbps=0.0, active=False)
    logger.info("preview session torn down")


@app.get("/api/stream/stats")
async def stream_stats():
    """Live preview telemetry for the HUD: fps, resolution, bitrate."""
    active = _stream["proc"] is not None and _stream["proc"].poll() is None
    return {**_stream_stats, "active": active}


@app.get("/api/stream/video")
async def preview_video():
    """
    Live preview as an MJPEG multipart stream (usable directly in an <img> tag).
    Requires ffmpeg on PATH and the camera preview started via /api/stream/start.
    """
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=500, detail="ffmpeg is required for in-app preview (not found on PATH)")

    if _stream["proc"] is None or _stream["proc"].poll() is not None:
        _stop_session()
        _stream["stop"] = threading.Event()
        _stream["loop"] = asyncio.get_running_loop()
        _stream_stats.update(fps=0.0, width=0, height=0, kbps=0.0, active=True)
        # The legacy ephemeral keepalive steals the camera's stream target -
        # take over the flow while a viewer is attached.
        global keep_alive_task
        if keep_alive_task and not keep_alive_task.done():
            keep_alive_task.cancel()
            keep_alive_task = None
        threading.Thread(
            target=_stream_worker, args=(_stream["loop"],), daemon=True
        ).start()

    async def gen():
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        _stream["subs"].add(queue)
        try:
            yield b"--frame\r\n"
            while True:
                frame = await queue.get()
                header = (
                    f"Content-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii")
                yield header + frame + b"\r\n--frame\r\n"
        except asyncio.CancelledError:
            pass
        finally:
            _stream["subs"].discard(queue)
            # No viewers left -> tear the session down so UDP/ffmpeg are released.
            await asyncio.sleep(0.5)
            if not _stream["subs"]:
                _stop_session()

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")
