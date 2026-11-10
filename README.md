# ShutterNet Backend (GoPro HERO5 Black Desktop Replacement)

A Python/FastAPI backend designed to communicate with the GoPro HERO5 Black over Wi-Fi.

## Architecture & How GoPro HERO5 Wi-Fi Works

1. **Connection**: Connect your desktop/laptop Wi-Fi directly to your GoPro's Wi-Fi network (default gateway IP `10.5.5.9`).
2. **Commands & Status**: GoPro exposes a lightweight HTTP control server at `http://10.5.5.9/gp/gpControl/...`.
3. **Media Browsing**: Media list is fetched via `/gp/gpMediaList` or HTTP directory browsing `/videos/DCIM/`.
4. **Live Streaming**: Triggered via control endpoint + UDP keep-alive packets sent to `10.5.5.9:8554`. The GoPro broadcasts an MPEG-TS stream readable locally at `udp://@0.0.0.0:8554` (e.g. via VLC, FFmpeg, or converted to WebRTC/HLS/WebSocket for frontend).

---

## Getting Started

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Server
```bash
uvicorn main:app --reload --port 8000
```

Interactive Swagger API docs will be accessible at:
[http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Endpoints

- **`GET /api/health`**: Tests if camera is reachable at `10.5.5.9`.
- **`GET /api/status`**: Fetches battery level, recording state, and full camera status.
- **`POST /api/control/shutter?action=start|stop|toggle`**: Triggers shutter (record video / capture photo).
- **`POST /api/control/mode?mode=video|photo|multishot`**: Changes camera capture mode.
- **`POST /api/control/locate`**: Triggers camera beep sound.
- **`GET /api/media`**: Lists all videos, photos, and thumbnails stored on the SD card.
- **`GET /api/media/thumbnail?directory=...&filename=...`**: Fetches the thumbnail preview for an item.
- **`POST /api/media/download`**: Downloads full-resolution files into the local `./downloads` folder.
- **`POST /api/stream/start`**: Starts UDP stream and background heartbeat loop.
- **`POST /api/stream/stop`**: Halts the keep-alive heartbeat loop.
