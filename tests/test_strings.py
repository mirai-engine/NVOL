"""simdjson's string copy (nvol_json_copy_and_find): the compiled library against the oracle,
on each forced dispatch path, at every source and destination offset within a 64-byte block."""
import ctypes
import random

import pytest

from nvol import oracle
from nvol.__main__ import string_cases
from conftest import paths

PATHS = paths({"sse2": 0, "avx2": 1})


@pytest.fixture(params=list(PATHS))
def lib(request, native):
    L = native._lib
    L.nvol_json_copy_and_find.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    L.nvol_json_copy_and_find.restype = ctypes.c_uint64
    L.nvol_test_set_avx2(PATHS[request.param])
    yield L
    L.nvol_test_set_avx2(-1)


def _call(L, data, so, do):
    src = ctypes.create_string_buffer(so + 32)
    ctypes.memmove(ctypes.addressof(src) + so, data, 32)
    dst = (ctypes.c_uint8 * (do + 48))(*([0xAB] * (do + 48)))
    m = L.nvol_json_copy_and_find(ctypes.addressof(src) + so, ctypes.addressof(dst) + do)
    return m, bytes(dst)


def test_random(lib):
    rng = random.Random(38)
    for data in string_cases(rng, 3000):
        do = rng.randrange(16)
        m, out = _call(lib, data, rng.randrange(64), do)
        assert m == oracle.copy_and_find(data), data
        assert out[do:do + 32] == data, data
        assert out[:do] == b"\xab" * do and out[do + 32:] == b"\xab" * 16, "wrote outside dst[0..32)"


def test_each_position(lib):
    """A single backslash, then a single quote, at each of the 32 bytes."""
    for i in range(32):
        for c, shift in ((0x5C, 0), (0x22, 32)):
            data = bytearray(b"x" * 32)
            data[i] = c
            m, _ = _call(lib, bytes(data), 3, 5)
            assert m == 1 << (i + shift), (i, chr(c))
