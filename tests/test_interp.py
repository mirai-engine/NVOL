"""The kernels themselves (IR) against the oracle, under strict UB checks, for every
generated architecture (x86_64 and aarch64)."""
import pytest

from nvol.__main__ import check
from nvol.kernels import build_modules

MODULES = build_modules()


@pytest.mark.parametrize("arch", sorted(MODULES))
def test_interpreter_matches_oracle(arch):
    assert check(MODULES[arch], cases=20, seed=1234) == 0


@pytest.mark.parametrize("name", [f"nvol_{base}_scalar" for base in
                                  ("memchr", "memchr2", "memchr3", "memrchr", "memrchr2", "memrchr3")])
def test_swar_scalar_kernels(name):
    """The word-at-a-time kernels on their own, at every length 0..80 and alignment 0..7: the
    dispatch reaches them only under 16 bytes, but on a target without SIMD they take every
    length. The bytes are the ones that make SWAR's borrow mark a false byte (the needle +-1,
    its high bit flipped, 0x00, 0x7F, 0x80, 0xFF)."""
    import random
    from nvol.interp import Interpreter, Memory
    mod = MODULES["x86_64"]
    n = int(name[-8]) if name[-8].isdigit() else 1
    rev = "memrchr" in name
    rng = random.Random(5)
    for L in range(0, 81):
        for off in range(8):
            for _ in range(6):
                needles = [rng.randrange(256) for _ in range(n)]
                pool = [x for nd in needles for x in (nd, (nd + 1) & 255, (nd - 1) & 255, nd ^ 0x80)]
                pool += [0, 0x7F, 0x80, 0xFF]
                hits = rng.random() < 0.3
                hay = bytes(rng.choice(pool) if hits else rng.choice([x for x in pool if x not in needles] or [0])
                            for _ in range(L))
                base = 0x10000 + off
                got = Interpreter(mod, Memory(hay, base)).call(name, needles + [base, base + L])
                idx = [i for i, c in enumerate(hay) if c in needles]
                want = 0 if not idx else base + (idx[-1] if rev else idx[0])
                assert got == want, (name, L, off, needles, hay.hex(), got, want)


def test_mptr_subtraction_is_rejected():
    """Only PTR has PtrDiff/PtrSub: an MPTR difference would print as an integer subtraction of
    pointers, which C accepts and Rust and Odin reject (the first json_index count did this)."""
    from nvol import ir
    b = ir.Builder("t", ir.USIZE)
    out = b.param("out", ir.MPTR)
    with pytest.raises(TypeError):
        out - out
    with pytest.raises(TypeError):
        out - 4


def _hinted(likely):
    """A function with a hinted if, inside a while (for_languages rebuilds both)."""
    from nvol import ir
    b = ir.Builder("t_hint", ir.U64, public=True)
    x = b.param("x", ir.U64)
    r = b.var("r", 0, ir.U64)
    with b.while_(x.ne(0)):
        with b.if_((x & 1).ne(0), likely=likely):
            b.set(r, r + 1)
        b.set(x, x >> 1)
    b.ret(r)
    return ir.Module("t", [b.build()])


@pytest.mark.parametrize("likely", [False, True])
def test_branch_hint_reaches_each_backend(likely):
    """If.likely changes no result, and every language backend passes it on: C/C++
    NVOL_(UN)LIKELY, Rust cold_path() on the rare side, Odin intrinsics.expect."""
    from nvol import ir
    from nvol.backends import c as c_backend, odin, rust
    from nvol.interp import Interpreter, Memory
    mod = _hinted(likely)
    assert Interpreter(mod, Memory(b"", 0x1000)).call("t_hint", [0b1011001]) == 4
    c = "".join(c_backend.generate(mod, "c").values())
    assert ("NVOL_LIKELY(" if likely else "NVOL_UNLIKELY(") in c.split("t_hint", 1)[1]
    r = "".join(rust.generate(mod).values())
    assert "core::hint::cold_path();" in r
    o = "".join(odin.generate(mod).values())
    assert f"intrinsics.expect(" in o and ("true)" if likely else "false)") in o
