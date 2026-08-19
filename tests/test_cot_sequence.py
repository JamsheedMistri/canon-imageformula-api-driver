"""Integrity checks on the bundled verified choreography (no hardware)."""

from r10.cot_scan import load_sequence


def test_sequence_loads():
    seq = load_sequence()
    assert len(seq) == 195
    for r in seq:
        assert isinstance(r["cdb_bytes"], bytes) and len(r["cdb_bytes"]) == 12


def test_exactly_one_feed_before_scans():
    seq = load_sequence()
    ops = [r["cdb_bytes"] for r in seq]
    feeds = [i for i, c in enumerate(ops) if c[0] == 0x31 and c[1] == 0x01]
    scans = [i for i, c in enumerate(ops) if c[0] == 0x1B]
    assert len(feeds) == 1
    assert scans and feeds[0] < scans[0], "feed must precede all SCANs"


def test_scan_window_lists():
    """Calibration SCANs target the internal references (ff/fe, stationary);
    only the FINAL scan uses the document windows 00 01 (moves paper)."""
    seq = load_sequence()
    payloads = [r["payload_bytes"] for r in seq if r["cdb_bytes"][0] == 0x1B]
    assert all(p in (b"\xff\xff", b"\xfe\xfe", b"\x00\x01") for p in payloads)
    assert payloads[-1] == b"\x00\x01"
    assert all(p != b"\x00\x01" for p in payloads[:-1])


def test_final_windows_are_8bit_with_preroll():
    seq = load_sequence()
    windows = [r["payload_bytes"] for r in seq if r["cdb_bytes"][0] == 0x24]
    final = windows[-2:]              # front + back of the document scan
    for w in final:
        assert w[0x22] == 0x08, "final scan must be 8-bit"
        assert w[0x12:0x16] == bytes.fromhex("fffffe28"), "ULy -472 pre-roll"
    for w in windows[:-2]:
        assert w[0x22] == 0x0C, "calibration cycles are 12-bit"


def test_payload_lengths_match_cdb():
    seq = load_sequence()
    for r in seq:
        cdb, p = r["cdb_bytes"], r["payload_bytes"]
        if cdb[0] in (0x24, 0xE1):
            assert p is not None and len(p) == cdb[8]
        elif cdb[0] in (0xD6, 0x1B):
            assert p is not None and len(p) == cdb[4]
