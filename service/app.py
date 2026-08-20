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
         &calibration=cached|full    (default cached - reuse the warm
                                      calibration; full = COT's complete
                                      per-scan choreography)
         Single png/jpeg page -> bare image; multiple -> ZIP of page_NN files.
         PDF/TIFF always return one multi-page file.
         Response headers: X-Page-Count, Content-Disposition filename.
    POST /release             close the raw pipe and remount the volume so
                              CaptureOnTouch can be used; the next request
                              reclaims the device automatically.

The scanner is claimed lazily on first use and held (volume unmounted) while
the server runs; a lock serializes access to the single device.

Calibration caching (protocol.md 6.7.4): a background thread claims the device
at startup and re-runs the stationary AGC calibration (no paper needed) every
--calib-interval seconds (default 300), so the scanner's registers + shading
stay warm. /scan defaults to ?calibration=cached, which skips straight to
feed + document pass (a few seconds to first feed instead of ~60-90 s).
?calibration=full runs CaptureOnTouch's complete per-scan choreography for
A/B quality comparison. A scan that arrives mid-calibration waits for it to
finish, then starts immediately.
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
        self.device_info: dict | None = None   # cached identity (static)

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
                self.device_info = self.scanner.device_info()
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

# Background recalibration interval (seconds); overridable via CLI.
CALIB_INTERVAL_S = 300.0
_RETRY_S = 30.0            # retry cadence while the device is unplugged/busy


def _calibrator_loop(stop: threading.Event) -> None:
    """Keep the scanner's calibration warm: claim the device at startup and
    re-run the stationary AGC calibration whenever it goes stale. Runs with
    an empty feeder (the calibration never touches paper)."""
    while not stop.is_set():
        wait = _RETRY_S
        if dev.lock.acquire(blocking=False):
            try:
                dev.busy_with = "calibration"
                try:
                    s = dev.acquire()
                except HTTPException as exc:
                    print(f"[calibrator] device unavailable: {exc.detail}")
                else:
                    age = s.calibration_age
                    if age is None or age >= CALIB_INTERVAL_S:
                        s.paper_present()   # seed the paper hint for /status
                        print("[calibrator] warm calibration ...")
                        t0 = time.time()
                        s.warm_calibrate()
                        print(f"[calibrator] done in {time.time() - t0:.1f} s")
                        wait = CALIB_INTERVAL_S
                    else:
                        # A scan's full calibration (or a recent cycle)
                        # already refreshed it; wake up when it goes stale.
                        wait = CALIB_INTERVAL_S - age
            except Exception as exc:                      # noqa: BLE001
                print(f"[calibrator] calibration failed: {exc}")
            finally:
                dev.busy_with = None
                dev.lock.release()
        else:
            wait = 10.0    # a scan is running; check again shortly
        stop.wait(wait)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=_calibrator_loop, args=(stop,),
                              name="calibrator", daemon=True)
    thread.start()
    yield
    stop.set()
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
                "calibration": "cached (default, instant) | full (COT's "
                               "complete per-scan choreography)",
                "returns": "document bytes; multi-page png/jpeg -> ZIP",
            },
            "POST /release": "free the device so CaptureOnTouch can run",
        },
    }


@app.get("/status")
def status():
    if not dev.lock.acquire(blocking=False):
        # A background calibration is invisible to clients: report the
        # last-known paper state (refreshed between calibration cycles) as
        # if the scanner were idle. A /scan issued now simply waits for the
        # calibration to finish.
        if dev.busy_with == "calibration" and dev.scanner is not None:
            s = dev.scanner
            age = s.calibration_age
            return {"attached": True, "busy": False,
                    "device": dev.device_info,
                    "paper_present": s.paper_hint,
                    "slice": s.slice_dev,
                    "calibrating": True,
                    "calibration_age_s": (round(age) if age is not None
                                          else None)}
        return {"attached": True, "busy": True, "busy_with": dev.busy_with}
    try:
        s = dev.acquire()
        age = s.calibration_age
        return {"attached": True, "busy": False,
                "device": s.device_info(),
                "paper_present": s.paper_present(),
                "slice": s.slice_dev,
                "calibration_age_s": round(age) if age is not None else None}
    except HTTPException as exc:
        if exc.status_code == 503:
            return {"attached": False, "busy": False, "detail": exc.detail}
        raise
    finally:
        dev.lock.release()


def _acquire_for_scan(wait_s: float = 120.0) -> None:
    """Take the device lock, waiting out an in-flight background calibration
    (so a scan issued mid-calibration starts the moment it finishes) and
    transient /status probes (which hold the lock sub-second without setting
    busy_with). Fails fast only if the device is busy with another scan."""
    deadline = time.time() + wait_s
    while not dev.lock.acquire(blocking=False):
        if dev.busy_with == "scan":
            raise HTTPException(409, "scanner busy (scan)")
        if time.time() > deadline:
            raise HTTPException(409, f"scanner busy ({dev.busy_with})")
        time.sleep(0.5)


@app.post("/scan")
def scan(format: str = Query("pdf", pattern="^(pdf|png|jpe?g|tiff)$"),
         max_pages: int = Query(10, ge=1, le=10),
         quality: int = Query(85, ge=1, le=100),
         skip_shading: bool = False,
         calibration: str = Query("cached", pattern="^(cached|full)$")):
    _acquire_for_scan()
    try:
        dev.busy_with = "scan"
        s = dev.acquire()
        s.skip_shading = skip_shading
        if not s.paper_present():
            raise HTTPException(409, "feeder is empty - load a document")
        use_cached = calibration == "cached" and s.last_calibrated is not None
        calib_age = s.calibration_age if use_cached else None
        pages = []
        try:
            for raw in s.scan_batch(max_pages,
                                    use_cached_calibration=use_cached):
                pages.append(render.render_page(raw))
        except NoPaperError:
            raise HTTPException(409, "feeder is empty - load a document")
        except R10Error as exc:
            raise HTTPException(500, f"scan failed: {exc}")
        if not pages:
            raise HTTPException(500, "no pages captured")
        data, mime, ext = render.encode(pages, format, quality=quality)
        name = time.strftime(f"scan_%Y%m%d_%H%M%S.{ext}")
        headers = {
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Page-Count": str(len(pages)),
            "X-Calibration": "cached" if use_cached else "full",
        }
        if calib_age is not None:
            headers["X-Calibration-Age"] = f"{calib_age:.0f}"
        return Response(content=data, media_type=mime, headers=headers)
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
    ap.add_argument("--calib-interval", type=float, default=300.0,
                    help="seconds between background recalibrations "
                         "(default 300)")
    args = ap.parse_args()
    CALIB_INTERVAL_S = args.calib_interval
    uvicorn.run(app, host=args.host, port=args.port)
