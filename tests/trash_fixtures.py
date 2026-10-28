"""Fake Recycle Bin helpers shared by the trash tests.

A separate module (not ``conftest``) on purpose: ``tests/e2e`` has its own
``conftest.py``, so importing shared helpers from ``conftest`` breaks the
moment the whole suite runs with both directories on ``sys.path``.

Nothing here touches the machine's real Recycle Bin: every helper writes into
the test's ``tmp_path``.
"""
import os

import pytest

RECYCLE_FILETIME_EPOCH = 116444736000000000


def _to_filetime(ts: float) -> int:
    return int(ts * 10_000_000) + RECYCLE_FILETIME_EPOCH


def make_recycle_index_v2(original, size, ts, *, null_terminated=False):
    """Windows NT/2000+ $I record: header, size, FILETIME, UTF-16LE path."""
    chars = original + ("\x00" if null_terminated else "")
    return b"".join([
        (2).to_bytes(8, "little", signed=True),
        size.to_bytes(8, "little", signed=True),
        _to_filetime(ts).to_bytes(8, "little", signed=True),
        len(chars).to_bytes(4, "little", signed=True),
        chars.encode("utf-16-le"),
    ])


def make_recycle_index_v1(original, size, ts):
    """Windows 95/98/ME $I record: 280 bytes, ANSI path."""
    head = b"".join([
        (1).to_bytes(8, "little", signed=True),
        size.to_bytes(8, "little", signed=True),
        _to_filetime(ts).to_bytes(8, "little", signed=True),
    ])
    ansi = original.encode("mbcs" if os.name == "nt" else "latin-1")
    body = ansi + b"\x00"
    return head + body + b"\x00" * (280 - len(head) - len(body))


def write_recycle_entry(bin_dir, original, content, *, suffix="ABC123.txt", ts=1_700_000_000.0, version=2):
    """Creates an $I/$R pair in a fake bin; returns (original_path, data_path)."""
    data_path = bin_dir / f"$R{suffix}"
    data_path.write_bytes(content)
    if version == 1:
        payload = make_recycle_index_v1(str(original), len(content), ts)
    else:
        payload = make_recycle_index_v2(str(original), len(content), ts)
    (bin_dir / f"$I{suffix}").write_bytes(payload)
    return str(original), str(data_path)


@pytest.fixture
def fake_recycle_bin(tmp_path):
    """A fake per-user Recycle Bin folder ($Recycle.Bin\\<SID>)."""
    bin_dir = tmp_path / "recycle" / "$Recycle.Bin" / "S-1-5-21-0-0-0-1001"
    bin_dir.mkdir(parents=True)
    return bin_dir
