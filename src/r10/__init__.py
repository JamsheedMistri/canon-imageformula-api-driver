"""Userspace driver and API for the Canon imageFORMULA R10 scanner.

The working product path (verified CaptureOnTouch-exact quality):

    CotScanner (cot_scan.py)  - full verified scan choreography
    render.py                 - raw frame -> finished PNG/JPEG/PDF/TIFF
    pipe_transport.py         - SCSI over the FAT file-tunnel (transfer.dat /
                                INDATA.dat on the raw block device)
    cot_pipeline.py           - the reimplemented COT tone pipeline

The original direct-USB (Bulk-Only Transport) attempt was removed as a dead
end (macOS's mass-storage claim); see ``docs/PROCESS.md`` for why.
"""

from .errors import (
    R10Error,
    DeviceNotFoundError,
    TransportError,
    ScsiError,
    NoPaperError,
    NotReadyError,
)
from .cot_scan import CotScanner, find_r10_slice
from .pipe_transport import RawPipeTransport

__all__ = [
    "CotScanner",
    "find_r10_slice",
    "RawPipeTransport",
    "R10Error",
    "DeviceNotFoundError",
    "TransportError",
    "ScsiError",
    "NoPaperError",
    "NotReadyError",
    "VENDOR_ID",
    "PRODUCT_ID",
]

VENDOR_ID = 0x1083
PRODUCT_ID = 0x167F

__version__ = "1.0.0"
