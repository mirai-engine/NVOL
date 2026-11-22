"""Shared machinery for source-printing backends."""
from __future__ import annotations

from .. import ir
from ..isa import ISAS, lowering

BINOPS = {"add": "+", "sub": "-", "and": "&", "or": "|", "xor": "^", "shr": ">>", "shl": "<<",
          "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!=",
          "land": "&&", "lor": "||"}


class Printer:
    lang: str = ""
    indent_unit = "    "

    def __init__(self, module: ir.Module):
        self.mod = module
        self.isa = None
        self.out: list = []
        self.depth = 0

    # -- helpers ------------------------------------------------------------
    def line(self, s: str = "") -> None:
        self.out.append((self.indent_unit * self.depth + s) if s else "")

    def text(self) -> str:
        return "\n".join(self.out) + "\n"

    # -- to be provided by languages ----------------------------------------
    def ty(self, t: ir.Ty) -> str: ...
    def const(self, c: ir.Const) -> str: ...
    def ptr_add(self, p: str, n: str) -> str: ...
    def ptr_sub(self, p: str, n: str) -> str: ...
    def ptr_diff(self, a: str, b: str) -> str: ...
    def ptr_addr(self, p: str) -> str: ...
    def load8(self, p: str) -> str: ...
    def cast(self, x: str, t: ir.Ty) -> str: ...
    def special_bin(self, op: str, a: str, b: str, ty) -> str: ...
    def table_load(self, table: str, idx: str) -> str: ...

    def call_table(self, table: str, idx: str, args: list) -> str: ...

    def call(self, fn: str, args: list) -> str:
        return f"{fn}({', '.join(args)})"

    # -- expressions --------------------------------------------------------
    @staticmethod
    def aligned(x):
        """("up", p, m) for `p + (m+1 - (addr(p) & m))` and ("down", p, m) for
        `p - (addr(p) & m)`, m + 1 a power of two (the kernels' vector alignment); else None.
        A backend may build those from the address (`(addr | m) + 1`, `addr & ~m`): as
        pointer arithmetic on p, LLVM ties the aligned pointer to p and LSR keeps two
        induction variables in the loop that walks it (Odin: T2 in docs/TOOLCHAINS.md; Clang
        on the C: P5 in docs/PARITY.md)."""
        def addr_and(e, p):
            if (type(e) is ir.Bin and e.op == "and" and type(e.a) is ir.PtrAddr and e.a.p is p
                    and type(e.b) is ir.Const and e.b.value is not None):
                return e.b.value
            return None
        n = x.n
        if type(x) is ir.PtrAdd and type(n) is ir.Bin and n.op == "sub" and type(n.a) is ir.Const:
            m = addr_and(n.b, x.p)
            if m is not None and n.a.value == m + 1 and m & (m + 1) == 0:
                return ("up", x.p, m)
        if type(x) is ir.PtrSub:
            m = addr_and(n, x.p)
            if m is not None and m & (m + 1) == 0:
                return ("down", x.p, m)
        return None

    def e(self, x) -> str:
        t = type(x)
        if t is ir.Var:
            return x.name
        if t is ir.Const:
            return self.const(x)
        if t is ir.Bin:
            if x.op in ("wadd", "wsub", "wmul", "max", "min"):
                return self.special_bin(x.op, self.e(x.a), self.e(x.b), x.ty)
            return f"({self.e(x.a)} {BINOPS[x.op]} {self.e(x.b)})"
        if t is ir.TableLoad:
            return self.table_load(x.table, self.e(x.idx))
        if t is ir.Not:
            return f"(!{self.e(x.a)})"
        if t is ir.Cast:
            return self.cast(self.e(x.a), x.ty)
        if t is ir.PtrAdd:
            return self.ptr_add(self.e(x.p), self.e(x.n))
        if t is ir.PtrSub:
            return self.ptr_sub(self.e(x.p), self.e(x.n))
        if t is ir.PtrDiff:
            return self.ptr_diff(self.e(x.a), self.e(x.b))
        if t is ir.PtrAddr:
            return self.ptr_addr(self.e(x.p))
        if t is ir.Load8:
            return self.load8(self.e(x.p))
        if t is ir.Prim:
            tpl = lowering(self.isa, self.lang, x.name)
            return tpl.format(*[self.e(a) for a in x.args])
        if t is ir.Call:
            return self.call(x.fn, [self.e(a) for a in x.args])
        if t is ir.CallTable:
            return self.call_table(x.table, self.e(x.idx), [self.e(a) for a in x.args])
        if t is ir.Visit:
            return self.visit(self.e(x.cb), self.e(x.at), self.e(x.mask))
        raise TypeError(x)

    def visit(self, cb: str, at: str, mask: str) -> str: ...

    def tables_used(self, f: ir.Function) -> list:
        found = []

        def ex(e):
            if isinstance(e, ir.CallTable) and e.table not in found:
                found.append(e.table)
            for c in ir._expr_children(e):
                ex(c)

        def st(ss):
            for s in ss:
                for v in vars(s).values():
                    if isinstance(v, ir.Expr):
                        ex(v)
                    elif isinstance(v, list):
                        st([x for x in v if isinstance(x, ir.Stmt)])
        st(f.body)
        return found

    def isa_of(self, f: ir.Function):
        return ISAS[f.isa] if f.isa else None
