"""Teddy: generated C (via the Python binding) against the oracle.

avx2 path -> Slim256 / Fat256 (and Slim128 below 32 + m - 1 bytes);
sse2 path (AVX2 disabled) -> Slim128 (SSSE3)."""
import random

import pytest

from nvol import oracle
from nvol.__main__ import teddy_cases
from conftest import ARCH, paths

PATHS = paths({"ssse3": 0, "avx2": 1})


@pytest.fixture(params=list(PATHS))
def path(request, native):
    native._lib.nvol_test_set_avx2(PATHS[request.param])
    yield request.param
    native._lib.nvol_test_set_avx2(-1)


def test_random(native, path):
    rng = random.Random(77)
    avx2 = path == "avx2"
    for pats, hay, start, end in teddy_cases(rng, 4000):
        s = native.PackedSearcher.build(pats)
        assert (s is not None) == oracle.teddy_buildable(pats, avx2, True, ARCH), (path, len(pats), pats[:3])
        if s is None:
            continue
        assert s.find(hay, start, end) == oracle.leftmost_first(pats, hay, start, end), (path, pats, hay, start, end)


def test_long_haystacks_and_alignment(native, path):
    rng = random.Random(5)
    for npat in (1, 3, 8, 16, 20, 33, 64):
        for lens in ((1, 1), (2, 3), (3, 3), (4, 7)):
            if lens == (1, 1) and npat > 16:
                continue
            pats = [bytes(rng.choice(b"abcdefgh") for _ in range(rng.randint(*lens))) for _ in range(npat)]
            s = native.PackedSearcher.build(pats)
            assert s is not None
            for hl in (17, 31, 32, 33, 34, 35, 47, 64, 65, 200, 1000):
                for off in range(0, 64, 9):
                    buf = bytearray(b"z" * (off + hl + 64))
                    p = rng.choice(pats)
                    at = rng.randrange(hl - len(p) + 1)
                    buf[off + at:off + at + len(p)] = p
                    hay = memoryview(buf)[off:off + hl]
                    assert s.find(hay) == oracle.leftmost_first(pats, bytes(hay)), (path, npat, lens, hl, off)


def test_find_iter_and_none(native):
    s = native.PackedSearcher.build([b"foo", b"bar", b"fo"])
    assert list(s.find_iter(b"xfoobarfo foox")) == [(0, 1, 4), (1, 4, 7), (2, 7, 9), (0, 10, 13)]
    assert native.PackedSearcher.build([]) is None
    assert native.PackedSearcher.build([b"a", b""]) is None
    # mask 1 with > 16 patterns: rejected on x86_64; aho-corasick's NEON branch never rejects
    assert (native.PackedSearcher.build([bytes([i]) for i in range(17)]) is None) == (ARCH == "x86_64")
    assert native.PackedSearcher.build([b"ab%d" % i for i in range(65)]) is None   # > 64
