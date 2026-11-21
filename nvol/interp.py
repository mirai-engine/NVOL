"""Reference interpreter: executes IR with the semantics in `nvol.primitives`.

This is the semantic engine. It is slow, strict and paranoid:
  * every pointer that is *formed* must stay within [base, base+size]
    (one-past-the-end allowed) -- catches C/Rust UB in pointer arithmetic,
  * every load must be in bounds and aligned loads must be aligned,
  * unsigned arithmetic that underflows is an error (kernels must not rely on
    wrap-around), primitive preconditions are asserted.

A kernel that passes the interpreter against the oracle is algorithmically
correct for the simulated addresses; backends are then checked differentially.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import ir
from .isa import ISAS
from .primitives import PRIMS

WIDTH = {ir.U8: 8, ir.U32: 32, ir.U64: 64, ir.USIZE: 64, ir.PTR: 64, ir.MPTR: 64, ir.BOOL: 1}


class UB(AssertionError):
    pass


class Memory:
    """One or more disjoint read-only regions (haystack, needle, finder state)."""

    def __init__(self, data: bytes, base: int):
        self.regions = []
        self.add(data, base)

    def add(self, data: bytes, base: int) -> int:
        for b, d in self.regions:
            assert base + len(data) < b or b + len(d) < base, "regions must not touch"
        self.regions.append((base, bytearray(data)))
        return base

    def check_ptr(self, p: int) -> int:
        for b, d in self.regions:
            if b <= p <= b + len(d):
                return p
        raise UB(f"pointer {p:#x} outside every region")

    def read(self, p: int, n: int) -> bytes:
        for b, d in self.regions:
            if b <= p and p + n <= b + len(d):
                return d[p - b:p - b + n]
        raise UB(f"read of {n} bytes at {p:#x} out of bounds")

    def write(self, p: int, data: bytes) -> None:
        for b, d in self.regions:
            if b <= p and p + len(data) <= b + len(d):
                d[p - b:p - b + len(data)] = data
                return
        raise UB(f"write of {len(data)} bytes at {p:#x} out of bounds")

    def region(self, base: int) -> bytes:
        return bytes(next(d for b, d in self.regions if b == base))


@dataclass
class Ctx:
    nbytes: int
    mem: Memory
    cpu: dict = field(default_factory=dict)
    isa: Optional[str] = None


class _Ret(Exception):
    def __init__(self, v):
        self.v = v


class Interpreter:
    def __init__(self, module: ir.Module, mem: Memory, cpu=None):
        self.mod, self.mem, self.cpu = module, mem, cpu or {}
        self.steps = 0

    def call(self, fname: str, args: list):
        f = self.mod.fn(fname)
        if f.ifunc:
            ctx0 = Ctx(0, self.mem, self.cpu)
            for cond, target in f.ifunc:
                if cond is None or PRIMS[cond.name].ref(ctx0):
                    return self.call(target, args)
            raise UB(f"{fname}: no ifunc target applies")
        nbytes = ISAS[f.isa].bytes if f.isa else 0
        ctx = Ctx(nbytes, self.mem, self.cpu, f.isa)
        env = {p.name: a for p, a in zip(f.params, args)}
        try:
            self._block(f.body, env, ctx)
        except _Ret as r:
            return r.v
        if f.ret is ir.VOID:
            return None
        raise UB(f"{fname}: fell off the end without returning")

    # ---------------------------------------------------------------- stmts
    def _block(self, stmts, env, ctx):
        for s in stmts:
            self.steps += 1
            if isinstance(s, (ir.Let, ir.Assign)):
                env[s.var.name] = self._e(s.value, env, ctx)
            elif isinstance(s, ir.While):
                while self._e(s.cond, env, ctx):
                    self._block(s.body, env, ctx)
            elif isinstance(s, ir.If):
                if self._e(s.cond, env, ctx):
                    self._block(s.then, env, ctx)
                elif s.els:
                    self._block(s.els, env, ctx)
            elif isinstance(s, ir.Return):
                raise _Ret(None if s.value is None else self._e(s.value, env, ctx))
            elif isinstance(s, ir.Store8):
                v = self._e(s.value, env, ctx)
                if not 0 <= v < 256:
                    raise UB(f"Store8 of {v}")
                self.mem.write(self._e(s.ptr, env, ctx), bytes([v]))
            elif isinstance(s, ir.StoreVec):
                v = self._e(s.value, env, ctx)
                assert len(v) == ctx.nbytes
                self.mem.write(self._e(s.ptr, env, ctx), bytes(v))
            elif isinstance(s, ir.Store64):
                v = self._e(s.value, env, ctx)
                self.mem.write(self._e(s.ptr, env, ctx), (v & ((1 << 64) - 1)).to_bytes(8, "little"))
            else:
                raise TypeError(s)

    # ---------------------------------------------------------------- exprs
    def _e(self, e, env, ctx):
        t = type(e)
        if t is ir.Var:
            return env[e.name]
        if t is ir.Const:
            if e.value is None:
                return 0 if e.ty is ir.PTR else (1 << WIDTH[e.ty]) - 1
            return e.value
        if t is ir.Bin:
            if e.op == "land":
                return bool(self._e(e.a, env, ctx)) and bool(self._e(e.b, env, ctx))
            if e.op == "lor":
                return bool(self._e(e.a, env, ctx)) or bool(self._e(e.b, env, ctx))
            a, b = self._e(e.a, env, ctx), self._e(e.b, env, ctx)
            op = e.op
            if op == "add":
                r = a + b
                if r >> WIDTH[e.ty]:
                    raise UB(f"{e.ty} overflow {a}+{b}")
                return r
            if op == "sub":
                if b > a:
                    raise UB(f"{e.ty} underflow {a}-{b}")
                return a - b
            if op == "wadd":
                return (a + b) & ((1 << WIDTH[e.ty]) - 1)
            if op == "wsub":
                return (a - b) & ((1 << WIDTH[e.ty]) - 1)
            if op == "wmul":
                return (a * b) & ((1 << WIDTH[e.ty]) - 1)
            if op == "shl":
                if b >= WIDTH[e.ty]:
                    raise UB(f"shift by {b} >= width of {e.ty}")
                return (a << b) & ((1 << WIDTH[e.ty]) - 1)
            if op == "max":
                return max(a, b)
            if op == "min":
                return min(a, b)
            if op == "and":
                return a & b
            if op == "or":
                return a | b
            if op == "shr":
                if b >= WIDTH[e.ty]:
                    raise UB(f"shift by {b} >= width of {e.ty}")
                return a >> b
            return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b,
                    "eq": a == b, "ne": a != b}[op]
        if t is ir.Not:
            return not self._e(e.a, env, ctx)
        if t is ir.Cast:
            v = self._e(e.a, env, ctx)
            if v >> WIDTH[e.ty]:
                raise UB(f"lossy cast of {v} to {e.ty}")
            return v
        if t is ir.PtrAdd:
            return self.mem.check_ptr(self._e(e.p, env, ctx) + self._e(e.n, env, ctx))
        if t is ir.PtrSub:
            return self.mem.check_ptr(self._e(e.p, env, ctx) - self._e(e.n, env, ctx))
        if t is ir.PtrDiff:
            a, b = self._e(e.a, env, ctx), self._e(e.b, env, ctx)
            if a < b:
                raise UB("negative pointer difference")
            return a - b
        if t is ir.PtrAddr:
            return self._e(e.p, env, ctx)
        if t is ir.TableLoad:
            i = self._e(e.idx, env, ctx)
            tbl = self.mod.tables[e.table]
            if not 0 <= i < len(tbl):
                raise UB(f"table {e.table} index {i} out of range")
            return tbl[i]
        if t is ir.Load8:
            return self.mem.read(self._e(e.p, env, ctx), 1)[0]
        if t is ir.Prim:
            args = [self._e(a, env, ctx) for a in e.args]
            return PRIMS[e.name].ref(ctx, *args)
        if t is ir.Call:
            return self.call(e.fn, [self._e(a, env, ctx) for a in e.args])
        if t is ir.Visit:
            cb = self._e(e.cb, env, ctx)
            return bool(cb(self._e(e.at, env, ctx), self._e(e.mask, env, ctx)))
        if t is ir.CallTable:
            fns = self.mod.fn_tables[e.table]
            i = self._e(e.idx, env, ctx)
            if not 0 <= i < len(fns):
                raise UB(f"function table {e.table} index {i} out of range")
            return self.call(fns[i], [self._e(a, env, ctx) for a in e.args])
        raise TypeError(e)
