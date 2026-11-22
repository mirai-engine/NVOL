"""Random IR programs through the assembly backend, against the reference interpreter.

The kernels exercise what the library needs; these programs exercise what the backend must get
right for any IR: every scalar operation on every integer width, comparisons and short-circuit
conditions, casts, loads and stores at bounded offsets, table loads, scalar primitives, nested
`If`/`While`, early returns, inlined calls (with a callee that assigns its parameters), calls
out of line with 7 arguments (stack arguments on both ABIs) inside loops, and ~20 values live
at once (spills, values across calls). Each program is valid by construction (wrapping
arithmetic, masked shift counts and indices, lossless casts, bounded loops), so the
interpreter never reports UB; a case that does is a generator bug and fails the test.

Inlined helpers return early (from their top level, which inside a caller's loop is a loop
exit the backend moves out of line, and from their own loops), and their results are assigned
straight to variables.

Programs and inputs come from fixed seeds; NVOL_FUZZ=<programs>,<seed> runs another batch."""
import os
import random

import pytest

import test_asm
from nvol import ir
from nvol.ir import BOOL, MPTR, PTR, U8, U32, U64, USIZE, Builder

INTS = [U8, U32, U64, USIZE]
WIDTH = {U8: 8, U32: 32, U64: 64, USIZE: 64}
PROGRAMS, SEED = (int(x) for x in os.environ.get("NVOL_FUZZ", "48,1").split(","))
BUF = 64


class Gen:
    def __init__(self, rng: random.Random, b: Builder, helpers: list):
        self.rng, self.b, self.helpers = rng, b, helpers
        self.scopes = [[]]          # [(Var, mutable)]
        self.p = self.out = None
        self.loops = 0

    # ---------------------------------------------------------------- variables
    def visible(self, ty=None, mutable=False):
        return [v for s in self.scopes for v, m in s
                if (ty is None or v.ty is ty) and (m or not mutable)]

    def bind(self, v, mutable=False):
        self.scopes[-1].append((v, mutable))
        return v

    # ---------------------------------------------------------------- expressions
    def const(self, ty):
        if ty is BOOL:
            return ir.Const(self.rng.randrange(2), BOOL)
        w = WIDTH[ty]
        v = self.rng.choice([0, 1, 2, 7, 0x7F, 0x80, 0xFF, (1 << w) - 1, 1 << (w - 1),
                             self.rng.randrange(1 << w)])
        return ir.Const(v & ((1 << w) - 1), ty)

    def expr(self, ty, d):
        r = self.rng
        if d <= 0 or r.random() < 0.2:
            vs = self.visible(ty)
            return r.choice(vs) if vs and r.random() < 0.8 else self.const(ty)
        if ty is BOOL:
            k = r.randrange(5)
            if k == 0:
                return ir.Not(self.expr(BOOL, d - 1))
            if k == 1:
                return (ir.land if r.random() < 0.5 else ir.lor)(self.expr(BOOL, d - 1), self.expr(BOOL, d - 1))
            t = r.choice(INTS)
            a, b = self.expr(t, d - 1), self.expr(t, d - 1)
            return ir.Bin(r.choice(["lt", "le", "gt", "ge", "eq", "ne"]), a, b, BOOL)
        w = WIDTH[ty]
        k = r.randrange(12)
        a = lambda: self.expr(ty, d - 1)
        if k == 0:
            return ir.Bin(r.choice(["wadd", "wsub", "wmul"]), a(), a(), ty)
        if k == 1:
            return ir.Bin(r.choice(["and", "or", "xor"]), a(), a(), ty)
        if k == 2:
            n = ir.Bin("and", a(), ir.Const(w - 1, ty), ty)
            return ir.Bin(r.choice(["shl", "shr"]), a(), n, ty)
        if k == 3:
            return ir.Bin(r.choice(["max", "min"]), a(), a(), ty)
        if k == 4:                                     # cast: widen, or narrow a masked value
            src = r.choice(INTS + [BOOL])
            x = self.expr(src, d - 1)
            if src is not BOOL and WIDTH[src] > w:
                x = ir.Bin("and", x, ir.Const((1 << w) - 1, src), src)
            return x.cast(ty)
        if k == 5:                                     # loads from the input buffer
            n = {U8: 1, U32: r.choice([2, 4]), U64: 8, USIZE: 8}[ty]
            ptr = self.p + self.index(BUF - n)
            if n == 1:
                return ir.Load8(ptr)
            x = ir.prim({2: "load16u", 4: "load32u", 8: "load64"}[n], ptr)
            return x if x.ty is ty else x.cast(ty)
        if k == 6 and ty is U8:
            return ir.TableLoad("fz", self.expr(U8, d - 1).cast(USIZE))
        if k == 7 and ty is USIZE:
            name = r.choice(["popcnt", "first_offset", "last_offset", "first_offset64", "last_offset64"])
            mt, mw = (U64, 64) if name.endswith("64") else (U32, 32)
            m = self.expr(mt, d - 1)
            if name != "popcnt":
                m = ir.Bin("or", m, ir.Const(1 << r.randrange(mw), mt), mt)
            return ir.prim(name, m)
        if k == 8 and self.helpers and d >= 2:
            h = r.choice(self.helpers)
            call = ir.Call(h.name, [self.arg(p.ty, d - 2) for p in h.params], h.ret)
            return call if h.ret is ty else self.fit(call, ty)
        return ir.Bin(r.choice(["wadd", "and", "or"]), a(), a(), ty)

    def fit(self, x, ty):
        """x (an integer) as ty: masked when narrowing."""
        if WIDTH[x.ty] > WIDTH[ty]:
            x = ir.Bin("and", x, ir.Const((1 << WIDTH[ty]) - 1, x.ty), x.ty)
        return x.cast(ty)

    def arg(self, ty, d):
        return self.p if ty is PTR else self.expr(ty, d)

    def index(self, limit):
        """A usize in [0, limit]: a random expression masked down."""
        m = 1
        while m * 2 - 1 <= limit:
            m *= 2
        return ir.Bin("and", self.expr(USIZE, 2), ir.Const(m - 1, USIZE), USIZE)

    # ---------------------------------------------------------------- statements
    def block(self, n, d, ret):
        for _ in range(n):
            self.stmt(d, ret)

    def stmt(self, d, ret):
        r, b = self.rng, self.b
        k = r.randrange(10)
        if k <= 2:
            ty = r.choice(INTS + [BOOL])
            self.bind(b.let("v", self.expr(ty, 3), ty), False)
        elif k == 3:
            ty = r.choice(INTS)
            self.bind(b.var("m", self.expr(ty, 2), ty), True)
        elif k == 4 and self.visible(mutable=True):
            v = r.choice(self.visible(mutable=True))
            b.set(v, self.expr(v.ty, 3))
        elif k == 5 and d > 0:
            with b.if_(self.expr(BOOL, 3)):
                self.scopes.append([])
                self.block(r.randrange(1, 4), d - 1, ret)
                if r.random() < 0.25:
                    b.ret(self.expr(ret, 2))
                self.scopes.pop()
            if r.random() < 0.5:
                with b.else_():
                    self.scopes.append([])
                    self.block(r.randrange(1, 3), d - 1, ret)
                    self.scopes.pop()
        elif k == 6 and d > 0 and self.loops < 3:
            self.loops += 1
            i = b.var("i", 0, USIZE)
            with b.while_(i < r.randrange(0, 6)):
                self.scopes.append([])
                self.block(r.randrange(1, 4), d - 1, ret)
                self.scopes.pop()
                b.set(i, i + 1)
        elif k == 7 and r.random() < 0.5 and self.helpers:
            # a call's result straight into a variable (the inlined result may be renamed)
            mut = self.visible(U64, mutable=True)
            v = r.choice(mut) if mut and r.random() < 0.5 else self.bind(b.var("cr", 0, U64), True)
            h = self.helpers[0]
            b.set(v, ir.Call(h.name, [self.arg(p.ty, 1) for p in h.params], h.ret))
            b.store64(self.out + self.index(BUF - 8), v)      # read: the assignment stays
        elif k == 7:
            b.store8(self.out + self.index(BUF - 1), self.expr(U8, 3))
        elif k == 8:
            b.store64(self.out + self.index(BUF - 8), self.expr(U64, 3))
        else:
            ty = r.choice(INTS)
            self.bind(b.let("w", self.expr(ty, 4), ty), False)


def _helper(rng, name, params, ret, noinline, assign):
    b = Builder(name, ret, noinline=noinline)
    g = Gen(rng, b, [])
    ps = [b.param(f"a{i}", t) for i, t in enumerate(params)]
    for p in ps:
        if p.ty is PTR:
            g.p = p
        else:
            g.bind(p, mutable=assign)
    if g.p is None:
        raise ValueError("helper needs a pointer parameter")
    g.out = None
    for _ in range(rng.randrange(1, 4)):
        ty = rng.choice(INTS)
        g.bind(b.let("h", g.expr(ty, 3), ty))
    if assign:
        for p in ps:
            if p.ty is not PTR and rng.random() < 0.6:
                b.set(p, g.expr(p.ty, 2))
    # early returns: at the top (inlined into a caller's loop, a loop exit there) and in a loop
    if rng.random() < 0.7:
        with b.if_(g.expr(BOOL, 2)):
            b.ret(g.expr(ret, 2))
    if rng.random() < 0.7:
        i = b.var("hi", 0, USIZE)
        with b.while_(i < rng.randrange(1, 5)):
            with b.if_(g.expr(BOOL, 2)):
                b.ret(g.expr(ret, 2))
            b.set(i, i + 1)
    b.ret(g.expr(ret, 3))
    return b.build()


def _program(rng, k):
    """t{k} and its two helpers."""
    hin = _helper(rng, f"t{k}_hin", [U64, U32, PTR, U8], U64, noinline=False, assign=True)
    hout = _helper(rng, f"t{k}_hout", [U64, U8, U32, USIZE, PTR, U64, U32], U32, noinline=True,
                   assign=rng.random() < 0.5)
    ret = rng.choice([U8, U32, U64, BOOL])
    b = Builder(f"t{k}", ret, public=True)
    g = Gen(rng, b, [hin, hout])
    g.p = b.param("p", PTR)
    g.out = b.param("out", MPTR)
    for name, ty in [("x", U64), ("y", U32), ("z", U8), ("w", USIZE), ("v", U64), ("u", U32)]:
        g.bind(b.param(name, ty))
    many = []
    for _ in range(rng.randrange(12, 22)):             # pressure: all live until the end
        ty = rng.choice(INTS)
        many.append(g.bind(b.let("k", g.expr(ty, 2), ty)))
    g.block(rng.randrange(4, 10), 2, ret)
    acc = ir.Const(0, U64)
    for v in many:
        acc = ir.Bin("wadd", acc, v.cast(U64) if v.ty is not U64 else v, U64)
    tail = g.expr(ret, 3)
    if ret is BOOL:
        b.ret(ir.lor(tail, ir.Bin("eq", acc, ir.Const(0x1234, U64), BOOL)))
    else:
        b.ret(ir.Bin("wadd", tail, g.fit(acc, ret), ret))
    return [hin, hout, b.build()]


def _module():
    rng = random.Random(SEED)
    fns = []
    for k in range(PROGRAMS):
        fns += _program(rng, k)
    return ir.Module("fuzz", fns, tables={"fz": bytes(rng.randrange(256) for _ in range(256))})


MODULE = _module()
MAINS = [f.name for f in MODULE.functions if f.public]


@pytest.fixture(scope="module")
def fuzzlib(tmp_path_factory):
    from conftest import _cc
    import shutil
    if not shutil.which("nasm"):
        pytest.skip("no nasm")
    cc = _cc()
    if not cc:
        pytest.skip("no C compiler")
    return test_asm._build(tmp_path_factory.mktemp("fuzz"), "z", MODULE.functions, cc, MODULE)


@pytest.mark.parametrize("name", MAINS)
def test_program(fuzzlib, name):
    rng = random.Random(f"{SEED}:{name}")
    for _ in range(25):
        c = test_asm.Case()
        p = c.region(bytes(rng.randrange(256) for _ in range(BUF)))
        out = c.region(bytes(BUF))
        scal = [rng.choice([0, 1, 0xFF, (1 << 64) - 1, 1 << 63, rng.randrange(1 << 64)]),
                rng.randrange(1 << 32), rng.randrange(256), rng.randrange(1 << 64),
                rng.randrange(1 << 64), rng.choice([0, (1 << 32) - 1, rng.randrange(1 << 32)])]
        test_asm._run(fuzzlib, MODULE, name, c, [test_asm._ptr(p, 0), test_asm._ptr(out, 0)] + scal)
