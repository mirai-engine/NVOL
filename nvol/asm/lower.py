"""IR function -> machine IR (`mir.Ins` over virtual registers): the part every target shares.

`Lower` holds the inlining, the CSE (per statement, and across statements by variable versions
and a memory epoch), the statement and loop structure, and the cold and rare markers the
allocator reads. A target subclass selects the instructions: `x86.X86Lower` today. The MIR's
`jmp` and conditional jumps (`jb`, `jne`, ..., always with the label first) are generic, so
liveness, blocks and the cold layout in `regalloc` are shared too.

Value representation: every integer, pointer and bool lives zero-extended in a 64-bit register.
The IR makes that cheap: a `Cast` must be lossless (the interpreter treats a lossy one as UB),
plain `add`/`sub` never wrap, and every comparison is unsigned. So casts cost nothing, all
arithmetic runs at 64 bits, and only the wrapping operations (`wadd`, `wsub`, `wmul`, `shl`) on
u8/u32 truncate their result.

Control flow: `While` is rotated (the test at the bottom, one branch per iteration), and a
condition is lowered straight into compare-and-branch (`land`/`lor` short-circuit, `Not` flips
the branch) instead of materializing a bool.
"""
from __future__ import annotations

from .. import ir
from ..isa import ISAS
from .abi import ABI
from .mir import Imm, Ins, Lab, Mem, Reg, VReg


# unsigned condition codes, and their negation
CC = {"lt": "b", "le": "be", "gt": "a", "ge": "ae", "eq": "e", "ne": "ne"}
NEG = {"b": "ae", "ae": "b", "be": "a", "a": "be", "e": "ne", "ne": "e"}
WIDTH = {ir.U8: 8, ir.U32: 32, ir.U64: 64, ir.USIZE: 64, ir.PTR: 64, ir.MPTR: 64, ir.BOOL: 64,
         ir.VISIT: 64}


def _imm32(v: int):
    """v (an unsigned value of up to 64 bits) as a sign-extended imm32, or None."""
    s = v - (1 << 64) if v >= (1 << 63) else v
    return s if -(1 << 31) <= s < (1 << 31) else None


def _const_value(c: ir.Const) -> int:
    if c.value is None:
        return 0 if c.ty in (ir.PTR, ir.MPTR) else (1 << (8 if c.ty is ir.U8 else
                                                            32 if c.ty is ir.U32 else 64)) - 1
    return int(c.value)


class Unsupported(NotImplementedError):
    pass


class Pool:
    """Constant vectors of a module (`.rodata`), one label per distinct value."""

    def __init__(self):
        self.consts: dict = {}          # bytes -> label

    def get(self, data: bytes) -> str:
        if data not in self.consts:
            self.consts[data] = f"NVOL_K{len(self.consts)}"
        return self.consts[data]


class Lower:
    """The target-neutral lowering: inlining, CSE, the statement and loop structure, the cold
    and rare markers. A target subclass selects the instructions (`x86.X86Lower`)."""


    def __init__(self, f: ir.Function, abi: ABI, pool: Pool = None, mod: ir.Module = None):
        self.f, self.abi = f, abi
        self.pool = pool if pool is not None else Pool()
        self.mod = mod
        self.isa = ISAS[f.isa] if f.isa else None
        self.outer = self.isa
        # VEX encoding for every vector instruction of an AVX2 function, also in the 128-bit
        # kernels it inlines (no AVX-SSE transitions); the vector width is the current kernel's
        self.vex = bool(self.isa and self.isa.bytes == 32)
        self.targets = {t for ts in mod.fn_tables.values() for t in ts} if mod else set()
        self.ret_to = None          # (result vreg, end label) while lowering an inlined body
        self.hoisted = {}           # id(expr) -> vreg: loop-invariant parts of While conditions
        self.loops = 0              # While nesting at the statement being lowered
        self.slots = []             # one entry per open While (an exit inside one is cold)
        self.cse = {}               # structural key -> vreg, within one statement
        # across statements: the value of an immutable let, in the scope of the block that
        # defines it (it dominates what follows there). A key holds each variable's version
        # (bumped by every assignment) and, for a load, the memory epoch (bumped by every
        # store and call), so a match is the same value
        self.dom = {}
        self.ver = {}
        self.epoch = 0
        self.uses = ir._uses(f.body, {})    # variable name -> reads in the function
        self.depth = 0
        self.code: list = []
        self.nv = 0
        self.nl = 0
        self.vars: dict = {}
        self.ret_label = self.label()

    @property
    def vw(self) -> int:
        return self.isa.bytes * 8 if self.isa else 128

    @property
    def wide(self) -> bool:
        return bool(self.isa and self.isa.bytes == 32)

    # ---------------------------------------------------------------- helpers
    def new(self, cls: str = "g") -> VReg:
        self.nv += 1
        return VReg(cls, self.nv)

    def label(self) -> Lab:
        self.nl += 1
        return Lab(f".L{self.nl}")

    def emit(self, op, args, defs=(), uses=(), clobbers=()) -> Ins:
        i = Ins(op, list(args), list(defs), list(uses), list(clobbers))
        self.code.append(i)
        return i

    def place(self, lab: Lab) -> None:
        self.emit("label", [lab])

    def copy(self, src) -> VReg:
        d = self.new()
        self.mov(d, src)
        return d

    # ---------------------------------------------------------------- expressions
    def gen(self, e: ir.Expr) -> VReg:
        h = self.hoisted.get(id(e))
        if h is not None:
            return h
        # the same subexpression twice in one statement (the kernels repeat `load8(p + i)`):
        # once. Nothing is assigned inside a statement and nothing stores, so loads and mutable
        # variables are fine; a call might store, so it empties the cache. A foldable vector
        # load is shared too: folded into two operations it would still be two loads (two
        # µops on the load ports); shared, it is one `vmovdqu`, and a load used once still
        # folds (`_fold_loads`)
        key = self.key(e) if type(e) not in (ir.Var, ir.Const) else None
        if key is not None and key in self.cse:
            return self.cse[key]
        if key is not None and key in self.dom:
            return self.dom[key]
        v = self.gen_uncached(e)
        if type(e) in (ir.Call, ir.CallTable, ir.Visit) or \
                (type(e) is ir.Prim and e.name.startswith("cpu_")):
            self.cse = {}
            self.epoch += 1
        elif key is not None and v is not None:
            self.cse[key] = v
        return v

    def foldable(self, e) -> bool:
        """A vector load the next operation can take as a memory operand (VEX: any; SSE: the
        aligned ones): repeating it costs nothing, sharing it costs a register and a load."""
        return type(e) is ir.Prim and (e.name == "load_a" or (self.vex and e.name == "load_u"))

    def key(self, e):
        """A structural key for e, or None when e may have effects (calls)."""
        t = type(e)
        if t is ir.Var:
            r = self.vars[e.name]
            return ("V", id(r), self.ver.get(id(r), 0))
        if t is ir.Const:
            return ("C", e.value, e.ty.name)
        if t in (ir.Call, ir.CallTable, ir.Visit) or (t is ir.Prim and e.name.startswith("cpu_")):
            return None
        kids = []
        for c in ir._expr_children(e):
            k = self.key(c)
            if k is None:
                return None
            kids.append(k)
        tag = getattr(e, "op", None) or getattr(e, "name", None) or getattr(e, "table", None)
        mem = self.epoch if t is ir.Load8 or (t is ir.Prim and e.name.startswith("load")) else None
        return (t.__name__, tag, e.ty.name, tuple(kids), mem)

    # ---------------------------------------------------------------- calls
    def inlinable(self, g: ir.Function) -> bool:
        """The C backend's rule: what C emits as NVOL_INLINE is inlined, the rest is called."""
        return not (g.ifunc or g.noinline or g.public or "_entry_" in g.name
                    or g.name in self.targets)

    def call(self, e: ir.Call) -> VReg:
        g = self.mod.fn(e.fn) if self.mod else None
        if g is None:
            raise Unsupported(f"{self.f.name}: call to {e.fn} without its module")
        if self.inlinable(g):
            return self.inline(g, e.args)
        return self.call_out(_target(g), [self.gen(a) for a in e.args], g.ret)

    def inline(self, g: ir.Function, args: list, tail: bool = False) -> VReg:
        """g's body in place: its own variables, each Return an assignment to the result and a
        jump to the end. A parameter the body never assigns is the argument's register itself.

        tail (`return g(...)`): g's returns are ours (to our result, or out of the function,
        or a tail call), with no result register live across g's whole body."""
        if self.depth > 16:
            raise Unsupported(f"{self.f.name}: inlining {g.name} too deep")
        if g.isa and ISAS[g.isa].bytes == 32 and not self.vex:
            # (the C rule never does this: an AVX2 kernel inlines only into AVX2 functions)
            raise Unsupported(f"{self.f.name}: inlining the AVX2 kernel {g.name} into a non-AVX2 function")
        vals = [self.gen(a) for a in args]
        assigned = _assigned(g.body)
        scope = {}
        for p, v in zip(g.params, vals):
            if p.name in assigned:
                d = self.new("v" if p.ty is ir.VEC else "g")
                self.mov(d, v)
                v = d
            scope[p.name] = v
        saved = (self.vars, self.isa, self.ret_to)
        self.vars = scope
        if g.isa:
            self.isa = ISAS[g.isa]
        if tail:
            self.depth += 1
            self.block(g.body)
            self.depth -= 1
            self.vars, self.isa, self.ret_to = saved
            return None
        res = None if g.ret is ir.VOID else self.new("v" if g.ret is ir.VEC else "g")
        end = self.label()
        self.ret_to = (res, end)
        self.depth += 1
        self.block(g.body)
        self.depth -= 1
        self.place(end)
        self.vars, self.isa, self.ret_to = saved
        return res

    # ---------------------------------------------------------------- statements
    def block(self, stmts: list) -> None:
        saved = self.dom
        self.dom = dict(saved)
        try:
            self._block(stmts)
        finally:
            self.dom = saved

    def _block(self, stmts: list) -> None:
        k = 0
        while k < len(stmts):
            s = stmts[k]
            nxt = stmts[k + 1] if k + 1 < len(stmts) else None
            if isinstance(s, ir.Let) and not s.mut and s.var.ty is ir.BOOL \
                    and isinstance(nxt, ir.If) and _is_var(nxt.cond, s.var) \
                    and self.uses.get(s.var.name, 0) == 1 and _pure(s.value):
                # `let none = is_zero(m); if none: ...`: branch on the flags (vptest, cmp)
                # instead of materializing a bool and testing it
                self.stmt(ir.If(s.value, nxt.then, nxt.els))
                k += 2
                continue
            run = 1
            while _acc_sub(s) and k + run < len(stmts) and _acc_sub(stmts[k + run])                     and stmts[k + run].var.name == s.var.name:
                run += 1
            if run >= 2:
                self._acc_pairs(stmts[k:k + run])
                k += run
                continue
            self.stmt(s)
            k += 1

    def _acc_pairs(self, run: list) -> None:
        """`acc = vsub8(acc, x0); acc = vsub8(acc, x1); ...` (a counting accumulator) as
        `acc -= x0 + x1` per pair: half the dependent subtractions on acc per iteration, as
        GCC reassociates it (u8 lanes wrap, so the sum regroups exactly)."""
        dst = self.vars[run[0].var.name]
        for i in range(0, len(run) - 1, 2):
            self.cse = {}
            x0 = self.gen(run[i].value.args[1])
            x1 = self.gen(run[i + 1].value.args[1])
            self.sub8_into(dst, self.add8(x0, x1))
        self.ver[id(dst)] = self.ver.get(id(dst), 0) + 1
        if len(run) % 2:
            self.stmt(run[-1])

    def _into(self, dst: VReg, value: ir.Expr) -> None:
        """dst <- value. When value is computed into a fresh temporary, the temporary is renamed
        to dst instead of copied (`cur = cur + 1` becomes `lea cur, [cur + 1]`), unless dst is
        read after the temporary is first written: `x = y - x` (mov t, y; sub t, x) must keep
        the copy."""
        start, nv0 = len(self.code), self.nv
        v = self.gen(value)
        if v.id <= nv0 or v is dst:
            self.mov(dst, v)
            return
        seq = self.code[start:]
        k = next((i for i, ins in enumerate(seq) if v in ins.defs), None)
        if k is None or any(dst in ins.uses for ins in seq[k + 1:]):
            self.mov(dst, v)
            return
        for ins in seq[k:]:
            ins.defs = [dst if r is v else r for r in ins.defs]
            ins.uses = [dst if r is v else r for r in ins.uses]
            ins.args = [_rename(a, v, dst) for a in ins.args]
        # the chain before it too: `h = (h << 1) + b` computes `mov t, h; shl t, 1; ...;
        # mov h, t; add h, b`. A fresh t copied into dst, that nothing reads afterwards, is dst
        # itself when dst is neither read nor written between t's first write and the copy:
        # `shl h, 1; ...; add h, b`
        while True:
            for j, ins in enumerate(seq):
                if ins.op not in ("mov", "movdqa", "vmovdqa") or len(ins.args) != 2 \
                        or not all(isinstance(a, Reg) for a in ins.args) \
                        or ins.args[0].r is not dst or ins.args[0].w != ins.args[1].w \
                        or (ins.op == "mov" and ins.args[0].w != 64):
                    continue
                t = ins.args[1].r
                if not isinstance(t, VReg) or t.id <= nv0 or t is dst:
                    continue
                k2 = next((i for i, x in enumerate(seq) if t in x.defs), None)
                if k2 is None or k2 >= j \
                        or any(dst in x.uses or dst in x.defs for x in seq[k2 + 1:j]) \
                        or any(t in x.uses or t in x.defs for x in seq[j + 1:]):
                    continue
                for x in seq[k2:j + 1]:
                    x.defs = [dst if r is t else r for r in x.defs]
                    x.uses = [dst if r is t else r for r in x.uses]
                    x.args = [_rename(a, t, dst) for a in x.args]
                break
            else:
                break

    def stmt(self, s) -> None:
        self.cse = {}
        if isinstance(s, ir.Let):
            nv0 = self.nv
            k = None
            if not s.mut and type(s.value) not in (ir.Var, ir.Const) and not self.foldable(s.value):
                k = self.key(s.value)
            v = self.gen(s.value)
            if v.id > nv0:                      # a fresh temporary: it is the variable
                self.vars[s.var.name] = v
            else:
                d = self.new(v.cls)
                self.vars[s.var.name] = d
                self.mov(d, v)
            r = self.vars[s.var.name]
            # (vectors not: a value kept across statements holds one of 16 registers)
            if k is not None and r.cls == "g" and k[-1] in (None, self.epoch):
                self.dom[k] = r
        elif isinstance(s, ir.Assign):
            dst = self.vars[s.var.name]
            self._into(dst, s.value)
            self.ver[id(dst)] = self.ver.get(id(dst), 0) + 1
        elif isinstance(s, ir.While):
            # the next iteration runs after the body: what it assigns or stores is new at the
            # top too
            for n in _assigned(s.body):
                if n in self.vars:
                    r = self.vars[n]
                    self.ver[id(r)] = self.ver.get(id(r), 0) + 1
            if _effects(s.body) or _effects_expr(s.cond):
                self.epoch += 1
            # the condition's arithmetic on values the body never assigns (`end - 128`) is
            # computed once, before the loop, instead of at every test
            assigned, hoist = _assigned(s.body), []

            def walk(e):
                if type(e) not in (ir.Var, ir.Const) and e.ty is not ir.BOOL \
                        and _invariant(e, assigned):
                    hoist.append(e)
                    return
                for c in ir._expr_children(e):
                    walk(c)
            walk(s.cond)
            for e in hoist:
                self.hoisted[id(e)] = self.gen(e)
            # inverted: the condition tested once on entry (to the exit when false) and at the
            # bottom of the body, with no label in between, so the last arithmetic of the body
            # can set the flags the test needs (_peephole fuses them)
            body, done = self.label(), self.label()
            self.branch(s.cond, done, False)
            # the loop head (the back edge's target) 16-aligned when that costs at most 10 bytes
            # of padding, else 8-aligned, as GCC does
            self.emit("align", [Imm(16), Imm(10), Imm(0)])
            self.place(body)
            self.slots.append(1)
            self.block(s.body)
            self.slots.pop()
            self.cse = {}               # (the entry test's values are not the bottom's)
            self.branch(s.cond, body, True)
            self.place(done)
            self.cse = {}
            for e in hoist:
                del self.hoisted[id(e)]
        elif isinstance(s, ir.If) and self.slots and not s.els and s.then \
                and isinstance(s.then[-1], ir.Return):
            # an exit from a loop (a kernel's "found"): taken once. It is lowered in place (the
            # register allocator sees the natural order) and marked; after allocation it moves
            # to the end of the function with its branch inverted, so the loop runs straight
            # with one taken branch per iteration, as GCC and Clang lay it out
            skip, cold = self.label(), self.label()
            self.branch(s.cond, skip, False)
            self.emit("coldbegin", [cold])
            self.place(cold)
            self.block(s.then)
            self.emit("coldend", [])
            self.place(skip)
        elif isinstance(s, ir.If):
            els, end = self.label(), self.label()
            self.branch(s.cond, els, False)
            # a static frequency for the allocator: an arm inside a loop that loops or returns
            # itself (a search kernel's candidate verification) runs rarely
            rare = bool(self.slots) and not s.els and _loops_or_returns(s.then)
            if rare:
                self.emit("rarebegin", [])
            self.block(s.then)
            if rare:
                self.emit("rareend", [])
            if s.els:
                if not (s.then and isinstance(s.then[-1], ir.Return)):
                    self.emit("jmp", [end])
                self.place(els)
                self.block(s.els)
                self.place(end)
            else:
                self.place(els)
        elif isinstance(s, ir.Return):
            if isinstance(s.value, ir.Call) and self.mod:
                g = self.mod.fn(s.value.fn)
                if g is not None and self.inlinable(g) and g.ret is not ir.VOID:
                    self.inline(g, s.value.args, tail=True)
                    return
            if self.ret_to is not None:
                res, end = self.ret_to
                if s.value is not None:
                    self._into(res, s.value)
                self.emit("jmp", [end])
                return
            if self.tail_call(s.value):
                return
            if s.value is not None:
                self.ret_value(self.gen(s.value))
            self.emit("jmp", [self.ret_label])
        elif isinstance(s, ir.Store8):
            self.epoch += 1
            self.store8(s)
        elif isinstance(s, ir.StoreVec):
            self.epoch += 1
            self.store_vec(s)
        elif isinstance(s, ir.Store64):
            self.epoch += 1
            self.store64(s)
        else:
            raise Unsupported(f"{self.f.name}: {type(s).__name__}")

def _rename(a, old: VReg, new: VReg):
    if isinstance(a, Reg) and a.r is old:
        return Reg(new, a.w)
    if isinstance(a, Mem) and (a.base is old or a.index is old):
        return Mem(new if a.base is old else a.base, new if a.index is old else a.index,
                   a.scale, a.disp, a.sym, a.w, a.arg, a.spill)
    return a


def _cmp_width(e: ir.Bin, s) -> int:
    """32 when both operands of a comparison fit in 32 bits (they live zero-extended, so the
    32-bit compare is the same and one byte shorter), else 64."""
    small = (ir.BOOL, ir.U8, ir.U32)
    if e.a.ty in small and e.b.ty in small and (not isinstance(s, Imm) or 0 <= s.v < (1 << 31)):
        return 32
    return 64


def _target(g: ir.Function):
    """Where a call to g goes: its symbol, or for a dispatched function (ifunc) its pointer,
    `call [rel ptr]`, as the C wrapper inlined (one jump less than through the stub)."""
    if g.ifunc and not (len(g.ifunc) == 1 and g.ifunc[0][0] is None):
        return Mem(sym=f"{g.name}_ptr", w=64)
    return Lab(g.name)


def _bit_test(e):
    """(x, n, true when the bit is set) for `(x & (1 << n)) != 0` / `== 0`, else None."""
    if not (isinstance(e, ir.Bin) and e.op in ("eq", "ne") and isinstance(e.b, ir.Const)
            and _const_value(e.b) == 0 and isinstance(e.a, ir.Bin) and e.a.op == "and"):
        return None
    for x, m in ((e.a.a, e.a.b), (e.a.b, e.a.a)):
        if isinstance(m, ir.Bin) and m.op == "shl" and isinstance(m.a, ir.Const) \
                and _const_value(m.a) == 1:
            return x, m.b, e.op == "ne"
    return None


def _is_var(e, v) -> bool:
    return isinstance(e, ir.Var) and e.name == v.name


def _pure(e) -> bool:
    """No calls (the expression moves to the next statement's condition)."""
    if isinstance(e, (ir.Call, ir.CallTable, ir.Visit)) or \
            (isinstance(e, ir.Prim) and e.name.startswith("cpu_")):
        return False
    return all(_pure(c) for c in ir._expr_children(e))


def _same(a, b) -> bool:
    """The same variable (an expression the backend may evaluate once for both)."""
    return isinstance(a, ir.Var) and isinstance(b, ir.Var) and a.name == b.name


def _masked_addr(e, p):
    """m when e is `addr(p) & m` (m + 1 a power of two), else None."""
    if isinstance(e, ir.Bin) and e.op == "and" and isinstance(e.b, ir.Const) \
            and isinstance(e.a, ir.PtrAddr) and _same(e.a.p, p):
        m = _const_value(e.b)
        if m & (m + 1) == 0:
            return m
    return None


def _loops_or_returns(body) -> bool:
    for st in body:
        if isinstance(st, (ir.While, ir.Return)):
            return True
        if isinstance(st, ir.If) and (_loops_or_returns(st.then) or _loops_or_returns(st.els or [])):
            return True
    return False


def _invariant(e, assigned: set) -> bool:
    """Pure arithmetic on constants and on variables a loop body never assigns."""
    t = type(e)
    if t is ir.Var:
        return e.name not in assigned
    if t is ir.Const:
        return True
    if t in (ir.Bin, ir.PtrAdd, ir.PtrSub, ir.PtrDiff, ir.PtrAddr, ir.Cast, ir.Not):
        return all(_invariant(c, assigned) for c in ir._expr_children(e))
    return False            # loads, primitives, calls, visits, tables: evaluated in the loop


def _effects_expr(e) -> bool:
    if type(e) in (ir.Call, ir.CallTable, ir.Visit):
        return True
    return any(_effects_expr(c) for c in ir._expr_children(e))


def _effects(stmts: list) -> bool:
    """A store or a call anywhere in stmts (memory may change)."""
    for st in stmts:
        if isinstance(st, (ir.Store8, ir.StoreVec, ir.Store64)):
            return True
        if isinstance(st, ir.While) and (_effects(st.body) or _effects_expr(st.cond)):
            return True
        if isinstance(st, ir.If) and (_effects(st.then) or _effects(st.els or [])
                                      or _effects_expr(st.cond)):
            return True
        for a in ("value", "cond", "ptr"):
            x = getattr(st, a, None)
            if isinstance(x, ir.Expr) and _effects_expr(x):
                return True
    return False


def _mentions(e, name: str) -> bool:
    if isinstance(e, ir.Var):
        return e.name == name
    return any(_mentions(c, name) for c in ir._expr_children(e))


def _acc_sub(s) -> bool:
    """`acc = vsub8(acc, x)` with x not reading acc."""
    return isinstance(s, ir.Assign) and isinstance(s.value, ir.Prim) and s.value.name == "vsub8"         and _is_var(s.value.args[0], s.var) and not _mentions(s.value.args[1], s.var.name)


def _assigned(stmts: list) -> set:
    out = set()
    for s in stmts:
        if isinstance(s, ir.Assign):
            out.add(s.var.name)
        elif isinstance(s, ir.While):
            out |= _assigned(s.body)
        elif isinstance(s, ir.If):
            out |= _assigned(s.then) | _assigned(s.els or [])
    return out


def lower(f: ir.Function, abi: ABI, pool: Pool = None, mod: ir.Module = None) -> list:
    from .x86 import X86Lower
    return X86Lower(f, abi, pool, mod).run()
