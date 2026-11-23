"""UTF-8 validation (nvol_utf8_valid): the compiled library against Python's strict decoder,
on each forced dispatch path and at every start offset within a 64-byte block."""
import ctypes
import random

import pytest

from nvol import oracle
from nvol.__main__ import utf8_cases
from conftest import paths

PATHS = paths({"no-avx2": 0, "avx2": 1})


@pytest.fixture(params=list(PATHS))
def lib(request, native):
    L = native._lib
    L.nvol_utf8_valid.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    L.nvol_utf8_valid.restype = ctypes.c_size_t
    L.nvol_test_set_avx2(PATHS[request.param])
    yield L
    L.nvol_test_set_avx2(-1)


def _valid(L, data, off):
    buf = ctypes.create_string_buffer(off + len(data))
    ctypes.memmove(ctypes.addressof(buf) + off, data, len(data))
    return L.nvol_utf8_valid(ctypes.addressof(buf) + off, len(data))


def test_random(lib):
    rng = random.Random(36)
    for data in utf8_cases(rng, 3000):
        assert _valid(lib, data, rng.randrange(64)) == oracle.utf8_valid(data), data


def test_every_code_point_boundary(lib):
    """Each invalid form once, after a prefix that puts it at every offset of a block."""
    bad = [b"\xc0\x80", b"\xe0\x9f\xbf", b"\xed\xa0\x80", b"\xf0\x8f\xbf\xbf", b"\xf4\x90\x80\x80",
           b"\x80", b"\xc3", b"\xe2\x82"]
    good = [chr(c) for c in (0xE9, 0x20AC, 0xD7FF, 0xE000, 0x1F600, 0x10FFFF)]
    for k in range(130):
        pre = b"a" * k
        for g in good:
            data = pre + g.encode() + b"z" * 70
            assert _valid(lib, data, 0) == 1, (k, g)
        for x in bad:
            data = pre + x + b"z" * 70
            assert _valid(lib, data, 0) == 0, (k, x)


def test_empty_and_ascii(lib):
    assert _valid(lib, b"", 0) == 1
    assert _valid(lib, b"x" * 1000, 3) == 1
