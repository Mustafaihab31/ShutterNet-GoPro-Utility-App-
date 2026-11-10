import os
import re
import socket
import logging
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

logger = logging.getLogger("gopro_backend")

class MediaItem(BaseModel):
    name: str
    url: str
    size_bytes: Optional[int] = None
    directory: str
    is_video: bool = False
    is_photo: bool = False
    is_thumbnail: bool = False

class CameraStatus(BaseModel):
    connected: bool
    ip: str
    model: Optional[str] = "HERO5 Black"
    battery_level: Optional[int] = None
    recording: Optional[bool] = None
    raw_status: Optional[Dict[str, Any]] = None

class GoProHero5:
    """
    API client for GoPro HERO5 Black over Wi-Fi.
    Default GoPro AP IP is 10.5.5.9.
    """
    def __init__(self, ip: str = "10.5.5.9", timeout: float = 5.0):
        self.ip = ip
        self.base_url = f"http://{self.ip}"
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def is_reachable(self) -> bool:
        """Quick check if GoPro responds at 10.5.5.9"""
        try:
            res = await self.client.get("/gp/gpControl/status", timeout=2.0)
            return res.status_code == 200
        except Exception:
            return False

    async def get_status(self) -> CameraStatus:
        """Fetch camera status and parse key fields."""
        try:
            res = await self.client.get("/gp/gpControl/status")
            if res.status_code == 200:
                data = res.json()
                # Status field codes for Hero 5:
                # 10: recording status (0 = false, 1 = true)
                # 2: battery level (or percentage)
                status_dict = data.get("status", {})
                is_recording = bool(status_dict.get("10", 0) == 1)
                battery = status_dict.get("2", None)
                return CameraStatus(
                    connected=True,
                    ip=self.ip,
                    recording=is_recording,
                    battery_level=battery,
                    raw_status=data
                )
        except Exception as e:
            logger.warning(f"Failed to fetch status: {e}")
        
        return CameraStatus(connected=False, ip=self.ip)

    # --- Controls ---

    async def start_shutter(self) -> bool:
        """Start recording or take a photo."""
        res = await self.client.get("/gp/gpControl/command/shutter?p=1")
        return res.status_code == 200

    async def stop_shutter(self) -> bool:
        """Stop recording."""
        res = await self.client.get("/gp/gpControl/command/shutter?p=0")
        return res.status_code == 200

    async def set_mode(self, mode: str) -> bool:
        """
        Set primary camera mode:
        video (0), photo (1), multishot/burst/timelapse (2)
        """
        mode_map = {
            "video": 0,
            "photo": 1,
            "multishot": 2,
            "timelapse": 2
        }
        val = mode_map.get(mode.lower())
        if val is None:
            raise ValueError(f"Unknown mode: {mode}. Expected: {list(mode_map.keys())}")
        res = await self.client.get(f"/gp/gpControl/command/sub_mode?mode={val}")
        return res.status_code == 200

    async def locate(self, enable: bool = True) -> bool:
        """Beep / locate camera."""
        p = "1" if enable else "0"
        res = await self.client.get(f"/gp/gpControl/command/system/locate?p={p}")
        return res.status_code == 200

    async def delete_file(self, folder: str, filename: str) -> bool:
        """Delete specific file on SD card."""
        res = await self.client.get(f"/gp/gpControl/command/storage/delete?p={folder}/{filename}")
        return res.status_code == 200

    # --- Live Preview Streaming ---

    def start_preview_stream(self) -> bool:
        """
        HERO5 starts its live UDP stream (TS over UDP port 8554)
        when a magic packet / start command is sent.
        Also starts a keep-alive listener.
        """
        try:
            # Command to wake preview stream
            # 1. Trigger preview start command
            import asyncio
            # We also send UDP packet "_GPHD_:0:0:2:0.000000\n" to 10.5.5.9:8554
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"_GPHD_:0:0:2:0.000000\n", (self.ip, 8554))
            return True
        except Exception as e:
            logger.error(f"Failed to start stream: {e}")
            return False

    async def trigger_preview_api(self) -> bool:
        try:
            res = await self.client.get("/gp/gpControl/execute?p1=gpStream&a1=proto_v2&c1=restart")
            return res.status_code == 200
        except Exception:
            return False

    # --- Media / SD Card Access ---

    async def list_media(self) -> List[MediaItem]:
        """
        HERO5 serves media list via JSON: /gp/gpMediaList
        Or falls back to directory scraping /videos/DCIM/
        """
        items: List[MediaItem] = []
        try:
            res = await self.client.get("/gp/gpMediaList")
            if res.status_code == 200:
                data = res.json()
                media_dirs = data.get("media", [])
                for dir_entry in media_dirs:
                    directory_name = dir_entry.get("d")
                    file_list = dir_entry.get("fs", [])
                    for f in file_list:
                        fname = f.get("n")
                        size = int(f.get("s", 0))
                        file_url = f"{self.base_url}/videos/DCIM/{directory_name}/{fname}"
                        lower = fname.lower()
                        is_vid = lower.endswith(".mp4") or lower.endswith(".lrv")
                        is_photo = lower.endswith(".jpg") or lower.endswith(".gpr")
                        items.append(MediaItem(
                            name=fname,
                            url=file_url,
                            size_bytes=size,
                            directory=directory_name,
                            is_video=is_vid,
                            is_photo=is_photo,
                            is_thumbnail=lower.endswith(".thm")
                        ))
                return items
        except Exception as e:
            logger.warning(f"gpMediaList endpoint failed ({e}), trying HTML scraper...")

        # Fallback HTML scraping
        try:
            res = await self.client.get("/videos/DCIM/")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                links = [a["href"] for a in soup.find_all("a", href=True)]
                for link in links:
                    if link.endswith("/") and not link.startswith(".."):
                        subdir = link.strip("/")
                        sub_res = await self.client.get(f"/videos/DCIM/{subdir}/")
                        if sub_res.status_code == 200:
                            sub_soup = BeautifulSoup(sub_res.text, "html.parser")
                            for file_link in sub_soup.find_all("a", href=True):
                                fname = file_link["href"].strip("/")
                                if fname.startswith("."):
                                    continue
                                file_url = f"{self.base_url}/videos/DCIM/{subdir}/{fname}"
                                lower = fname.lower()
                                items.append(MediaItem(
                                    name=fname,
                                    url=file_url,
                                    directory=subdir,
                                    is_video=lower.endswith(".mp4") or lower.endswith(".lrv"),
                                    is_photo=lower.endswith(".jpg") or lower.endswith(".gpr"),
                                    is_thumbnail=lower.endswith(".thm")
                                ))
        except Exception as e:
            logger.error(f"HTML scraping fallback failed: {e}")

        return items

    def get_thumbnail_url(self, directory: str, filename: str) -> str:
        """
        GoPro HERO 5 quick thumbnail generation endpoint.
        """
        return f"{self.base_url}/gp/gpControl/thumbnail?p={directory}/{filename}"
