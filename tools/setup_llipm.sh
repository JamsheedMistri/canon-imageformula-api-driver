#!/bin/bash
# Prepare Canon's LLiPm image-processing libraries for direct ctypes calls.
#
# The whole point (per docs/protocol.md 6.6): stop approximating CaptureOnTouch's
# image processing and instead run Canon's ACTUAL code on our captured bytes.
# The libraries were pulled off the device (tools/read_archive.py) and live in
#   captures/onboard/app_extract/CaptureOnTouch Lite.app/Contents/Frameworks
#
# They are x86_64 (run under Rosetta) and reference each other via
# @executable_path, which won't resolve when loaded by a stray Python. This copies
# the three needed Mach-O files out, rewrites their install names to absolute
# paths, and ad-hoc re-signs them so dyld will load them via ctypes.
#
# Output: /tmp/cotfw/{pafcv2,rdd20,LLiPmDRP215}
set -euo pipefail

FW="captures/onboard/app_extract/CaptureOnTouch Lite.app/Contents/Frameworks"
OUT="${1:-/tmp/cotfw}"
mkdir -p "$OUT"

cp "$FW/pafcv2.framework/Versions/A/pafcv2" "$OUT/pafcv2"
cp "$FW/rdd20.framework/Versions/A/rdd20" "$OUT/rdd20"
cp "$FW/R10Lite.ds/Contents/Frameworks/LLiPmDRP215.framework/Versions/A/LLiPmDRP215" "$OUT/LLiPmDRP215"
chmod u+w "$OUT"/*

install_name_tool -id "$OUT/pafcv2" "$OUT/pafcv2"
install_name_tool -id "$OUT/rdd20" "$OUT/rdd20"
install_name_tool -id "$OUT/LLiPmDRP215" \
  -change "@executable_path/../Frameworks/DRP215IILite.ds/Contents/Frameworks/LLiPmDRP215.framework/Versions/A/LLiPmDRP215" "$OUT/LLiPmDRP215" \
  -change "@executable_path/../Frameworks/pafcv2.framework/Versions/A/pafcv2" "$OUT/pafcv2" \
  -change "@executable_path/../Frameworks/rdd20.framework/Versions/A/rdd20" "$OUT/rdd20" \
  "$OUT/LLiPmDRP215"

codesign --remove-signature "$OUT"/* 2>/dev/null || true
codesign --force --sign - "$OUT/pafcv2" "$OUT/rdd20" "$OUT/LLiPmDRP215"

echo "prepared LLiPm in $OUT"
