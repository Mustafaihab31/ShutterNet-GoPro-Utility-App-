# ShutterNet (GoPro HERO5 Black Desktop Replacement)

A GoPro HERO5 Black control center: Python/FastAPI backend + Tauri desktop app with a Studio Sidebar dashboard.

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

## Desktop App (Tauri)

The dashboard (`index.html`, warm-graphite "Studio Sidebar" layout) is wrapped in a Tauri v2 window. In dev mode Tauri boots the FastAPI backend for you and points the webview at it. **Note: crate downloads need a normal internet connection — the GoPro Wi-Fi has none.**

```bash
npm install          # one-time: Tauri CLI
npm run dev          # starts uvicorn + opens the ShutterNet window
npm run build        # bundles a Windows installer into src-tauri/target/release/bundle/
```

If you prefer running pieces manually while iterating on the UI:

```bash
python -m uvicorn main:app --port 8000   # backend + dashboard at http://localhost:8000
```

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
