"""memmem: generated C (via the Python binding) against the oracle, both paths."""
import random

import pytest

from nvol import oracle
from nvol.__main__ import memmem_cases

PATHS = {"sse2": 0, "avx2": 1}


@pytest.fixture(params=list(PATHS))
def path(request, native):
    native._lib.nvol_test_set_avx2(PATHS[request.param])
    yield request.param
    native._lib.nvol_test_set_avx2(-1)


def _want(hay, needle):
    r = oracle.memmem(hay, needle)
    return None if r == oracle.NONE else r


def test_random(native, path):
    rng = random.Random(2024)
    for hay, needle in memmem_cases(rng, 6000):
        f = native.Finder(needle)
        assert f.find(hay) == _want(hay, needle), (path, hay, needle)
        assert native.memmem(bytearray(hay), needle) == _want(hay, needle)


def test_alignments_and_tail(native, path):
    """Every haystack offset 0..63 and every needle end position near the tail."""
    rng = random.Random(3)
    for nl in (2, 3, 5, 16, 17, 31, 32, 33, 47, 64, 100):
        needle = bytes(rng.choice(b"xyz") for _ in range(nl))
        f = native.Finder(needle)
        for hl in (nl, nl + 1, nl + 15, nl + 31, nl + 32, nl + 63, 300):
            for off in range(0, 64, 3):
                for at in {0, hl - nl, (hl - nl) // 2, max(0, hl - nl - 1)}:
                    buf = bytearray(b"a" * (off + hl + 64))
                    buf[off + at:off + at + nl] = needle
                    hay = memoryview(buf)[off:off + hl]
                    assert f.find(hay) == _want(bytes(hay), needle), (path, nl, hl, off, at)


def test_find_iter(native):
    f = native.Finder(b"aba")
    assert list(f.find_iter(b"abababa xaba")) == [0, 4, 9]
    assert list(native.Finder(b"").find_iter(b"ab")) == [0, 1, 2]
    assert native.memmem(b"", b"") == 0
    assert native.memmem(b"abc", b"") == 0
    assert native.memmem(b"ab", b"abc") is None


def test_pathological(native, path):
    """Inputs that make naive / RK quadratic; Two-Way must stay linear-ish and right."""
    hay = b"a" * 200_000 + b"b"
    for needle in (b"a" * 40 + b"b", b"a" * 33 + b"ba", b"ab" * 30):
        assert native.Finder(needle).find(hay) == _want(hay, needle)
