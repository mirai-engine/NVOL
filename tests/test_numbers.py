"""Eight-digit number parsing (nvol_is_eight_digits, nvol_parse_eight_digits): the compiled
library against Python's int(), on each forced dispatch path (the SWAR and the SSE4.1 one).
The 8 bytes are the last of their buffer; the interpreter checks (nvol check) that no
implementation reads past them."""
import ctypes
import random

import pytest

from nvol import oracle
from nvol.__main__ import digit_cases
from conftest import paths

PATHS = paths({"swar": 0, "sse42": 2})


@pytest.fixture(params=list(PATHS))
def lib(request, native):
    L = native._lib
    for name in ("nvol_is_eight_digits", "nvol_parse_eight_digits"):
        getattr(L, name).argtypes = [ctypes.c_void_p]
        getattr(L, name).restype = ctypes.c_uint32
    L.nvol_test_set_avx2(PATHS[request.param])
    yield L
    L.nvol_test_set_avx2(-1)


def _call(L, name, data):
    buf = ctypes.create_string_buffer(64)
    ctypes.memmove(ctypes.addressof(buf) + 56, data, 8)
    return getattr(L, name)(ctypes.addressof(buf) + 56)


def test_every_byte_at_every_position(lib):
    """nvol_is_eight_digits on '5' * 8 with one byte replaced by each of the 256 values."""
    for i in range(8):
        for c in range(256):
            data = bytearray(b"5" * 8)
            data[i] = c
            assert _call(lib, "nvol_is_eight_digits", bytes(data)) == oracle.is_eight_digits(bytes(data)), (i, c)


def test_random(lib):
    rng = random.Random(37)
    for data in digit_cases(rng, 3000):
        assert _call(lib, "nvol_is_eight_digits", data) == oracle.is_eight_digits(data), data
        if oracle.is_eight_digits(data):
            assert _call(lib, "nvol_parse_eight_digits", data) == int(data), data


def test_extremes(lib):
    for data in (b"00000000", b"99999999", b"00000001", b"10000000", b"12345678", b"87654321"):
        assert _call(lib, "nvol_parse_eight_digits", data) == int(data), data
