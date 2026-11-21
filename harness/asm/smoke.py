"""Smoke test of a library built from the committed generated/nasm (the assembly kernels and the
C glue in asm mode), as a user builds it: the Python binding over it, against plain Python, on
every forced CPU path. The depth is in pytest (tests/test_asm.py and the `asm` backend of the
`native` fixture); this checks that the shipped files assemble, link and run with each toolchain.

    python harness/asm/smoke.py <nvol shared library>
"""
import importlib.util
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(lib):
    os.environ["NVOL_LIB"] = os.path.abspath(lib)
    spec = importlib.util.spec_from_file_location("nvol_ops", os.path.join(ROOT, "generated", "python", "nvol_ops.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nvol_ops"] = mod
    spec.loader.exec_module(mod)
    mod._lib.nvol_test_set_avx2.argtypes = [mod.ctypes.c_int]
    return mod


def none_if_neg(i):
    return None if i < 0 else i


def first_in(hay, members, start, inside=True):
    for i in range(start, len(hay)):
        if (hay[i] in members) == inside:
            return i
    return None


def main(lib) -> int:
    L = load(lib)
    rng = random.Random(1)
    alpha = b"abcxyz\x00\x80\xff"
    checks = 0
    for path in (-1, 0, 1, 2):          # autodetect, no AVX2/SSE4.2, AVX2, SSE4.2 only
        L._lib.nvol_test_set_avx2(path)
        for _ in range(400):
            hay = bytes(rng.choice(alpha) for _ in range(rng.randrange(0, 300)))
            a, b, c = (rng.choice(alpha) for _ in range(3))
            want = [none_if_neg(hay.find(bytes([a]))), none_if_neg(hay.rfind(bytes([a]))),
                    min([i for i in (hay.find(bytes([a])), hay.find(bytes([b]))) if i >= 0], default=None),
                    max([i for i in (hay.rfind(bytes([a])), hay.rfind(bytes([b])), hay.rfind(bytes([c]))) if i >= 0], default=None),
                    hay.count(bytes([a])), hay.count(bytes([a])),
                    next((i for i, x in enumerate(hay) if x >= 0x80), None)]
            got = [L.memchr(a, hay), L.memrchr(a, hay), L.memchr2(a, b, hay), L.memrchr3(a, b, c, hay),
                   L.count(a, hay), L.count_parity(a, hay), L.first_non_ascii(hay)]
            assert got == want, (path, hay, a, b, c, got, want)
            needle = bytes(rng.choice(b"abc") for _ in range(rng.randrange(1, 12)))
            assert L.memmem(hay, needle) == none_if_neg(hay.find(needle)), (path, hay, needle)
            assert L.Finder(needle).find(hay) == none_if_neg(hay.find(needle)), (path, hay, needle)
            members = bytes(rng.choice(alpha) for _ in range(rng.randrange(0, 5)))
            s = L.ByteSet(members)
            start = rng.randrange(len(hay) + 1)
            assert s.find(hay, start) == first_in(hay, members, start), (path, hay, members, start)
            assert s.find_not(hay, start) == first_in(hay, members, start, False), (path, hay, members, start)
            checks += 12
    L._lib.nvol_test_set_avx2(-1)
    print(f"asm smoke: {checks} checks ok ({os.path.basename(lib)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
