"""Decode + render raw COT-choreography frames to finished documents.

Frame format (measured, docs/protocol.md 6.7.2): lines of 15,312 bytes, each
three 5,104-px 8-bit segments = the R/G/B channels of the front side at
600 dpi horizontal x 300 dpi vertical. The render recipe below is the one
verified against the CaptureOnTouch reference PDF on the same page.
"""

from __future__ import annotations

import io
import zipfile

import numpy as np
from PIL import Image, ImageFilter

from .cot_pipeline import destripe, make_gamma_data

LINE_STRIDE = 15312
SEG_PX = 5104
DPI = 300


def decode_gray300(raw: bytes) -> np.ndarray:
    """Raw frame -> float32 grayscale at 300x300 dpi (uncorrected)."""
    lines = len(raw) // LINE_STRIDE
    if lines < 8:
        raise ValueError(f"frame too short: {len(raw)} bytes")
    arr = np.frombuffer(raw, np.uint8, count=lines * LINE_STRIDE)
    arr = arr.reshape(lines, 3, SEG_PX).astype(np.float32)
    gray600 = arr.mean(axis=1)                     # R/G/B -> luma
    return (gray600[:, 0::2] + gray600[:, 1::2]) / 2.0   # 600 -> 300 dpi


def autocrop_rows(gray: np.ndarray, margin: int = 6) -> np.ndarray:
    """Trim the pre-roll / post-roll backing bands (the window's ULy = -472
    starts the capture before the page reaches the sensor).

    The raw signal is dim and low-contrast (backing row mean ~130, paper
    ~142 on the reference capture), so the threshold is the midpoint between
    the darkest rows (backing) and the bulk (paper), and only the OUTER
    bounds are cropped - interior dark rows (dense text) are content."""
    rm = gray.mean(axis=1)
    lo, hi = np.percentile(rm, 2), np.percentile(rm, 75)
    if hi - lo < 4:            # page fills the frame; nothing to trim
        return gray
    good = np.flatnonzero(rm > (lo + hi) / 2)
    if good.size < 100:
        return gray
    top = max(int(good[0]) - margin, 0)
    bot = min(int(good[-1]) + margin + 1, gray.shape[0])
    return gray[top:bot]


def render_page(raw: bytes, *, black_pt: int = 150, white_pt: int = 225,
                paper_target: float = 235.0, median: int = 3,
                sharpen: int = 140, rotate: int = -90) -> Image.Image:
    """Full verified render: decode -> crop -> normalize paper -> destripe ->
    COT levels stretch (makeGammaDataforFC) -> median -> unsharp -> rotate."""
    gray = autocrop_rows(decode_gray300(raw))
    paper = float(np.percentile(gray, 90))
    scaled = np.clip(gray * (paper_target / max(paper, 1.0)), 0, 255.0)
    flat = destripe(scaled.astype(np.float32), white=paper_target)
    im = Image.fromarray(
        make_gamma_data(black_pt, white_pt)[np.clip(flat, 0, 255)
                                            .astype(np.uint8)], "L")
    if median:
        im = im.filter(ImageFilter.MedianFilter(median))
    if sharpen:
        im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=sharpen,
                                               threshold=3))
    if rotate:
        im = im.rotate(rotate, expand=True)
    return im


# -- output encoding ---------------------------------------------------------

MIME = {"png": "image/png", "jpeg": "image/jpeg", "pdf": "application/pdf",
        "zip": "application/zip", "tiff": "image/tiff"}


def encode(pages: list[Image.Image], fmt: str, *,
           quality: int = 85) -> tuple[bytes, str, str]:
    """Encode rendered pages -> (bytes, mime type, file extension).

    ``pdf`` and ``tiff`` are multi-page containers. For ``png``/``jpeg`` a
    single page returns the bare image; multiple pages return a ZIP with
    page_NN entries.
    """
    fmt = fmt.lower().replace("jpg", "jpeg")
    if fmt not in ("pdf", "png", "jpeg", "tiff"):
        raise ValueError(f"unsupported format {fmt!r}")
    buf = io.BytesIO()
    if fmt in ("pdf", "tiff"):
        pages[0].save(buf, fmt.upper(), save_all=True,
                      append_images=pages[1:], resolution=DPI,
                      dpi=(DPI, DPI))
        return buf.getvalue(), MIME[fmt], fmt
    if len(pages) == 1:
        _save_image(pages[0], buf, fmt, quality)
        return buf.getvalue(), MIME[fmt], fmt
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i, page in enumerate(pages, 1):
            pbuf = io.BytesIO()
            _save_image(page, pbuf, fmt, quality)
            zf.writestr(f"page_{i:02d}.{fmt}", pbuf.getvalue())
    return buf.getvalue(), MIME["zip"], "zip"


def _save_image(im: Image.Image, buf: io.BytesIO, fmt: str,
                quality: int) -> None:
    if fmt == "jpeg":
        im.save(buf, "JPEG", quality=quality, dpi=(DPI, DPI))
    else:
        im.save(buf, fmt.upper(), dpi=(DPI, DPI))
