"""IR function -> machine IR (`mir.Ins` over virtual registers).

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
from .mir import PHYS, RSP, VECS, Imm, Ins, Lab, Mem, Reg, VReg, regs_in

# element-wise vector operations: one instruction each (legacy name; VEX adds "v")
VBIN = {"cmpeq": "pcmpeqb", "vor": "por", "mask_or": "por", "vand": "pand", "vsub8": "psubb",
        "vmin8": "pminub", "vadd64": "paddq", "vshuffle": "pshufb", "unpacklo8": "punpcklbw",
        "unpackhi8": "punpckhbw"}
SHIN = {"shin1": 15, "shin2": 14, "shin3": 13, "hshin1": 15, "hshin2": 14, "hshin3": 13}

RAX, RCX = PHYS["rax"], PHYS["rcx"]

# unsigned condition codes, and their negation
CC = {"lt": "b", "le": "be", "gt": "a", "ge": "ae", "eq": "e", "ne": "ne"}
NEG = {"b": "ae", "ae": "b", "be": "a", "a": "be", "e": "ne", "ne": "e"}
WIDTH = {ir.U8: 8, ir.U32: 32, ir.U64: 64, ir.USIZE: 64, ir.PTR: 64, ir.MPTR: 64, ir.BOOL: 64,
         ir.VISIT: 64}


def _imm32(v: int):
    """v (an unsigned value of up to 64 bits) as a sign-extended imm32, or None."""
    s = v - (1 << 64) if v >= (1 << 63) else v
    return s if -(1 << 31) <= s < (1 << 31) else None


POPCNT8 = bytes(bin(i).count("1") for i in range(256))   # (aligned 32 like every pool entry)


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

    def mov(self, dst, src, w: int = 64) -> None:
        """dst <- src; dst a register (VReg or Phys), src a register, Imm or Mem."""
        if getattr(dst, "cls", "g") == "v":
            self.emit("vmovdqa" if self.vex else "movdqa", [Reg(dst, self.vw), Reg(src, self.vw)],
                      defs=[dst], uses=[src])
            return
        srcop = Reg(src, w) if not isinstance(src, (Imm, Mem)) else src
        self.emit("mov", [Reg(dst, w), srcop], defs=[dst], uses=regs_in(srcop))

    # ---------------------------------------------------------------- vectors
    def vop(self, op: str, a, b=None, imm=None, w: int = None) -> VReg:
        """d = a `op` b: three operands with VEX (AVX2 functions), copy + two operands with
        legacy SSE. b is a register, a Mem or None; imm an immediate byte."""
        w = w or self.vw
        d = self.new("v")
        tail = ([Reg(b, w)] if isinstance(b, VReg) else [b] if b is not None else []) + \
               ([Imm(imm)] if imm is not None else [])
        busy = [b] if isinstance(b, VReg) else regs_in(b) if b is not None else []
        if self.vex:
            self.emit("v" + op, [Reg(d, w), Reg(a, w)] + tail, defs=[d], uses=[a] + busy)
        else:
            self.mov(d, a)
            self.emit(op, [Reg(d, w)] + tail, defs=[d], uses=[d] + busy)
        return d

    def vconst(self, data: bytes) -> VReg:
        d = self.new("v")
        lab = self.pool.get(bytes(data))
        self.emit("vmovdqa" if self.vex else "movdqa", [Reg(d, self.vw), Mem(sym=lab)], defs=[d])
        return d

    def vzero(self) -> VReg:
        d = self.new("v")
        if self.vex:
            self.emit("vpxor", [Reg(d, 128), Reg(d, 128), Reg(d, 128)], defs=[d])
        else:
            self.emit("pxor", [Reg(d, 128), Reg(d, 128)], defs=[d])
        return d

    def vflags(self, e: ir.Prim) -> str:
        """Set the flags for a vector test; returns the condition code that means true."""
        a = self.gen(e.args[0])
        if e.name == "any_nonzero":
            r = self.new()
            self.emit(self._v("pmovmskb"), [Reg(r, 32), Reg(a, self.vw)], defs=[r], uses=[a])
            self.emit("test", [Reg(r, 32), Reg(r, 32)], uses=[r])
            return "ne"
        if e.name == "is_zero":
            if self.vex:
                self.emit("vptest", [Reg(a, self.vw), Reg(a, self.vw)], uses=[a])
                return "e"
            t = self.vop("pcmpeqb", a, self.vzero())
            r = self.new()
            self.emit("pmovmskb", [Reg(r, 32), Reg(t, 128)], defs=[r], uses=[t])
            self.emit("cmp", [Reg(r, 32), Imm(0xFFFF)], uses=[r])
            return "e"
        raise Unsupported(e.name)

    def _v(self, op: str) -> str:
        return "v" + op if self.vex else op

    def copy(self, src) -> VReg:
        d = self.new()
        self.mov(d, src)
        return d

    def const(self, v: int) -> VReg:
        d = self.new()
        v &= (1 << 64) - 1
        if v == 0:
            self.emit("xor", [Reg(d, 32), Reg(d, 32)], defs=[d])
        elif v < (1 << 32):
            self.emit("mov", [Reg(d, 32), Imm(v)], defs=[d])
        elif _imm32(v) is not None:
            self.emit("mov", [Reg(d, 64), Imm(_imm32(v))], defs=[d])
        else:
            self.emit("mov", [Reg(d, 64), Imm(v)], defs=[d])
        return d

    def rhs(self, e: ir.Expr):
        """Right operand of a two-operand instruction: an imm32 when it fits, else a register."""
        if isinstance(e, ir.Const):
            s = _imm32(_const_value(e))
            if s is not None:
                return Imm(s)
        return Reg(self.gen(e), 64)

    def truncate(self, d: VReg, ty: ir.Ty) -> None:
        if ty is ir.U32:
            self.emit("trunc32", [Reg(d, 32), Reg(d, 32)], defs=[d], uses=[d])
        elif ty is ir.U8:
            self.emit("movzx", [Reg(d, 32), Reg(d, 8)], defs=[d], uses=[d])

    # ---------------------------------------------------------------- addresses
    def addr(self, e: ir.Expr, w: int = 0) -> Mem:
        """A pointer expression as [base + index + disp], folding constant offsets."""
        disp, idx = 0, []
        while True:
            if isinstance(e, ir.PtrAdd):
                if isinstance(e.n, ir.Const) and _imm32(disp + _const_value(e.n)) is not None:
                    disp += _const_value(e.n)
                else:
                    idx.append(e.n)
                e = e.p
            elif isinstance(e, ir.PtrSub) and isinstance(e.n, ir.Const) \
                    and _imm32(disp - _const_value(e.n)) is not None:
                disp -= _const_value(e.n)
                e = e.p
            else:
                break
        base = self.gen(e)
        index = None
        idx.reverse()           # inner offsets first, as the interpreter evaluates them
        if idx:
            index = self.gen(idx[0])
            for x in idx[1:]:
                t = self.new()
                self.emit("lea", [Reg(t, 64), Mem(index, self.gen(x))], defs=[t],
                          uses=[index] + [])
                # (lea t, [index + x]): uses recomputed below
                self.code[-1].uses = regs_in(self.code[-1].args[1])
                index = t
        return Mem(base=base, index=index, disp=disp, w=w)

    # ---------------------------------------------------------------- expressions
    def gen(self, e: ir.Expr) -> VReg:
        h = self.hoisted.get(id(e))
        if h is not None:
            return h
        # the same subexpression twice in one statement (the kernels repeat `load8(p + i)`):
        # once. Nothing is assigned inside a statement and nothing stores, so loads and mutable
        # variables are fine; a call might store, so it empties the cache
        key = self.key(e) if type(e) not in (ir.Var, ir.Const) and not self.foldable(e) else None
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

    def gen_uncached(self, e: ir.Expr) -> VReg:
        t = type(e)
        if t is ir.Var:
            return self.vars[e.name]
        if t is ir.Const:
            return self.const(_const_value(e))
        if t is ir.Cast:
            return self.gen(e.a)
        if t is ir.PtrAddr:
            return self.gen(e.p)
        if t in (ir.PtrAdd, ir.PtrSub):
            r = self.ptr_idiom(e)
            if r is not None:
                return r
        if t in (ir.PtrAdd, ir.PtrSub) and (t is ir.PtrAdd or isinstance(e.n, ir.Const)):
            m = self.addr(e)
            if m.index is None and m.disp == 0:
                return m.base
            d = self.new()
            self.emit("lea", [Reg(d, 64), m], defs=[d], uses=regs_in(m))
            return d
        if t is ir.PtrSub:
            d = self.copy(self.gen(e.p))
            s = self.rhs(e.n)
            self.emit("sub", [Reg(d, 64), s], defs=[d], uses=[d] + regs_in(s))
            return d
        if t is ir.PtrDiff:
            d = self.copy(self.gen(e.a))
            s = self.rhs(e.b)
            self.emit("sub", [Reg(d, 64), s], defs=[d], uses=[d] + regs_in(s))
            return d
        if t is ir.Load8:
            m = self.addr(e.p, 8)
            d = self.new()
            self.emit("movzx", [Reg(d, 32), m], defs=[d], uses=regs_in(m))
            return d
        if t is ir.Bin:
            return self.binop(e)
        if t is ir.Not:
            d = self.copy(self.gen(e.a))
            self.emit("xor", [Reg(d, 32), Imm(1)], defs=[d], uses=[d])
            return d
        if t is ir.Prim:
            return self.prim(e)
        if t is ir.Call:
            return self.call(e)
        if t is ir.CallTable:
            i = self.gen(e.idx)        # the interpreter's order: the index, then the arguments
            vals = [self.gen(a) for a in e.args]
            # a table of 32-bit offsets from its own start (in .text: no relocations)
            base, fn = self.new(), self.new()
            self.emit("lea", [Reg(base, 64), Mem(sym=f"nvol_fntbl_{e.table}")], defs=[base])
            self.emit("movsxd", [Reg(fn, 64), Mem(base, i, scale=4, w=32)], defs=[fn], uses=[base, i])
            self.emit("add", [Reg(fn, 64), Reg(base, 64)], defs=[fn], uses=[fn, base])
            g = self.mod.fn(self.mod.fn_tables[e.table][0])
            return self.call_out(fn, vals, g.ret)
        if t is ir.Visit:
            # cb points to {fn, ctx}: fn(ctx, at, mask), an int (non-zero = stop)
            cb, at, mask = self.gen(e.cb), self.gen(e.at), self.gen(e.mask)
            fn = self.new()
            self.emit("mov", [Reg(fn, 64), Mem(cb, w=64)], defs=[fn], uses=[cb])
            return self.call_out(fn, [Mem(cb, disp=8, w=64), at, mask], "int")
        if t is ir.TableLoad:
            d = self.new()
            sym = f"nvol_tbl_{e.table}"
            if isinstance(e.idx, ir.Const):
                self.emit("movzx", [Reg(d, 32), Mem(sym=sym, disp=_const_value(e.idx), w=8)], defs=[d])
                return d
            i = self.gen(e.idx)
            base = self.new()
            self.emit("lea", [Reg(base, 64), Mem(sym=sym)], defs=[base])
            self.emit("movzx", [Reg(d, 32), Mem(base, i, w=8)], defs=[d], uses=[base, i])
            return d
        raise Unsupported(f"{self.f.name}: {t.__name__}")

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

    def tail_call(self, e) -> bool:
        """`return f(...)` with f out of line, of the same return type, and every argument in a
        register: the epilogue and a jump to f, which returns to our caller (what GCC does at
        -O2): no call, no return through this function."""
        if not isinstance(e, ir.Call) or not self.mod:
            return False
        g = self.mod.fn(e.fn)
        if self.inlinable(g) or g.ret is not self.f.ret or len(e.args) > len(self.abi.args):
            return False
        vals = [self.gen(a) for a in e.args]
        uses = []
        for i, v in enumerate(vals):
            r = PHYS[self.abi.args[i]]
            self.mov(r, v)
            uses.append(r)
        if self.vex:
            self.emit("vzeroupper", [])
        t = _target(g)
        self.emit("tailcall", [t], uses=uses)
        self.code[-1].hint = "near" if isinstance(t, Lab) else None
        return True

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

    def call_out(self, target, vals: list, ret) -> VReg:
        """A call by the ABI: arguments in registers, then on the stack above the shadow space;
        every caller-saved register destroyed (all vector registers in a VEX function: the
        callee keeps only the low 128 bits of xmm6-15). `ret` "int" is a C int used as a bool."""
        n = len(self.abi.args)
        uses = []
        for i, v in enumerate(vals):
            if i < n:
                r = PHYS[self.abi.args[i]]
                self.mov(r, v)
                uses.append(r)
            else:
                if not isinstance(v, VReg):
                    v = self.copy(v)
                m = Mem(RSP, disp=self.abi.shadow + 8 * (i - n), w=64)
                self.emit("mov", [m, Reg(v, 64)], uses=[v])
        if self.vex:
            self.emit("vzeroupper", [])
        clob = self.abi.caller_saved()
        if self.vex:
            clob = [r for r in clob if r.cls == "g"] + [PHYS[v] for v in VECS]
        if isinstance(target, Lab):
            self.emit("call", [target], defs=[RAX], uses=uses, clobbers=clob)
        elif isinstance(target, Mem):         # through an ifunc's pointer
            self.emit("callind", [target], defs=[RAX], uses=uses, clobbers=clob)
        else:
            self.emit("callind", [Reg(target, 64)], defs=[RAX], uses=uses + [target], clobbers=clob)
        d = self.new()
        if ret == "int":
            self.emit("test", [Reg(RAX, 32), Reg(RAX, 32)], uses=[RAX])
            self.emit("setne", [Reg(d, 8)], defs=[d])
            self.emit("movzx", [Reg(d, 32), Reg(d, 8)], defs=[d], uses=[d])
        elif ret in (ir.U8, ir.BOOL):           # the upper bits of a narrow result are undefined
            self.emit("movzx", [Reg(d, 32), Reg(RAX, 8)], defs=[d], uses=[RAX])
        elif ret is ir.U32:
            self.emit("mov", [Reg(d, 32), Reg(RAX, 32)], defs=[d], uses=[RAX])
        elif ret is ir.VOID:
            return None
        else:
            self.mov(d, RAX)
        return d

    def ptr_idiom(self, e):
        """The kernels' alignment arithmetic, simplified as GCC and Clang do:
        p + (V - (addr(p) & (V-1)))  ->  (p & -V) + V      (next V-aligned address after p)
        p - (addr(p) & m)            ->  p & ~m            (round down)
        a - (k - (b - a))            ->  b - k"""
        n = e.n
        if isinstance(e, ir.PtrAdd) and isinstance(n, ir.Bin) and n.op == "sub" \
                and isinstance(n.a, ir.Const) and _masked_addr(n.b, e.p) == _const_value(n.a) - 1 \
                and _const_value(n.a) & (_const_value(n.a) - 1) == 0:
            d = self.copy(self.gen(e.p))
            self.emit("and", [Reg(d, 64), Imm(-_const_value(n.a))], defs=[d], uses=[d])
            self.emit("add", [Reg(d, 64), Imm(_const_value(n.a))], defs=[d], uses=[d])
            return d
        if isinstance(e, ir.PtrSub) and _masked_addr(n, e.p) is not None:
            d = self.copy(self.gen(e.p))
            self.emit("and", [Reg(d, 64), Imm(~_masked_addr(n, e.p))], defs=[d], uses=[d])
            return d
        if isinstance(e, ir.PtrSub) and isinstance(n, ir.Bin) and n.op == "sub" \
                and isinstance(n.a, ir.Const) and isinstance(n.b, ir.PtrDiff) and _same(n.b.b, e.p):
            return self.gen(ir.PtrSub(n.b.a, n.a))
        return None

    def binop(self, e: ir.Bin) -> VReg:
        op = e.op
        if op in CC or op in ("land", "lor"):
            return self.bool_value(e)
        if op == "add":
            a = self.gen(e.a)
            s = self.rhs(e.b)
            d = self.new()
            m = Mem(a, disp=s.v) if isinstance(s, Imm) else Mem(a, s.r)
            self.emit("lea", [Reg(d, 64), m], defs=[d], uses=regs_in(m))
            return d
        if op == "sub":
            a = self.gen(e.a)
            s = self.rhs(e.b)
            d = self.new()
            if isinstance(s, Imm) and _imm32(-s.v) is not None:
                self.emit("lea", [Reg(d, 64), Mem(a, disp=-s.v)], defs=[d], uses=[a])
            else:
                self.mov(d, a)
                self.emit("sub", [Reg(d, 64), s], defs=[d], uses=[d] + regs_in(s))
            return d
        if op in ("and", "or"):
            d = self.copy(self.gen(e.a))
            s = self.rhs(e.b)
            self.emit(op, [Reg(d, 64), s], defs=[d], uses=[d] + regs_in(s))
            return d
        # wrapping u8/u32 operations run in 32-bit registers: the low 32 bits of the result
        # depend only on the low 32 bits of the operands, and the write zero-extends, so a
        # u32 result needs no truncation (a u8 one still does). A shift count is below the
        # width (the interpreter's UB otherwise), so the 5-bit count mask of a 32-bit shift
        # never matters
        w = 32 if e.ty in (ir.U8, ir.U32) else 64
        if op in ("shl", "shr"):
            ins = "shl" if op == "shl" else "shr"
            ow = w if op == "shl" else 64
            if isinstance(e.b, ir.Const):
                d = self.copy(self.gen(e.a))
                self.emit(ins, [Reg(d, ow), Imm(_const_value(e.b))], defs=[d], uses=[d])
            else:
                a = self.gen(e.a)          # the interpreter's order: left operand first
                n = self.gen(e.b)
                self.mov(RCX, n)
                d = self.copy(a)
                self.emit(ins, [Reg(d, ow), Reg(RCX, 8)], defs=[d], uses=[d, RCX])
            if op == "shl" and e.ty is ir.U8:
                self.truncate(d, e.ty)
            return d
        if op in ("wadd", "wsub", "wmul"):
            d = self.copy(self.gen(e.a))
            if op == "wmul":
                s = Reg(self.gen(e.b), w)
                self.emit("imul", [Reg(d, w), s], defs=[d], uses=[d, s.r])
            else:
                s = self.rhs(e.b)
                if isinstance(s, Reg):
                    s = Reg(s.r, w)
                self.emit("add" if op == "wadd" else "sub", [Reg(d, w), s], defs=[d],
                          uses=[d] + regs_in(s))
            if e.ty is ir.U8 or w == 64:
                self.truncate(d, e.ty)
            return d
        if op in ("max", "min"):
            d = self.copy(self.gen(e.a))
            b = self.gen(e.b)
            self.emit("cmp", [Reg(d, 64), Reg(b, 64)], uses=[d, b])
            self.emit("cmovb" if op == "max" else "cmova", [Reg(d, 64), Reg(b, 64)],
                      defs=[d], uses=[d, b])
            return d
        raise Unsupported(f"{self.f.name}: Bin {op}")

    def bool_value(self, e: ir.Expr) -> VReg:
        if isinstance(e, ir.Bin) and e.op in CC:
            a = self.gen(e.a)
            s = self.rhs(e.b)
            d = self.new()
            self.emit("cmp", [Reg(a, 64), s], uses=[a] + regs_in(s))
            self.emit("set" + CC[e.op], [Reg(d, 8)], defs=[d])
            self.emit("movzx", [Reg(d, 32), Reg(d, 8)], defs=[d], uses=[d])
            return d
        d = self.new()
        end = self.label()
        self.emit("xor", [Reg(d, 32), Reg(d, 32)], defs=[d])
        self.branch(e, end, False)
        self.emit("mov", [Reg(d, 32), Imm(1)], defs=[d])
        self.place(end)
        return d

    def prim(self, e: ir.Prim) -> VReg:
        n = e.name
        if n in ("load16u", "load32u", "load64"):
            w = {"load16u": 16, "load32u": 32, "load64": 64}[n]
            m = self.addr(e.args[0], w)
            d = self.new()
            if w == 16:
                self.emit("movzx", [Reg(d, 32), m], defs=[d], uses=regs_in(m))
            else:
                self.emit("mov", [Reg(d, w), m], defs=[d], uses=regs_in(m))
            return d
        if n in ("first_offset", "first_offset64", "last_offset"):
            a = self.gen(e.args[0])
            d = self.new()
            w = 64 if n == "first_offset64" else 32
            self.emit("bsf" if n != "last_offset" else "bsr", [Reg(d, w), Reg(a, w)],
                      defs=[d], uses=[a])
            return d
        if n == "popcnt":
            x = e.args[0]
            bits16 = isinstance(x, ir.Prim) and x.name == "movemask" and self.vw == 128
            return self.popcnt(self.gen(x), 16 if bits16 else 32)
        if n.startswith("cpu_"):            # the C glue's cached CPU checks
            return self.call_out(Lab(f"nvol_{n}"), [], "int")
        if n in ("is_zero", "any_nonzero"):
            cc = self.vflags(e)
            d = self.new()
            self.emit("set" + cc, [Reg(d, 8)], defs=[d])
            self.emit("movzx", [Reg(d, 32), Reg(d, 8)], defs=[d], uses=[d])
            return d
        return self.vprim(e)

    def popcnt(self, a: VReg, bits: int = 32) -> VReg:
        """POPCNT ships with every CPU that has SSE4.2 or AVX2. Below that, a 256-entry byte
        table (what libgcc's __popcountdi2 does): two lookups for a 16-bit mask (pmovmskb of a
        128-bit vector), four for 32 bits; 7 or 13 instructions against 16 for the SWAR count."""
        d = self.new()
        if any(i and i.name in ("sse42", "avx2") for i in (self.isa, self.outer)):
            self.emit("popcnt", [Reg(d, 32), Reg(a, 32)], defs=[d], uses=[a])
            return d
        tbl = self.new()
        self.emit("lea", [Reg(tbl, 64), Mem(sym=self.pool.get(POPCNT8))], defs=[tbl])
        first = True
        for k in range(bits // 8):
            idx = self.new()
            if k == 0:
                self.emit("movzx", [Reg(idx, 32), Reg(a, 8)], defs=[idx], uses=[a])
            else:
                self.emit("mov", [Reg(idx, 32), Reg(a, 32)], defs=[idx], uses=[a])
                self.emit("shr", [Reg(idx, 32), Imm(8 * k)], defs=[idx], uses=[idx])
                if k < bits // 8 - 1:
                    self.emit("movzx", [Reg(idx, 32), Reg(idx, 8)], defs=[idx], uses=[idx])
            if first:
                self.emit("movzx", [Reg(d, 32), Mem(tbl, idx, w=8)], defs=[d], uses=[tbl, idx])
                first = False
            else:
                t = self.new()
                self.emit("movzx", [Reg(t, 32), Mem(tbl, idx, w=8)], defs=[t], uses=[tbl, idx])
                self.emit("add", [Reg(d, 32), Reg(t, 32)], defs=[d], uses=[d, t])
        return d

    def popcnt_swar(self, a: VReg) -> VReg:
        """The SWAR count (what GCC emits for __builtin_popcount without -mpopcnt)."""
        d = self.new()
        t = self.new()
        steps = [("mov", d, a), ("shr", d, 1), ("and", d, 0x55555555), ("mov", t, a),
                 ("sub", t, d),                                     # t = x - ((x >> 1) & 0x55..)
                 ("mov", d, t), ("shr", d, 2), ("and", d, 0x33333333),
                 ("and", t, 0x33333333), ("add", t, d),             # t = pairs summed
                 ("mov", d, t), ("shr", d, 4), ("add", d, t), ("and", d, 0x0F0F0F0F),
                 ("imul", d, 0x01010101), ("shr", d, 24)]
        for op, x, y in steps:
            if op == "mov":
                self.emit("mov", [Reg(x, 32), Reg(y, 32)], defs=[x], uses=[y])
            elif op == "imul":
                self.emit("imul", [Reg(x, 32), Reg(x, 32), Imm(y)], defs=[x], uses=[x])
            elif isinstance(y, int):
                self.emit(op, [Reg(x, 32), Imm(y)], defs=[x], uses=[x])
            else:
                self.emit(op, [Reg(x, 32), Reg(y, 32)], defs=[x], uses=[x, y])
        return d

    def vprim(self, e: ir.Prim) -> VReg:
        n, isa = e.name, self.isa
        if isa is None:
            raise Unsupported(f"{self.f.name}: vector primitive {n} outside an ISA function")
        V = isa.bytes
        if n == "splat":
            x = e.args[0]
            if isinstance(x, ir.Const):
                return self.vconst(bytes([_const_value(x) & 0xFF]) * V)
            r = self.gen(x)
            d = self.new("v")
            if self.vex:
                self.emit("vmovd", [Reg(d, 128), Reg(r, 32)], defs=[d], uses=[r])
                self.emit("vpbroadcastb", [Reg(d, self.vw), Reg(d, 128)], defs=[d], uses=[d])
            else:
                t = self.new()
                self.emit("imul", [Reg(t, 32), Reg(r, 32), Imm(0x01010101)], defs=[t], uses=[r])
                self.emit("movd", [Reg(d, 128), Reg(t, 32)], defs=[d], uses=[t])
                self.emit("pshufd", [Reg(d, 128), Reg(d, 128), Imm(0)], defs=[d], uses=[d])
            return d
        if n == "vzero":
            return self.vzero()
        if n in ("load_a", "load_u"):
            m = self.addr(e.args[0])
            d = self.new("v")
            op = ("vmovdqa" if self.vex else "movdqa") if n == "load_a" else \
                 ("vmovdqu" if self.vex else "movdqu")
            self.emit(op, [Reg(d, self.vw), m], defs=[d], uses=regs_in(m))
            return d
        if n == "load_half_bcast":
            m = self.addr(e.args[0], 128)       # a 128-bit load into a 256-bit register
            d = self.new("v")
            self.emit("vbroadcasti128", [Reg(d, 256), m], defs=[d], uses=regs_in(m))
            return d
        if n in VBIN:
            a = self.gen(e.args[0])
            b = self.gen(e.args[1])
            return self.vop(VBIN[n], a, b)
        if n == "movemask":
            a = self.gen(e.args[0])
            d = self.new()
            self.emit(self._v("pmovmskb"), [Reg(d, 32), Reg(a, self.vw)], defs=[d], uses=[a])
            return d
        if n == "sad0":
            a = self.gen(e.args[0])
            return self.vop("psadbw", a, self.vzero())
        if n == "vshr4":
            return self.vop("psrlw", self.gen(e.args[0]), imm=4)
        if n == "vnib_hi":
            t = self.vop("psrlw", self.gen(e.args[0]), imm=4)
            return self.vop("pand", t, self.vconst(b"\x0f" * V))
        if n in ("shin1", "shin2", "shin3"):
            a, b = self.gen(e.args[0]), self.gen(e.args[1])
            if self.wide:     # alignr(a, permute2x128(b, a, 0x21), k): across the 128-bit lanes
                t = self.new("v")
                self.emit("vperm2i128", [Reg(t, 256), Reg(b, 256), Reg(a, 256), Imm(0x21)],
                          defs=[t], uses=[b, a])
                return self.vop("palignr", a, t, imm=SHIN[n])
            return self.vop("palignr", a, b, imm=SHIN[n])
        if n in ("hshin1", "hshin2", "hshin3"):
            return self.vop("palignr", self.gen(e.args[0]), self.gen(e.args[1]), imm=SHIN[n])
        if n == "swap_halves":
            a = self.gen(e.args[0])
            d = self.new("v")
            self.emit("vpermq", [Reg(d, 256), Reg(a, 256), Imm(0x4E)], defs=[d], uses=[a])
            return d
        if n.startswith("lane64_"):
            return self.lane64(self.gen(e.args[0]), int(n[-1]))
        if n == "hsum64":
            a = self.gen(e.args[0])
            x = a
            if self.wide:
                hi = self.new("v")
                self.emit("vextracti128", [Reg(hi, 128), Reg(a, 256), Imm(1)], defs=[hi], uses=[a])
                x = self.vop("paddq", hi, a, w=128)
            lo = self.lane64(x, 0, w=128)
            hi64 = self.lane64(x, 1, w=128)
            d = self.new()
            self.emit("lea", [Reg(d, 64), Mem(lo, hi64)], defs=[d], uses=[lo, hi64])
            return d
        if n == "skip_set16":
            a, b = self.gen(e.args[0]), self.gen(e.args[1])
            self.emit(self._v("pcmpistri"), [Reg(a, 128), Reg(b, 128), Imm(0x10)],
                      defs=[RCX], uses=[a, b])
            d = self.new()
            self.emit("mov", [Reg(d, 32), Reg(RCX, 32)], defs=[d], uses=[RCX])
            return d
        raise Unsupported(f"{self.f.name}: primitive {n}")

    def lane64(self, a: VReg, k: int, w: int = None) -> VReg:
        """64-bit lane k of a vector (lanes 2 and 3 only in 256-bit vectors)."""
        d = self.new()
        src = a
        if k >= 2:
            src = self.new("v")
            self.emit("vextracti128", [Reg(src, 128), Reg(a, 256), Imm(1)], defs=[src], uses=[a])
            k -= 2
        if k == 0:
            self.emit(self._v("movq"), [Reg(d, 64), Reg(src, 128)], defs=[d], uses=[src])
        elif self.vex:
            self.emit("vpextrq", [Reg(d, 64), Reg(src, 128), Imm(1)], defs=[d], uses=[src])
        else:            # SSE2: no pextrq (SSE4.1); move the high half down first
            t = self.vop("punpckhqdq", src, src, w=128)
            self.emit("movq", [Reg(d, 64), Reg(t, 128)], defs=[d], uses=[t])
        return d

    # ---------------------------------------------------------------- conditions
    def branch(self, e: ir.Expr, target: Lab, when: bool) -> None:
        """Jump to target when bool(e) == when; fall through otherwise."""
        if isinstance(e, ir.Bin) and e.op in ("eq", "ne") and e.a.ty is ir.BOOL                 and isinstance(e.b, ir.Const):
            # `b == False` (`is_zero(m).eq(False)`): a branch on b itself, on its flags
            same = (e.op == "eq") == bool(_const_value(e.b))
            self.branch(e.a, target, when if same else not when)
            return
        bit = _bit_test(e)
        if bit is not None:
            # (x & (1 << n)) != 0 -> bt x, n (CF = the bit), as GCC does
            x, n, is_set = bit
            a, b = self.gen(x), self.gen(n)
            w = 64 if x.ty in (ir.U64, ir.USIZE) else 32
            self.emit("bt", [Reg(a, w), Reg(b, w)], uses=[a, b])
            self.emit("jb" if is_set == when else "jae", [target])
            return
        if isinstance(e, ir.Bin) and e.op in CC:
            a = self.gen(e.a)
            s = self.rhs(e.b)
            w = _cmp_width(e, s)
            if isinstance(s, Imm) and s.v == 0 and e.op in ("eq", "ne"):
                self.emit("test", [Reg(a, w), Reg(a, w)], uses=[a])
            else:
                self.emit("cmp", [Reg(a, w), Reg(s.r, w) if isinstance(s, Reg) else s],
                          uses=[a] + regs_in(s))
            cc = CC[e.op] if when else NEG[CC[e.op]]
            self.emit("j" + cc, [target])
            return
        if isinstance(e, ir.Bin) and e.op in ("land", "lor"):
            # the second operand runs only on one path: its values are not reused after it
            if e.op == "land" and when:
                skip = self.label()
                self.branch(e.a, skip, False)
                kept = dict(self.cse)
                self.branch(e.b, target, True)
                self.cse = kept
                self.place(skip)
            elif e.op == "land":
                self.branch(e.a, target, False)
                kept = dict(self.cse)
                self.branch(e.b, target, False)
                self.cse = kept
            elif when:
                self.branch(e.a, target, True)
                kept = dict(self.cse)
                self.branch(e.b, target, True)
                self.cse = kept
            else:
                skip = self.label()
                self.branch(e.a, skip, True)
                kept = dict(self.cse)
                self.branch(e.b, target, False)
                self.cse = kept
                self.place(skip)
            return
        if isinstance(e, ir.Not):
            self.branch(e.a, target, not when)
            return
        if isinstance(e, ir.Prim) and e.name in ("is_zero", "any_nonzero"):
            cc = self.vflags(e)
            self.emit("j" + (cc if when else NEG[cc]), [target])
            return
        if isinstance(e, ir.Const):
            if bool(_const_value(e)) == when:
                self.emit("jmp", [target])
            return
        v = self.gen(e)
        w = 32 if e.ty in (ir.BOOL, ir.U8, ir.U32) else 64
        self.emit("test", [Reg(v, w), Reg(v, w)], uses=[v])
        self.emit("jne" if when else "je", [target])

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
        w = self.vw
        for i in range(0, len(run) - 1, 2):
            self.cse = {}
            x0 = self.gen(run[i].value.args[1])
            x1 = self.gen(run[i + 1].value.args[1])
            t = self.vop("paddb", x0, x1)
            if self.vex:
                self.emit("vpsubb", [Reg(dst, w), Reg(dst, w), Reg(t, w)], defs=[dst], uses=[dst, t])
            else:
                self.emit("psubb", [Reg(dst, w), Reg(t, w)], defs=[dst], uses=[dst, t])
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
                self.mov(RAX, self.gen(s.value))
            self.emit("jmp", [self.ret_label])
        elif isinstance(s, ir.Store8):
            self.epoch += 1
            m = self.addr(s.ptr, 8)
            if isinstance(s.value, ir.Const):
                self.emit("mov", [m, Imm(_const_value(s.value) & 0xFF)], uses=regs_in(m))
            else:
                v = self.gen(s.value)
                self.emit("mov", [m, Reg(v, 8)], uses=regs_in(m) + [v])
        elif isinstance(s, ir.StoreVec):
            self.epoch += 1
            m = self.addr(s.ptr)
            v = self.gen(s.value)
            self.emit("vmovdqu" if self.vex else "movdqu", [m, Reg(v, self.vw)],
                      uses=regs_in(m) + [v])
        elif isinstance(s, ir.Store64):
            self.epoch += 1
            m = self.addr(s.ptr, 64)
            v = self.rhs(s.value)
            self.emit("mov", [m, v], uses=regs_in(m) + regs_in(v))
        else:
            raise Unsupported(f"{self.f.name}: {type(s).__name__}")

    # ---------------------------------------------------------------- function
    def run(self) -> list:
        f = self.f
        if f.ifunc:
            raise Unsupported(f"{f.name}: ifunc")
        if f.ret is ir.VEC or any(p.ty is ir.VEC for p in f.params):
            raise Unsupported(f"{f.name}: vectors are passed only to inlined functions")
        for i, p in enumerate(f.params):
            d = self.new()
            self.vars[p.name] = d
            r = self.abi.arg_reg(i)
            src = Reg(r, 64) if r is not None else Mem(arg=i, w=64)
            # the C ABI leaves the upper bits of a narrow argument undefined: zero-extend
            if p.ty is ir.U8 or p.ty is ir.BOOL:
                src = Reg(r, 8) if r is not None else Mem(arg=i, w=8)
                self.emit("movzx", [Reg(d, 32), src], defs=[d], uses=regs_in(src))
            elif p.ty is ir.U32:
                src = Reg(r, 32) if r is not None else Mem(arg=i, w=32)
                self.emit("mov", [Reg(d, 32), src], defs=[d], uses=regs_in(src))
            else:
                self.emit("mov", [Reg(d, 64), src], defs=[d], uses=regs_in(src))
        self.block(f.body)
        self.place(self.ret_label)
        if self.vex:     # leave the upper halves clean: no AVX-SSE transition in the caller
            self.emit("vzeroupper", [])
        self.emit("ret", [], uses=[RAX] if f.ret is not ir.VOID else [])
        return _fold_loads(self.code, self.vex)


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


# vector operations whose last source may be a memory operand (VEX: any alignment; legacy SSE:
# 16-byte aligned only), and those whose two sources commute
_FOLD = {"pcmpeqb", "por", "pand", "psubb", "pminub", "paddq", "pshufb", "punpcklbw",
         "punpckhbw", "psadbw", "pxor"}
_COMMUTE = {"pcmpeqb", "por", "pand", "pminub", "paddq", "pxor"}
_LOADS = {"movdqa", "movdqu", "vmovdqa", "vmovdqu"}


def _fold_loads(code: list, vex: bool) -> list:
    """A vector load used once, by the next operation that can take it as a memory operand in
    the same block, becomes that operand (`vpcmpeqb ymm1, ymm0, [rdx]`, as GCC and Clang emit):
    one instruction less. Nothing in between may write memory, call, branch, or redefine the
    address registers. A copy of the loaded value (`movdqa d, L` before a two-operand SSE op)
    becomes the load itself."""
    count = {}
    for ins in code:
        for r in ins.uses:
            if isinstance(r, VReg):
                count[r] = count.get(r, 0) + 1
    drop = set()
    for i, ld in enumerate(code):
        if ld.op not in _LOADS or ld.hint or len(ld.args) != 2 or not isinstance(ld.args[0], Reg) \
                or not isinstance(ld.args[1], Mem) or ld.args[1].spill is not None \
                or ld.args[1].arg is not None:
            continue
        lv, mem = ld.args[0].r, ld.args[1]
        if not isinstance(lv, VReg) or count.get(lv) != 1:
            continue
        addr = set(regs_in(mem))
        for j in range(i + 1, len(code)):
            ins = code[j]
            if lv in ins.uses:
                break
            if ins.op == "label" or ins.op in ("call", "callind", "ret", "tailjmp", "jmp", "tailcall") \
                    or ins.op.startswith("j") or (ins.args and isinstance(ins.args[0], Mem)) \
                    or addr & set(ins.defs):
                j = None
                break
        else:
            j = None
        if j is None:
            continue
        use = code[j]
        if any(isinstance(a, Mem) for a in use.args):     # x86 takes one memory operand
            continue
        base = use.op[1:] if vex and use.op.startswith("v") else use.op
        regs = [k for k, a in enumerate(use.args) if isinstance(a, Reg) and a.r is lv]
        if len(regs) != 1:
            continue
        k = regs[0]
        srcs = [n for n, a in enumerate(use.args) if not isinstance(a, Imm)]
        aligned = ld.op in ("movdqa", "vmovdqa")
        if use.op in _LOADS and k == 1 and len(use.args) == 2:            # a copy: load directly
            use.op = ld.op
        elif base in _FOLD and (vex or aligned):
            last = srcs[-1]
            if k != last:
                if base not in _COMMUTE or not vex or k != srcs[-2]:
                    continue
                use.args[k], use.args[last] = use.args[last], use.args[k]
                k = last
        else:
            continue
        use.args[k] = Mem(mem.base, mem.index, mem.scale, mem.disp, mem.sym, 0)
        use.uses = [r for r in use.uses if r is not lv] + regs_in(mem)
        drop.add(i)
    return [ins for n, ins in enumerate(code) if n not in drop]


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
    return Lower(f, abi, pool, mod).run()
