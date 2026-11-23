"""simdjson stage 1 (nvol_json_index_raw): the compiled library against the byte-by-byte oracle,
over documents split into several calls (the state carried between them), on each forced
dispatch path."""
import ctypes
import random

import pytest

from nvol import oracle
from nvol.__main__ import json_cases
from nvol.kernels.json import STATE_BYTES
from conftest import paths

PATHS = paths({"no-avx2": 0, "avx2": 1})
P = ctypes.c_void_p


@pytest.fixture(params=list(PATHS))
def lib(request, native):
    L = native._lib
    L.nvol_json_index_raw.argtypes = [P, P, ctypes.c_size_t, ctypes.c_size_t, P]
    L.nvol_json_index_raw.restype = ctypes.c_size_t
    L.nvol_test_set_avx2(PATHS[request.param])
    yield L
    L.nvol_test_set_avx2(-1)


def _index(L, data, splits, state, off):
    """Positions and final state words; the blocks sit `off` bytes into their buffer."""
    buf = ctypes.create_string_buffer(off + len(data))
    ctypes.memmove(ctypes.addressof(buf) + off, data, len(data))
    st = (ctypes.c_uint64 * 4)(*state)
    out = (ctypes.c_uint32 * len(data))()
    pos, at = [], 0
    for k in splits:
        n = L.nvol_json_index_raw(st, ctypes.addressof(buf) + off + 64 * at, k, 64 * at, out)
        pos += list(out[:n])
        at += k
    return pos, tuple(st)


def _same(got, want):
    (gp, gs), (wp, ws) = got, want
    return gp == wp and gs[:3] == ws[:3] and (gs[3] != 0) == (ws[3] != 0)


def test_random(lib):
    rng = random.Random(35)
    for data, splits, state in json_cases(rng, 1500):
        got = _index(lib, data, splits, state, rng.randrange(64))
        want = oracle.json_index(data, 0, state)
        assert _same(got, want), (data, splits, state, got, want)


def test_document(lib):
    """A small document: the positions simdjson gives (an operator, or the first byte of a
    scalar or of a string), and nothing inside the strings."""
    doc = b'{"a": [1, 2.5, true, "x\\"y"], "b\\\\": null}'
    data = doc + b" " * (64 - len(doc))
    pos, st = _index(lib, data, [1], (0, 0, 0, 0), 0)
    assert bytes(data[i] for i in pos) == b'{":[1,2,t,"],":n}'
    assert st == (0, 0, 0, 0)


def test_state_size():
    assert STATE_BYTES == 32


def test_unclosed_string_and_control_byte(lib):
    data = b'"ab\x01' + b" " * 60
    pos, st = _index(lib, data, [1], (0, 0, 0, 0), 0)
    assert pos == [0]
    assert st[1] == (1 << 64) - 1 and st[3] != 0
