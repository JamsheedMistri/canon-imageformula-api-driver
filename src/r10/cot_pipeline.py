"""Faithful reimplementation of CaptureOnTouch's document tone pipeline.

Recovered from LLiPmDRP215 (docs/protocol.md 6.6). COT turns the raw sensor
frame (a washed-out midtone blob: paper ~166, text ~135) into a bimodal
paper-white/text-black document. The steps, in order:

1. Shading: normalize each column (and row) by its own paper-white level so the
   fixed-pattern sensor gain is removed and paper becomes flat. COT derives the
   white level from the on-board reference; the paper's own bright pixels give
   the same per-column profile and are more robust for our raw path.
2. Denoise: FilterSimplex applies an edge-preserving smooth so the subsequent
   contrast stretch doesn't amplify sensor grain. We use a median + mild blur,
   which preserves text edges while killing salt-and-pepper grain.
3. Levels: makeGammaDataforFC builds a piecewise-linear LUT
       out[v] = 0                         v < lo
              = (v - lo) * 255 / (hi - lo) lo <= v < hi
              = 255                        v >= hi
   i.e. a black-point/white-point stretch. This is the exact routine COT uses;
   see disassembly of __Z18makeGammaDataforFCiiPhj.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


# AdjustAnaproOffset (llipm 0x35c4) drives the sensor's analog black level to a
# target of 0x60 == 96 counts (`add eax, -0x60`), so the calibrated dark floor of
# every capture is 96. Confirmed against our raw (floor ~94-96).
DARK_TARGET = 96.0

# Cei::LLiPm::DRHachi::GammaBuilderImp::calcGrayGamma coefficient tables, dumped
# from llipm (x2table 0xa08b0, y2table 0xa08f0, ttable 0xa0930, plus the highlight
# tables at 0xa1130/0xa13f0). Index 1..7 selects the contrast preset; index 0 is
# the sentinel -1. Preset 4 (gain 1.0, offset -120) is the neutral document default.
_GG_X2 = (-1.0, 17.0, 23.0, 29.0, 34.0, 42.0, 50.0, 56.0)
_GG_Y2 = (-1.0, 58.0, 54.0, 52.0, 48.0, 41.0, 38.0, 33.0)
_GG_TT = (-1.0, 0.988235, 1.129412, 1.270588, 1.411765, 1.694118, 1.976471, 2.258824)
_GG_T3 = (-1.0, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4, 1.6)
_GG_T4 = (-1.0, -28.0, -59.0, -90.0, -120.0, -182.0, -244.0, -306.0)
_GG_K = 422.0
_GG_GAMMA = 1.0 / 2.2  # 0.45454..., the pow() exponent (llipm const 0x9ec30)


def gray_gamma_lut(contrast: int = 4, brightness: int = 128) -> np.ndarray:
    """Exact port of DRHachi::calcGrayGamma: the R10's photographic gray tone
    curve. A linear toe for shadows joined to a pow(x, 1/2.2) highlight segment,
    per the disassembly at llipm 0x1fa46. `contrast` is the 1..7 preset, 4 is
    neutral; `brightness` is the 0..255 slider (128 == no shift)."""
    c = max(1, min(7, int(contrast)))
    b = (int(brightness) - 128) * 128.0 / 127.0
    x2, y2, tt = _GG_X2[c], _GG_Y2[c], _GG_TT[c]
    k, t4 = _GG_T3[c] * _GG_K, _GG_T4[c]
    a = np.arange(256, dtype=np.float64)
    shadow = tt * (b + a - x2) + y2
    x = np.maximum((b + a) / 255.0, 0.0)
    highlight = np.power(x, _GG_GAMMA) * k + t4 + 0.5
    out = np.where((x2 - b) >= a, shadow, highlight)
    return np.clip(out, 0, 255).astype(np.uint8)


def make_gamma_data(lo: int, hi: int, fill: int = 0) -> np.ndarray:
    """Exact port of makeGammaDataforFC: piecewise-linear levels LUT (256)."""
    lo = max(0, min(255, int(lo)))
    hi = max(1, min(255, int(hi)))
    if hi <= lo:
        hi = lo + 1
    table = np.empty(256, np.uint8)
    table[:lo] = fill & 0xFF
    idx = np.arange(lo, hi, dtype=np.int64)
    # (i*255)//(hi-lo) with i = v-lo, matching the integer idiv in the binary
    table[lo:hi] = (((idx - lo) * 255) // (hi - lo)).astype(np.uint8)
    if hi <= 255:
        table[hi:] = 255
    return table


def _smooth1d(v: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return v
    k = np.ones(win, np.float32) / win
    pad = win // 2
    vp = np.pad(v, pad, mode="edge")
    return np.convolve(vp, k, mode="same")[pad:-pad] if pad else np.convolve(vp, k, mode="same")


def _background(f: np.ndarray, ds: int = 6, win: int = 7, blur: int = 21) -> np.ndarray:
    """Smooth 2D paper-white estimate via downsampled local-max (paper is the
    bright envelope; text/marks are darker and rejected by the max), then a
    heavy blur so only the low-frequency illumination remains (not content)."""
    h, w = f.shape
    im = Image.fromarray(np.clip(f, 0, 255).astype(np.uint8), "L")
    sm = im.resize((max(1, w // ds), max(1, h // ds)), Image.BILINEAR)
    sm = sm.filter(ImageFilter.MaxFilter(win))
    sm = sm.filter(ImageFilter.MedianFilter(win))
    sm = sm.filter(ImageFilter.GaussianBlur(blur))
    bg = sm.resize((w, h), Image.BILINEAR)
    return np.asarray(bg).astype(np.float32)


def shade(front: np.ndarray, white_pct: float = 97.0, smooth: int = 33) -> np.ndarray:
    """COT's per-column shading (AdjustLightCurve core, llipm 0x1d5da):
        out = (raw - dark) / (white - dark)
    where `dark` is the calibrated black level (DARK_TARGET, 0x60) and `white` is
    the per-column paper reference. Returns reflectance with paper ~= 1.0. A final
    smooth-2D-background divide removes residual illumination falloff."""
    f = front.astype(np.float32)
    sig = np.maximum(f - DARK_TARGET, 0.0)              # subtract analog dark floor
    col = np.percentile(sig, white_pct, axis=0)         # per-column white - dark
    col = np.maximum(_smooth1d(col, smooth), 1.0)
    refl = sig / col[None, :]                            # (raw-dark)/(white-dark)
    bg = _background(np.clip(refl * 235.0, 0, 255)) / 235.0
    refl = refl / np.maximum(bg, 1e-3) * float(np.median(bg))
    return refl  # paper ~= 1.0 everywhere


def _boxfilter(img: np.ndarray, r: int) -> np.ndarray:
    """O(N) box filter (mean over (2r+1)^2 window) via integral images."""
    h, w = img.shape
    cum = np.cumsum(img, axis=0)
    out = np.empty_like(img)
    out[: r + 1] = cum[r: 2 * r + 1]
    out[r + 1: h - r] = cum[2 * r + 1:] - cum[: h - 2 * r - 1]
    out[h - r:] = cum[h - 1] - cum[h - 2 * r - 1: h - r - 1]
    cum = np.cumsum(out, axis=1)
    out[:, : r + 1] = cum[:, r: 2 * r + 1]
    out[:, r + 1: w - r] = cum[:, 2 * r + 1:] - cum[:, : w - 2 * r - 1]
    out[:, w - r:] = cum[:, w - 1:] - cum[:, w - 2 * r - 1: w - r - 1]
    n = np.empty((h, w), np.float32)
    cnt_r = np.minimum(np.arange(h) + r + 1, h) - np.maximum(np.arange(h) - r, 0)
    cnt_c = np.minimum(np.arange(w) + r + 1, w) - np.maximum(np.arange(w) - r, 0)
    n[:] = cnt_r[:, None] * cnt_c[None, :]
    return out / n


def guided_denoise(img: np.ndarray, radius: int = 4, eps: float = 6.0) -> np.ndarray:
    """Edge-preserving smoothing (self-guided filter): flattens paper grain but
    keeps text edges. This is what FilterSimplex does before the tone stretch,
    so the contrast stretch produces clean white paper without speckle."""
    I = img.astype(np.float32)
    mean_I = _boxfilter(I, radius)
    mean_II = _boxfilter(I * I, radius)
    var = mean_II - mean_I * mean_I
    a = var / (var + eps * eps)
    b = mean_I - a * mean_I
    return _boxfilter(a, radius) * I + _boxfilter(b, radius)


def destripe(scaled: np.ndarray, paper_thresh: float = 200.0,
             white: float = 235.0) -> np.ndarray:
    """Remove fixed-pattern stripe noise by normalizing the paper level per
    column and per row. The column FPN in our raw scans measures ~8 counts std -
    the dominant structured noise - and a robust (median-of-paper) profile
    removes it without touching content, since text strokes are rejected by the
    paper mask. This is the same quantity AdjustLightCurve corrects live from
    the on-board white reference."""
    import warnings

    def paper_median(img: np.ndarray, axis: int) -> np.ndarray:
        masked = np.where(img > paper_thresh, img, np.nan)
        with warnings.catch_warnings():
            # columns/rows with no paper pixels (page edges) yield all-NaN
            # slices; they fall back to `white` below.
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(masked, axis=axis)
        return np.nan_to_num(med, nan=white)

    col = paper_median(scaled, 0)
    flat = scaled / np.maximum(col, 64.0)[None, :] * white
    row = paper_median(flat, 1)
    return np.clip(flat / np.maximum(row, 64.0)[:, None] * white, 0.0, 255.0)


def process(front: np.ndarray, *, black_pt: int = 150, white_pt: int = 222,
            white_scale: float = 235.0, gd_radius: int = 3, gd_eps: float = 40.0,
            median: int = 3, sharpen: int = 160, rotate: int = 90) -> Image.Image:
    """Full COT-style document render (the recipe that measurably matches the
    CaptureOnTouch reference on the same page):

      1. shade():   (raw - 96) / (white - 96) per column  (AdjustLightCurve).
      2. destripe(): remove residual column/row fixed-pattern paper noise.
      3. guided_denoise(): small-radius edge-preserving smooth (FilterSimplex
         role). Radius must stay ~3: larger radii smear 2-4 px text strokes.
      4. make_gamma_data(): COT's levels stretch (makeGammaDataforFC). The
         measured separation on the default-exposure capture is ink p5 ~137,
         paper ~222 after destripe, hence the 150/222 defaults.
      5. median + unsharp: consolidate strokes, whiten paper.

    The photographic calcGrayGamma path is deliberately NOT used here: it is
    tone-preserving and leaves faint print gray; COT's document look comes from
    the FC levels stretch after noise removal."""
    refl = shade(front)
    scaled = np.clip(refl * white_scale, 0, 255.0).astype(np.float32)
    flat = destripe(scaled, white=white_scale)
    dn = guided_denoise(flat, radius=gd_radius, eps=gd_eps)
    dn = np.clip(dn, 0, 255).astype(np.uint8)
    im = Image.fromarray(make_gamma_data(black_pt, white_pt)[dn], "L")
    if median:
        im = im.filter(ImageFilter.MedianFilter(median))
    if sharpen:
        im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=sharpen, threshold=3))
    if rotate:
        im = im.rotate(-rotate, expand=True)
    return im
