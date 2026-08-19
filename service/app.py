"""HTTP API for the Canon imageFORMULA R10, on the verified COT choreography.

Run (root required for raw block-device access):

    sudo .venv/bin/python service/app.py            # 127.0.0.1:8080
    sudo .venv/bin/python service/app.py --port 9000

Endpoints:
    GET  /                    API overview (this table, machine-readable)
    GET  /status              device identity, paper-in-feeder, busy flag
    POST /scan                scan the feeder, return the document bytes
         ?format=pdf|png|jpeg|tiff   (default pdf)
         &max_pages=1..10            (default 10 - all pages in the feeder)
         &quality=1..100             (JPEG quality, default 85)
         &skip_shading=true          (skip the 616 KiB shading readback, faster)
         Single png/jpeg page -> bare image; multiple -> ZIP of page_NN files.
         PDF/TIFF always return one multi-page file.
         Response headers: X-Page-Count, Content-Disposition filename.
    POST /release             close the raw pipe and remount the volume so
                              CaptureOnTouch can be used; the next request
                              reclaims the device automatically.

The scanner is claimed lazily on first use and held (volume unmounted) while
the server runs; a lock serializes access to the single device. A scan takes
roughly 60-90 s per page: feed, 9 stationary calibration cycles against the
internal dark/white references, shading readback, then the slow-feed 8-bit
document pass - the exact sequence CaptureOnTouch runs (protocol.md 6.7.2).
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from r10 import render  # noqa: E402
from r10.cot_scan import CotScanner, find_r10_slice  # noqa: E402
from r10.errors import NoPaperError, R10Error  # noqa: E402


class DeviceManager:
    """Lazily-claimed, lock-serialized access to the single scanner."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scanner: CotScanner | None = None
        self.busy_with: str | None = None

    def acquire(self) -> CotScanner:
        if self.scanner is None:
            slice_dev = find_r10_slice()
            if slice_dev is None:
                raise HTTPException(503, "R10 not attached (no ONTOUCHLITE "
                                         "volume found - is it plugged in "
                                         "and powered on?)")
            self.scanner = CotScanner(slice_dev, log=lambda s: None)
            try:
                self.scanner.open()
            except Exception as exc:
                self.scanner = None
                raise HTTPException(503, f"could not claim scanner: {exc}")
        return self.scanner

    def release(self) -> bool:
        if self.scanner is None:
            return False
        self.scanner.close()
        self.scanner = None
        return True


dev = DeviceManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    dev.release()          # remount the volume on shutdown


app = FastAPI(title="Canon imageFORMULA R10 API", version="1.0.0",
              lifespan=lifespan)


@app.get("/")
def index():
    return {
        "service": "Canon imageFORMULA R10 scanner API",
        "quality": "CaptureOnTouch-exact (verified replay choreography)",
        "endpoints": {
            "GET /status": "device identity, paper-in-feeder, busy flag",
            "POST /scan": {
                "format": "pdf | png | jpeg | tiff (default pdf)",
                "max_pages": "1..10, default 10 (all pages in feeder)",
                "quality": "JPEG quality 1..100, default 85",
                "skip_shading": "true skips the shading readback (faster)",
                "returns": "document bytes; multi-page png/jpeg -> ZIP",
            },
            "POST /release": "free the device so CaptureOnTouch can run",
        },
    }


@app.get("/status")
def status():
    if not dev.lock.acquire(blocking=False):
        return {"attached": True, "busy": True, "busy_with": dev.busy_with}
    try:
        s = dev.acquire()
        return {"attached": True, "busy": False,
                "device": s.device_info(),
                "paper_present": s.paper_present(),
                "slice": s.slice_dev}
    except HTTPException as exc:
        if exc.status_code == 503:
            return {"attached": False, "busy": False, "detail": exc.detail}
        raise
    finally:
        dev.lock.release()


@app.post("/scan")
def scan(format: str = Query("pdf", pattern="^(pdf|png|jpe?g|tiff)$"),
         max_pages: int = Query(10, ge=1, le=10),
         quality: int = Query(85, ge=1, le=100),
         skip_shading: bool = False):
    if not dev.lock.acquire(blocking=False):
        raise HTTPException(409, f"scanner busy ({dev.busy_with})")
    try:
        dev.busy_with = "scan"
        s = dev.acquire()
        s.skip_shading = skip_shading
        if not s.paper_present():
            raise HTTPException(409, "feeder is empty - load a document")
        pages = []
        try:
            for raw in s.scan_batch(max_pages):
                pages.append(render.render_page(raw))
        except NoPaperError:
            raise HTTPException(409, "feeder is empty - load a document")
        except R10Error as exc:
            raise HTTPException(500, f"scan failed: {exc}")
        if not pages:
            raise HTTPException(500, "no pages captured")
        data, mime, ext = render.encode(pages, format, quality=quality)
        name = time.strftime(f"scan_%Y%m%d_%H%M%S.{ext}")
        return Response(content=data, media_type=mime, headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Page-Count": str(len(pages)),
        })
    finally:
        dev.busy_with = None
        dev.lock.release()


@app.post("/release")
def release():
    if not dev.lock.acquire(blocking=False):
        raise HTTPException(409, f"scanner busy ({dev.busy_with})")
    try:
        released = dev.release()
        return {"released": released,
                "detail": "volume remounted" if released
                          else "device was not claimed"}
    finally:
        dev.lock.release()


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="R10 scanner API server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
