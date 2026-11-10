import os
import re
import socket
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

logger = logging.getLogger("gopro_backend")

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*$", re.I)
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_DATE_RE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
]


def parse_size_bytes(size_str: Optional[str]) -> Optional[int]:
    """'123.4 MB' -> bytes (int), or None if unparseable."""
    m = _SIZE_RE.match(size_str or "")
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])
    except ValueError:
        return None


def parse_date_ts(date_str: Optional[str]) -> Optional[float]:
    """Best-effort parse of the listing date column into an epoch timestamp."""
    if not date_str:
        return None
    text = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


class MediaItem(BaseModel):
    name: str
    url: str
    size_str: Optional[str] = None
    size_bytes: Optional[int] = None
    date_str: Optional[str] = None
    date_ts: Optional[float] = None
    directory: str
    thumbnail_url: Optional[str] = None
    lrv_url: Optional[str] = None
    is_video: bool = False
    is_photo: bool = False

class CameraStatus(BaseModel):
    connected: bool
    ip: str
    model: Optional[str] = "HERO5 Black"
    battery_level: Optional[int] = None
    recording: Optional[bool] = None
    mode: Optional[str] = None
    raw_status: Optional[Dict[str, Any]] = None

class GoProHero5:
    """
    API client for GoPro HERO5 Black over Wi-Fi.
    - Control server: http://10.5.5.9/gp/gpControl/...
    - Media file web server: http://10.5.5.9:8080/videos/DCIM/...
    """
    def __init__(self, ip: str = "10.5.5.9", control_port: int = 80, media_port: int = 8080, timeout: float = 5.0):
        self.ip = ip
        self.control_base_url = f"http://{self.ip}:{control_port}"
        self.media_base_url = f"http://{self.ip}:{media_port}"
        self.control_client = httpx.AsyncClient(base_url=self.control_base_url, timeout=timeout)
        self.media_client = httpx.AsyncClient(base_url=self.media_base_url, timeout=timeout)

    async def close(self):
        await self.control_client.aclose()
        await self.media_client.aclose()

    async def is_reachable(self) -> bool:
        """Quick check if GoPro responds to status query."""
        try:
            res = await self.control_client.get("/gp/gpControl/status", timeout=2.0)
            return res.status_code == 200
        except Exception:
            return False

    async def get_status(self) -> CameraStatus:
        """Fetch camera status and parse battery & recording fields."""
        try:
            res = await self.control_client.get("/gp/gpControl/status", timeout=3.0)
            if res.status_code == 200:
                data = res.json()
                status_dict = data.get("status", {})
                is_recording = bool(status_dict.get("10", 0) == 1)
                # key "70" is battery percentage; key "1" is a 0-4 gauge (4 = full)
                battery = status_dict.get("70")
                if battery is None:
                    lvl = status_dict.get("1")
                    if isinstance(lvl, (int, float)):
                        battery = int(lvl / 4 * 100)
                if isinstance(battery, (int, float)):
                    battery = int(battery) if 0 <= battery <= 100 else None
                mode = {0: "video", 1: "photo", 2: "multishot"}.get(status_dict.get("43"))
                model_name = status_dict.get("30", "HERO5 Black")
                return CameraStatus(
                    connected=True,
                    ip=self.ip,
                    model=model_name,
                    recording=is_recording,
                    battery_level=battery,
                    mode=mode,
                    raw_status=data
                )
        except Exception as e:
            logger.warning(f"Failed to fetch status: {e}")
        
        return CameraStatus(connected=False, ip=self.ip)

    # --- Controls ---

    async def start_shutter(self) -> bool:
        """Start recording or take photo."""
        res = await self.control_client.get("/gp/gpControl/command/shutter?p=1")
        return res.status_code == 200

    async def stop_shutter(self) -> bool:
        """Stop recording."""
        res = await self.control_client.get("/gp/gpControl/command/shutter?p=0")
        return res.status_code == 200

    async def set_mode(self, mode: str) -> bool:
        """
        Set primary camera mode. HERO5 requires both mode and sub_mode params:
        video (0/0), photo (1/1), multishot (2/0), timelapse (2/3).
        """
        mode_map = {
            "video": (0, 0),
            "photo": (1, 1),
            "multishot": (2, 0),
            "timelapse": (2, 3),
        }
        pair = mode_map.get(mode.lower())
        if pair is None:
            raise ValueError(f"Unknown mode: {mode}. Expected: {list(mode_map.keys())}")
        m, sm = pair
        res = await self.control_client.get(
            f"/gp/gpControl/command/sub_mode?mode={m}&sub_mode={sm}"
        )
        return res.status_code == 200

    async def locate(self, enable: bool = True) -> bool:
        """Beep camera to locate it."""
        p = "1" if enable else "0"
        res = await self.control_client.get(f"/gp/gpControl/command/system/locate?p={p}")
        return res.status_code == 200

    async def delete_file(self, folder: str, filename: str) -> bool:
        """Delete specific file on SD card."""
        res = await self.control_client.get(f"/gp/gpControl/command/storage/delete?p={folder}/{filename}")
        return res.status_code == 200

    # --- Live Preview Streaming ---

    def start_preview_udp(self) -> bool:
        """Send magic UDP packet to port 8554."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"_GPHD_:0:0:2:0.000000\n", (self.ip, 8554))
            sock.close()
            return True
        except Exception as e:
            logger.error(f"Failed to send UDP stream packet: {e}")
            return False

    async def trigger_preview_api(self) -> bool:
        """Execute stream restart command."""
        try:
            res = await self.control_client.get("/gp/gpControl/execute?p1=gpStream&a1=proto_v2&c1=restart")
            return res.status_code == 200
        except Exception:
            return False

    # --- Media / SD Card Access ---

    async def list_media_folders(self) -> List[str]:
        """Fetch list of DCIM folders (e.g. 154GOPRO)."""
        try:
            res = await self.media_client.get("/videos/DCIM/")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                folders = []
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    # Example: /videos/DCIM/154GOPRO/
                    match = re.search(r"(\d{3}GOPRO)/?$", href)
                    if match:
                        folders.append(match.group(1))
                return sorted(folders, reverse=True)
        except Exception as e:
            logger.error(f"Failed to list DCIM folders: {e}")
        return []

    async def list_media_in_folder(self, folder: str) -> List[MediaItem]:
        """Scrape all media files in a specific DCIM folder."""
        items: List[MediaItem] = []
        try:
            res = await self.media_client.get(f"/videos/DCIM/{folder}/")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                rows = soup.find_all("tr")
                # Pre-scan folder files to detect .THM (thumbnails) and .LRV (low-res preview)
                all_filenames = set()
                file_meta: Dict[str, tuple] = {}
                for row in rows:
                    tds = row.find_all("td")
                    if len(tds) < 2:
                        continue
                    a_tag = tds[0].find("a")
                    if not (a_tag and a_tag.get("href")):
                        continue
                    fname = a_tag.text.strip()
                    size_str = None
                    date_str = None
                    # Column order varies by firmware; detect size/date by shape.
                    for td in tds[1:]:
                        text = td.text.strip()
                        if size_str is None and _SIZE_RE.match(text):
                            size_str = text
                        elif date_str is None and _DATE_RE.search(text):
                            date_str = text
                    all_filenames.add(fname)
                    file_meta[fname] = (size_str, date_str)

                for fname, (size_str, date_str) in file_meta.items():
                    lower = fname.lower()
                    size_bytes = parse_size_bytes(size_str)
                    date_ts = parse_date_ts(date_str)
                    if lower.endswith(".mp4"):
                        base_prefix = os.path.splitext(fname)[0]
                        # GoPro pairs videos with .THM (jpeg thumbnail) and .LRV (low-res video)
                        thm_name = f"{base_prefix}.THM"
                        lrv_name = f"{base_prefix}.LRV"
                        thumb_url = f"{self.media_base_url}/videos/DCIM/{folder}/{thm_name}" if thm_name in all_filenames else None
                        lrv_url = f"{self.media_base_url}/videos/DCIM/{folder}/{lrv_name}" if lrv_name in all_filenames else None

                        items.append(MediaItem(
                            name=fname,
                            url=f"{self.media_base_url}/videos/DCIM/{folder}/{fname}",
                            size_str=size_str,
                            size_bytes=size_bytes,
                            date_str=date_str,
                            date_ts=date_ts,
                            directory=folder,
                            thumbnail_url=thumb_url,
                            lrv_url=lrv_url,
                            is_video=True,
                            is_photo=False
                        ))
                    elif lower.endswith(".jpg") or lower.endswith(".gpr"):
                        items.append(MediaItem(
                            name=fname,
                            url=f"{self.media_base_url}/videos/DCIM/{folder}/{fname}",
                            size_str=size_str,
                            size_bytes=size_bytes,
                            date_str=date_str,
                            date_ts=date_ts,
                            directory=folder,
                            thumbnail_url=f"{self.media_base_url}/videos/DCIM/{folder}/{fname}",
                            is_video=False,
                            is_photo=True
                        ))
        except Exception as e:
            logger.error(f"Failed to list media in folder {folder}: {e}")

        return items

    async def list_media(self, folder: Optional[str] = None) -> List[MediaItem]:
        """List all media, or filter by specific folder."""
        folders = [folder] if folder else await self.list_media_folders()
        all_items: List[MediaItem] = []
        for fld in folders:
            folder_items = await self.list_media_in_folder(fld)
            all_items.extend(folder_items)
        return all_items
