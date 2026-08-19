"""Render/encode tests using a synthetic raw frame (no hardware)."""

import io
import zipfile

import numpy as np
import pytest
from PIL import Image

from r10 import render


def _synthetic_raw(lines=400):
    """A frame with dark pre/post-roll bands and a bright paper middle bearing
    a dark horizontal bar (stand-in for text), in the real 3xSEG_PX layout."""
    seg = np.full((lines, render.SEG_PX), 40, np.uint8)      # backing = dark
    seg[80:320] = 210                                        # paper band
    seg[180:200, 500:4600] = 30                              # "text"
    frame = np.repeat(seg[:, None, :], 3, axis=1)            # R=G=B
    return frame.astype(np.uint8).tobytes()


def test_decode_shape():
    g = render.decode_gray300(_synthetic_raw())
    assert g.shape == (400, render.SEG_PX // 2)


def test_autocrop_trims_bands():
    g = render.decode_gray300(_synthetic_raw())
    c = render.autocrop_rows(g)
    assert c.shape[0] < g.shape[0]
    assert c.mean() > g.mean()          # dropped the dark bands


def test_render_page_is_grayscale_image():
    im = render.render_page(_synthetic_raw())
    assert im.mode == "L"
    assert im.size[0] > 0 and im.size[1] > 0


def test_encode_pdf_single_and_multi():
    im = render.render_page(_synthetic_raw())
    data, mime, ext = render.encode([im], "pdf")
    assert mime == "application/pdf" and data[:4] == b"%PDF"
    data, mime, ext = render.encode([im, im, im], "pdf")
    assert ext == "pdf" and data[:4] == b"%PDF"


def test_encode_png_single_bare_multi_zip():
    im = render.render_page(_synthetic_raw())
    data, mime, ext = render.encode([im], "png")
    assert mime == "image/png" and ext == "png"
    data, mime, ext = render.encode([im, im], "png")
    assert mime == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["page_01.png", "page_02.png"]


def test_encode_jpeg_quality():
    im = render.render_page(_synthetic_raw())
    data, mime, ext = render.encode([im], "jpeg", quality=90)
    assert mime == "image/jpeg"
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_encode_rejects_unknown_format():
    im = render.render_page(_synthetic_raw())
    with pytest.raises(ValueError):
        render.encode([im], "bmp")
