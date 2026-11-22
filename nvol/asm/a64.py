"""A64 (aarch64, NEON, AAPCS64): instruction selection on top of the target-neutral lowering of
`lower.py`, and the parts of register allocation that name A64 instructions. `regalloc` takes
this module as its target `T`; `gas_a64.py` prints the result.

The machine IR keeps the shared conventions: destination first, `jmp` and the generic
conditional jumps (`jb`, `jae`, `jbe`, `ja`, `je`, `jne`, label first), which the printer spells
`b` and `b.lo`/`b.hs`/`b.ls`/`b.hi`/`b.eq`/`b.ne`. A64 adds `cbz`/`cbnz` (label, register) and
`tbz`/`tbnz` (label, register, bit). Every instruction is 4 bytes, so there is no jump sizing:
`check_ranges` only proves every branch reaches.

Values live zero-extended in 64-bit registers, as on x86: a write to a w register clears the
upper half. Vector operations are three-operand. Every vector register is destroyed by a call:
AAPCS64 keeps only the low 64 bits of v8-v15, so no 128-bit value lives across one."""
from __future__ import annotations

from .. import ir
from .abi import ABI
from .lower import CC, NEG, Lower, Unsupported, _bit_test, _const_value, _masked_addr, _same, _target
from .mir import (CONDJUMPS, PHYS, SP, TERMINATORS, XZR, Imm, Ins, Lab, Mem, Phys, Reg, VReg,
                  regs_in)
from .regalloc import _subst, copy_back, jump_cleanup, reloads

X0 = PHYS["x0"]
# the generic condition codes as A64 spells them
COND = {"b": "lo", "ae": "hs", "be": "ls", "a": "hi", "e": "eq", "ne": "ne", "s": "mi", "ns": "pl"}
JCC = {"j" + k: v for k, v in COND.items()}

# element-wise vector operations: one instruction each, on 16 byte lanes
VBIN = {"cmpeq": "vcmeq", "vor": "vorr", "mask_or": "vorr", "vand": "vand", "vxor": "veor", "vandnot": "vbic", "vsub8": "vsub",
        "vmin8": "vumin"}
SHIN = {"shin1": 15, "shin2": 14, "shin3": 13}


def imm12(v: int):
    """v as an add/sub immediate: 0..4095, optionally shifted left by 12; or None."""
    if 0 <= v < 4096:
        return v
    if v > 0 and v % 4096 == 0 and v >> 12 < 4096:
        return v
    return None


def logical_imm(v: int, w: int) -> bool:
    """Whether v is a bitmask immediate of a w-bit logical instruction (and, orr, eor, tst): a
    rotated run of ones in an element of 2, 4, ..., w bits repeated across the register."""
    v &= (1 << w) - 1
    if v in (0, (1 << w) - 1):
        return False
    size = 2
    while size <= w:
        mask = (1 << size) - 1
        e = v & mask
        if all((v >> (k * size)) & mask == e for k in range(w // size)):
            for r in range(size):
                rot = ((e >> r) | (e << (size - r))) & mask
                if rot and (rot & (rot + 1)) == 0:
                    return True
            return False
        size *= 2
    return False


def mov_parts(v: int, w: int) -> list:
    """The instructions that put v in a w-bit register: [("orr", v)], or movz/movn then movk
    per 16-bit part, whichever is shortest."""
    v &= (1 << w) - 1
    if logical_imm(v, w):
        return [("orr", v)]
    parts = [(v >> s) & 0xFFFF for s in range(0, w, 16)]
    nz = [k for k, p in enumerate(parts) if p]
    nf = [k for k, p in enumerate(parts) if p != 0xFFFF]
    if len(nf) < len(nz):
        if not nf:
            return [("movn", 0, 0)]
        k0 = nf[0]
        out = [("movn", (~parts[k0]) & 0xFFFF, 16 * k0)]
        return out + [("movk", parts[k], 16 * k) for k in nf[1:]]
    if not nz:
        return [("movz", 0, 0)]
    out = [("movz", parts[nz[0]], 16 * nz[0])]
    return out + [("movk", parts[k], 16 * k) for k in nz[1:]]


def mem_ok(m: Mem, size: int) -> bool:
    """Whether [base, #disp] or [base, index] encodes for an access of `size` bytes: unsigned
    offsets scaled by the size, or any -256..255 (the unscaled ldur/stur forms)."""
    if m.sym is not None or m.arg is not None or m.spill is not None:
        return True
    if m.index is not None:
        return m.disp == 0
    d = m.disp
    return -256 <= d < 256 or (d >= 0 and d % size == 0 and d // size < 4096)


class A64Lower(Lower):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.vex = False

    # ---------------------------------------------------------------- moves and constants
    def mov(self, dst, src, w: int = 64) -> None:
        """dst <- src; dst a register, src a register, Imm or Mem."""
        if getattr(dst, "cls", "g") == "v":
            if isinstance(src, Mem):
                self.emit("ldr", [Reg(dst, 128), src], defs=[dst], uses=regs_in(src))
            else:
                self.emit("vmov", [Reg(dst, 128), Reg(src, 128)], defs=[dst], uses=[src])
            return
        if isinstance(src, Imm):
            self.emit("movimm", [Reg(dst, w), Imm(src.v)], defs=[dst])
        elif isinstance(src, Mem):
            self.emit("ldr", [Reg(dst, w), src], defs=[dst], uses=regs_in(src))
        else:
            self.emit("mov", [Reg(dst, w), Reg(src, w)], defs=[dst], uses=[src])

    def const(self, v: int) -> VReg:
        d = self.new()
        v &= (1 << 64) - 1
        self.emit("movimm", [Reg(d, 32 if v < (1 << 32) else 64), Imm(v)], defs=[d])
        return d

    def operand(self, e: ir.Expr, ok) -> object:
        """e as an immediate when `ok(value)` accepts it, else a register."""
        if isinstance(e, ir.Const) and ok(_const_value(e)):
            return Imm(_const_value(e))
        return Reg(self.gen(e), 64)

    def add_imm(self, a: VReg, k: int) -> VReg:
        """a + k for any 64-bit k (sub for a negative one)."""
        k &= (1 << 64) - 1
        s = k - (1 << 64) if k >= (1 << 63) else k
        d = self.new()
        if s >= 0 and imm12(s) is not None:
            self.emit("add", [Reg(d, 64), Reg(a, 64), Imm(s)], defs=[d], uses=[a])
        elif s < 0 and imm12(-s) is not None:
            self.emit("sub", [Reg(d, 64), Reg(a, 64), Imm(-s)], defs=[d], uses=[a])
        else:
            t = self.const(k)
            self.emit("add", [Reg(d, 64), Reg(a, 64), Reg(t, 64)], defs=[d], uses=[a, t])
        return d

    def op3(self, op: str, a: VReg, b, w: int = 64) -> VReg:
        """d = a `op` b, b a register or an immediate."""
        d = self.new()
        bo = Imm(b.v) if isinstance(b, Imm) else Reg(b.r if isinstance(b, Reg) else b, w)
        self.emit(op, [Reg(d, w), Reg(a, w), bo], defs=[d], uses=[a] + regs_in(bo))
        return d

    def logic(self, op: str, a: VReg, v: int, w: int = 64) -> VReg:
        """d = a `op` v (and, orr, eor): the immediate when it is a bitmask immediate, else v in
        a register."""
        v &= (1 << w) - 1
        if logical_imm(v, w):
            return self.op3(op, a, Imm(v), w=w)
        return self.op3(op, a, Reg(self.const(v), w), w=w)

    def sym_addr(self, sym: str) -> VReg:
        d = self.new()
        self.emit("adrl", [Reg(d, 64), Mem(sym=sym)], defs=[d])
        return d

    # ---------------------------------------------------------------- addresses
    def addr(self, e: ir.Expr, size: int = 1) -> Mem:
        """A pointer expression as [base, #disp] or [base, index], for an access of `size`
        bytes; an offset A64 cannot encode there is added into the base first."""
        disp, idx = 0, []
        while True:
            if isinstance(e, ir.PtrAdd):
                if isinstance(e.n, ir.Const):
                    disp += _const_value(e.n)
                else:
                    idx.append(e.n)
                e = e.p
            elif isinstance(e, ir.PtrSub) and isinstance(e.n, ir.Const):
                disp -= _const_value(e.n)
                e = e.p
            else:
                break
        base = self.gen(e)
        idx.reverse()           # inner offsets first, as the interpreter evaluates them
        index = None
        for x in idx:
            r = self.gen(x)
            if index is None:
                index = r
            else:
                index = self.op3("add", index, Reg(r, 64))
        disp = ((disp + (1 << 63)) % (1 << 64)) - (1 << 63)
        if index is not None and disp:
            base = self.op3("add", base, Reg(index, 64))
            index = None
        m = Mem(base=base, index=index, disp=disp, w=8 * size)
        if not mem_ok(m, size):
            m = Mem(base=self.add_imm(base, disp), w=8 * size)
        return m

    # ---------------------------------------------------------------- expressions
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
            m = self.addr(e, 1)
            if m.index is None and m.disp == 0:
                return m.base
            if m.index is not None:
                return self.op3("add", m.base, Reg(m.index, 64))
            return self.add_imm(m.base, m.disp)
        if t is ir.PtrSub:
            a = self.gen(e.p)
            return self.op3("sub", a, self.operand(e.n, lambda v: imm12(v) is not None))
        if t is ir.PtrDiff:
            a = self.gen(e.a)
            return self.op3("sub", a, self.operand(e.b, lambda v: imm12(v) is not None))
        if t is ir.Load8:
            m = self.addr(e.p, 1)
            d = self.new()
            self.emit("ldrb", [Reg(d, 32), m], defs=[d], uses=regs_in(m))
            return d
        if t is ir.Bin:
            return self.binop(e)
        if t is ir.Not:
            return self.logic("eor", self.gen(e.a), 1, w=32)
        if t is ir.Prim:
            return self.prim(e)
        if t is ir.Call:
            return self.call(e)
        if t is ir.CallTable:
            i = self.gen(e.idx)        # the interpreter's order: the index, then the arguments
            vals = [self.gen(a) for a in e.args]
            # a table of 32-bit offsets from its own start (in .text: no relocations)
            base = self.sym_addr(f"nvol_fntbl_{e.table}")
            off = self.new()
            self.emit("ldrsw", [Reg(off, 64), Mem(base, i, scale=4, w=32)], defs=[off], uses=[base, i])
            fn = self.op3("add", base, Reg(off, 64))
            g = self.mod.fn(self.mod.fn_tables[e.table][0])
            return self.call_out(fn, vals, g.ret)
        if t is ir.Visit:
            # cb points to {fn, ctx}: fn(ctx, at, mask), an int (non-zero = stop)
            cb, at, mask = self.gen(e.cb), self.gen(e.at), self.gen(e.mask)
            fn = self.new()
            self.emit("ldr", [Reg(fn, 64), Mem(cb, w=64)], defs=[fn], uses=[cb])
            return self.call_out(fn, [Mem(cb, disp=8, w=64), at, mask], "int")
        if t is ir.TableLoad:
            d = self.new()
            base = self.sym_addr(f"nvol_tbl_{e.table}")
            if isinstance(e.idx, ir.Const):
                m = Mem(base, disp=_const_value(e.idx), w=8)
                if not mem_ok(m, 1):
                    m = Mem(self.add_imm(base, _const_value(e.idx)), w=8)
            else:
                m = Mem(base, self.gen(e.idx), w=8)
            self.emit("ldrb", [Reg(d, 32), m], defs=[d], uses=regs_in(m))
            return d
        raise Unsupported(f"{self.f.name}: {t.__name__}")

    def ptr_idiom(self, e):
        """The alignment arithmetic of the kernels, as `x86.X86Lower.ptr_idiom` simplifies it:
        p + (V - (addr(p) & (V-1))) -> (p & -V) + V; p - (addr(p) & m) -> p & ~m;
        a - (k - (b - a)) -> b - k."""
        n = e.n
        if isinstance(e, ir.PtrAdd) and isinstance(n, ir.Bin) and n.op == "sub" \
                and isinstance(n.a, ir.Const) and _masked_addr(n.b, e.p) == _const_value(n.a) - 1 \
                and _const_value(n.a) & (_const_value(n.a) - 1) == 0:
            V = _const_value(n.a)
            d = self.logic("and", self.gen(e.p), -V)
            return self.add_imm(d, V)
        if isinstance(e, ir.PtrSub) and _masked_addr(n, e.p) is not None:
            return self.logic("and", self.gen(e.p), ~_masked_addr(n, e.p))
        if isinstance(e, ir.PtrSub) and isinstance(n, ir.Bin) and n.op == "sub" \
                and isinstance(n.a, ir.Const) and isinstance(n.b, ir.PtrDiff) and _same(n.b.b, e.p):
            return self.gen(ir.PtrSub(n.b.a, n.a))
        return None

    def binop(self, e: ir.Bin) -> VReg:
        op = e.op
        if op in CC or op in ("land", "lor"):
            return self.bool_value(e)
        if op in ("add", "sub"):
            a = self.gen(e.a)
            if isinstance(e.b, ir.Const):
                k = _const_value(e.b)
                return self.add_imm(a, k if op == "add" else -k)
            return self.op3(op, a, Reg(self.gen(e.b), 64))
        if op in ("and", "or", "xor"):
            a = self.gen(e.a)
            b = self.operand(e.b, lambda v: logical_imm(v, 64))
            return self.op3({"and": "and", "or": "orr", "xor": "eor"}[op], a, b)
        # wrapping u8/u32 operations in 32-bit registers (the write zero-extends); a u8 result
        # is cut to its byte. A shift count is below the width (the interpreter's UB otherwise)
        w = 32 if e.ty in (ir.U8, ir.U32) else 64
        if op in ("shl", "shr"):
            ow = w if op == "shl" else 64
            a = self.gen(e.a)
            b = Imm(_const_value(e.b)) if isinstance(e.b, ir.Const) else Reg(self.gen(e.b), ow)
            d = self.op3("lsl" if op == "shl" else "lsr", a, b, w=ow)
        elif op in ("wadd", "wsub", "wmul"):
            a = self.gen(e.a)
            if op == "wmul":
                d = self.op3("mul", a, Reg(self.gen(e.b), w), w=w)
            else:
                b = self.operand(e.b, lambda v: imm12(v & ((1 << w) - 1)) is not None)
                if isinstance(b, Imm):
                    b = Imm(b.v & ((1 << w) - 1))
                d = self.op3("add" if op == "wadd" else "sub", a, b, w=w)
        elif op in ("max", "min"):
            a, b = self.gen(e.a), self.gen(e.b)
            self.emit("cmp", [Reg(a, 64), Reg(b, 64)], uses=[a, b])
            d = self.new()
            self.emit("csel", [Reg(d, 64), Reg(a, 64), Reg(b, 64), "hs" if op == "max" else "ls"],
                      defs=[d], uses=[a, b])
            return d
        else:
            raise Unsupported(f"{self.f.name}: Bin {op}")
        if e.ty is ir.U8 and op != "shr":
            d = self.logic("and", d, 0xFF, w=32)
        return d

    def compare(self, a: VReg, b: ir.Expr) -> None:
        """cmp a, b: an immediate when it encodes (cmn for a small negative one)."""
        if isinstance(b, ir.Const):
            v = _const_value(b)
            if imm12(v) is not None:
                self.emit("cmp", [Reg(a, 64), Imm(v)], uses=[a])
                return
            s = v - (1 << 64) if v >= (1 << 63) else v
            if s < 0 and imm12(-s) is not None:
                self.emit("cmn", [Reg(a, 64), Imm(-s)], uses=[a])
                return
        r = self.gen(b)
        self.emit("cmp", [Reg(a, 64), Reg(r, 64)], uses=[a, r])

    def bool_value(self, e: ir.Expr) -> VReg:
        if isinstance(e, ir.Bin) and e.op in CC:
            a = self.gen(e.a)
            self.compare(a, e.b)
            d = self.new()
            self.emit("cset", [Reg(d, 32), COND[CC[e.op]]], defs=[d])
            return d
        d = self.new()
        end = self.label()
        self.emit("movimm", [Reg(d, 32), Imm(0)], defs=[d])
        self.branch(e, end, False)
        self.emit("movimm", [Reg(d, 32), Imm(1)], defs=[d])
        self.place(end)
        return d

    # ---------------------------------------------------------------- primitives
    def prim(self, e: ir.Prim) -> VReg:
        n = e.name
        if n in ("load16u", "load32u", "load64"):
            size = {"load16u": 2, "load32u": 4, "load64": 8}[n]
            m = self.addr(e.args[0], size)
            d = self.new()
            op, w = {2: ("ldrh", 32), 4: ("ldr", 32), 8: ("ldr", 64)}[size]
            self.emit(op, [Reg(d, w), m], defs=[d], uses=regs_in(m))
            return d
        if n in ("first_offset", "first_offset64", "last_offset", "last_offset64"):
            a = self.gen(e.args[0])
            d = self.new()
            if n.startswith("last"):
                c = self.new()
                self.emit("clz", [Reg(c, 64), Reg(a, 64)], defs=[c], uses=[a])
                d = self.logic("eor", c, 63)        # 63 - clz, clz in 0..63
            else:
                t = self.new()
                self.emit("rbit", [Reg(t, 64), Reg(a, 64)], defs=[t], uses=[a])
                self.emit("clz", [Reg(d, 64), Reg(t, 64)], defs=[d], uses=[t])
            if not n.endswith("64") and self.isa is not None and self.isa.name == "neon":
                d = self.op3("lsr", d, Imm(2))     # 4 mask bits per byte lane
            return d
        if n == "popcnt":
            return self.popcnt(self.gen(e.args[0]))
        if n.startswith("cpu_"):
            raise Unsupported(f"{self.f.name}: no CPU checks on aarch64 (NEON is the baseline)")
        if n in ("is_zero", "any_nonzero"):
            x = self.vmax(self.gen(e.args[0]))
            self.emit("cmp", [Reg(x, 64), Imm(0)], uses=[x])
            d = self.new()
            self.emit("cset", [Reg(d, 32), "eq" if n == "is_zero" else "ne"], defs=[d])
            return d
        return self.vprim(e)

    def popcnt(self, a: VReg) -> VReg:
        """cnt counts the bits of each byte; addv adds the 8 counts."""
        v = self.new("v")
        self.emit("fmovdx", [Reg(v, 64), Reg(a, 64)], defs=[v], uses=[a])
        c = self.new("v")
        self.emit("vcnt8b", [Reg(c, 64), Reg(v, 64)], defs=[c], uses=[v])
        s = self.new("v")
        self.emit("vaddvb", [Reg(s, 8), Reg(c, 64)], defs=[s], uses=[c])
        d = self.new()
        self.emit("umovb", [Reg(d, 32), Reg(s, 128)], defs=[d], uses=[s])
        return d

    def vmax(self, a: VReg) -> VReg:
        """The low 64 bits of umaxp(a, a), in an integer register: zero iff every byte of a is."""
        t = self.new("v")
        self.emit("vumaxp", [Reg(t, 128), Reg(a, 128), Reg(a, 128)], defs=[t], uses=[a])
        x = self.new()
        self.emit("fmovxd", [Reg(x, 64), Reg(t, 64)], defs=[x], uses=[t])
        return x

    def v2(self, op: str, a: VReg, b: VReg) -> VReg:
        d = self.new("v")
        self.emit(op, [Reg(d, 128), Reg(a, 128), Reg(b, 128)], defs=[d], uses=[a, b])
        return d

    def v1(self, op: str, a: VReg, imm=None) -> VReg:
        d = self.new("v")
        args = [Reg(d, 128), Reg(a, 128)] + ([Imm(imm)] if imm is not None else [])
        self.emit(op, args, defs=[d], uses=[a])
        return d

    def vprim(self, e: ir.Prim) -> VReg:
        n = e.name
        if self.isa is None:
            raise Unsupported(f"{self.f.name}: vector primitive {n} outside an ISA function")
        if n == "splat":
            x = e.args[0]
            d = self.new("v")
            if isinstance(x, ir.Const):
                self.emit("movi", [Reg(d, 128), Imm(_const_value(x) & 0xFF)], defs=[d])
            else:
                r = self.gen(x)
                self.emit("dup", [Reg(d, 128), Reg(r, 32)], defs=[d], uses=[r])
            return d
        if n == "vzero":
            d = self.new("v")
            self.emit("movi", [Reg(d, 128), Imm(0)], defs=[d])
            return d
        if n in ("load_a", "load_u"):
            m = self.addr(e.args[0], 16)
            d = self.new("v")
            self.emit("ldr", [Reg(d, 128), m], defs=[d], uses=regs_in(m))
            return d
        if n in VBIN:
            return self.v2(VBIN[n], self.gen(e.args[0]), self.gen(e.args[1]))
        if n == "vshuffle":         # vqtbl1q_u8(table, indices)
            return self.v2("vtbl", self.gen(e.args[0]), self.gen(e.args[1]))
        if n == "vadd64":
            return self.v2("vadd2d", self.gen(e.args[0]), self.gen(e.args[1]))
        if n == "movemask":
            # shrn #4 of the 16-bit lanes keeps 4 bits per byte; the top bit of each nibble is
            # the byte's (0x00 or 0xFF lanes), as the C does
            a = self.gen(e.args[0])
            t = self.new("v")
            self.emit("vshrn", [Reg(t, 64), Reg(a, 128), Imm(4)], defs=[t], uses=[a])
            x = self.new()
            self.emit("fmovxd", [Reg(x, 64), Reg(t, 64)], defs=[x], uses=[t])
            return self.logic("and", x, 0x8888888888888888)
        if n == "bmask64x4":
            # simdjson's to_bitmask: AND with the bit weights 1, 2, ..., 128 of each 8-byte half,
            # then three pairwise adds fold each 8 bytes into one; the low 64 bits are the mask
            r = self.const(0x8040201008040201)
            w = self.new("v")
            self.emit("dup2d", [Reg(w, 128), Reg(r, 64)], defs=[w], uses=[r])
            t = [self.v2("vand", self.gen(v), w) for v in e.args]
            s = self.v2("vaddp", self.v2("vaddp", t[0], t[1]), self.v2("vaddp", t[2], t[3]))
            s = self.v2("vaddp", s, s)
            x = self.new()
            self.emit("fmovxd", [Reg(x, 64), Reg(s, 64)], defs=[x], uses=[s])
            return x
        if n == "sad0":
            a = self.gen(e.args[0])
            for op in ("vuaddlp8h", "vuaddlp4s", "vuaddlp2d"):
                a = self.v1(op, a)
            return a
        if n == "hsum64":
            a = self.gen(e.args[0])
            t = self.new("v")
            self.emit("vaddpd", [Reg(t, 64), Reg(a, 128)], defs=[t], uses=[a])
            x = self.new()
            self.emit("fmovxd", [Reg(x, 64), Reg(t, 64)], defs=[x], uses=[t])
            return x
        if n in SHIN:               # vextq_u8(b, a, k)
            a, b = self.gen(e.args[0]), self.gen(e.args[1])
            d = self.new("v")
            self.emit("vext", [Reg(d, 128), Reg(b, 128), Reg(a, 128), Imm(SHIN[n])],
                      defs=[d], uses=[b, a])
            return d
        if n in ("lane64_0", "lane64_1"):
            a = self.gen(e.args[0])
            d = self.new()
            self.emit("umovd", [Reg(d, 64), Reg(a, 128), Imm(int(n[-1]))], defs=[d], uses=[a])
            return d
        if n == "vnib_hi":
            return self.v1("vushr", self.gen(e.args[0]), 4)
        if n == "msb_lanes":
            return self.v1("vsshr", self.gen(e.args[0]), 7)
        raise Unsupported(f"{self.f.name}: primitive {n}")

    # ---------------------------------------------------------------- calls
    def tail_call(self, e) -> bool:
        """`return f(...)` with f out of line, of the same return type: the epilogue and `b f`.
        Through a function table, `br`: the target in the first argument register the call
        leaves free (Clang's choice too; never callee-saved, which the epilogue reloads)."""
        if isinstance(e, ir.CallTable) and self.mod:
            g = self.mod.fn(self.mod.fn_tables[e.table][0])
            if g.ret is not self.f.ret or len(e.args) >= len(self.abi.args):
                return False
            i = self.gen(e.idx)        # the interpreter's order: the index, then the arguments
            vals = [self.gen(a) for a in e.args]
            base = self.sym_addr(f"nvol_fntbl_{e.table}")
            off = self.new()
            self.emit("ldrsw", [Reg(off, 64), Mem(base, i, scale=4, w=32)], defs=[off], uses=[base, i])
            fn = self.op3("add", base, Reg(off, 64))
            uses = []
            for k, v in enumerate(vals):
                r = PHYS[self.abi.args[k]]
                self.mov(r, v)
                uses.append(r)
            t = PHYS[self.abi.args[len(vals)]]
            self.mov(t, fn)
            self.emit("tailcall", [Reg(t, 64)], uses=uses + [t])
            return True
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
        t = _target(g)
        if not isinstance(t, Lab):
            raise Unsupported(f"{self.f.name}: a call through an ifunc pointer on aarch64")
        self.emit("tailcall", [t], uses=uses)
        return True

    def call_out(self, target, vals: list, ret) -> VReg:
        """A call by AAPCS64: arguments in x0-x7, every caller-saved register and every vector
        register destroyed. `ret` "int" is a C int used as a bool."""
        if len(vals) > len(self.abi.args):
            raise Unsupported(f"{self.f.name}: a call with stack arguments")
        uses = []
        for i, v in enumerate(vals):
            r = PHYS[self.abi.args[i]]
            self.mov(r, v)
            uses.append(r)
        clob = [r for r in self.abi.caller_saved() if r.cls == "g"] + \
               [PHYS[v] for v in self.abi.vecs]
        if isinstance(target, Lab):
            self.emit("call", [target], defs=[X0], uses=uses, clobbers=clob)
        elif isinstance(target, Mem):
            raise Unsupported(f"{self.f.name}: a call through an ifunc pointer on aarch64")
        else:
            self.emit("callind", [Reg(target, 64)], defs=[X0], uses=uses + [target], clobbers=clob)
        d = self.new()
        if ret == "int":
            self.emit("cmp", [Reg(X0, 32), Imm(0)], uses=[X0])
            self.emit("cset", [Reg(d, 32), "ne"], defs=[d])
        elif ret in (ir.U8, ir.BOOL):           # the upper bits of a narrow result are undefined
            self.emit("and", [Reg(d, 32), Reg(X0, 32), Imm(0xFF)], defs=[d], uses=[X0])
        elif ret is ir.U32:
            self.emit("mov", [Reg(d, 32), Reg(X0, 32)], defs=[d], uses=[X0])
        elif ret is ir.VOID:
            return None
        else:
            self.mov(d, X0)
        return d

    # ---------------------------------------------------------------- conditions
    def select(self, e: ir.Bin, x: ir.Expr, y: ir.Expr) -> VReg:
        """x when the comparison e holds, else y: csel, or csinv against xzr when x is all
        ones (`p == NULL ? -1 : p - start`, as Clang writes it)."""
        yv = self.gen(y)
        ones = isinstance(x, ir.Const) and _const_value(x) == (1 << 64) - 1
        xv = None if ones else self.gen(x)
        self.compare(self.gen(e.a), e.b)
        d = self.new()
        if ones:
            self.emit("csinv", [Reg(d, 64), Reg(yv, 64), Reg(XZR, 64), COND[NEG[CC[e.op]]]],
                      defs=[d], uses=[yv])
        else:
            self.emit("csel", [Reg(d, 64), Reg(xv, 64), Reg(yv, 64), COND[CC[e.op]]],
                      defs=[d], uses=[xv, yv])
        return d

    def branch(self, e: ir.Expr, target: Lab, when: bool) -> None:
        """Jump to target when bool(e) == when; fall through otherwise."""
        if isinstance(e, ir.Bin) and e.op in ("eq", "ne") and e.a.ty is ir.BOOL \
                and isinstance(e.b, ir.Const):
            same = (e.op == "eq") == bool(_const_value(e.b))
            self.branch(e.a, target, when if same else not when)
            return
        bit = _bit_test(e)
        if bit is not None:
            # (x & (1 << n)) != 0 -> tbnz x, #n (a variable n: the bit shifted down first)
            x, n, is_set = bit
            a = self.gen(x)
            if isinstance(n, ir.Const):
                k = _const_value(n)
            else:
                a = self.op3("lsr", a, Reg(self.gen(n), 64))
                k = 0
            self.emit("tbnz" if is_set == when else "tbz", [target, Reg(a, 64), Imm(k)], uses=[a])
            return
        if isinstance(e, ir.Bin) and e.op in CC:
            a = self.gen(e.a)
            if isinstance(e.b, ir.Const) and _const_value(e.b) == 0 and e.op in ("eq", "ne"):
                zero = (e.op == "eq") == when
                self.emit("cbz" if zero else "cbnz", [target, Reg(a, 64)], uses=[a])
                return
            self.compare(a, e.b)
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
            x = self.vmax(self.gen(e.args[0]))
            zero = (e.name == "is_zero") == when
            self.emit("cbz" if zero else "cbnz", [target, Reg(x, 64)], uses=[x])
            return
        if isinstance(e, ir.Const):
            if bool(_const_value(e)) == when:
                self.emit("jmp", [target])
            return
        v = self.gen(e)
        self.emit("cbnz" if when else "cbz", [target, Reg(v, 64)], uses=[v])

    # ---------------------------------------------------------------- function
    def run(self) -> list:
        f = self.f
        if f.ifunc:
            raise Unsupported(f"{f.name}: ifunc")
        if f.ret is ir.VEC or any(p.ty is ir.VEC for p in f.params):
            raise Unsupported(f"{f.name}: vectors are passed only to inlined functions")
        if len(f.params) > len(self.abi.args):
            raise Unsupported(f"{f.name}: stack arguments")
        for i, p in enumerate(f.params):
            d = self.new()
            self.vars[p.name] = d
            r = self.abi.arg_reg(i)
            # AAPCS64 leaves the upper bits of a narrow argument unspecified: zero-extend
            # (unless it is only passed on to a callee that extends it)
            if p.ty in (ir.U8, ir.BOOL, ir.U32) and self.forwarded_only(p):
                self.emit("mov", [Reg(d, 64), Reg(r, 64)], defs=[d], uses=[r])
            elif p.ty is ir.U8 or p.ty is ir.BOOL:
                self.emit("and", [Reg(d, 32), Reg(r, 32), Imm(0xFF)], defs=[d], uses=[r])
            elif p.ty is ir.U32:
                self.emit("mov", [Reg(d, 32), Reg(r, 32)], defs=[d], uses=[r])
            else:
                self.emit("mov", [Reg(d, 64), Reg(r, 64)], defs=[d], uses=[r])
        self.block(f.body)
        self.place(self.ret_label)
        self.emit("ret", [], uses=[X0] if f.ret is not ir.VOID else [])
        return self.code

    # ---------------------------------------------------------------- statement hooks
    def store8(self, s) -> None:
        m = self.addr(s.ptr, 1)
        if isinstance(s.value, ir.Const) and _const_value(s.value) & 0xFF == 0:
            self.emit("strb", [m, Reg(XZR, 32)], uses=regs_in(m))
            return
        v = self.gen(s.value)
        self.emit("strb", [m, Reg(v, 32)], uses=regs_in(m) + [v])

    def store_vec(self, s) -> None:
        m = self.addr(s.ptr, 16)
        v = self.gen(s.value)
        self.emit("str", [m, Reg(v, 128)], uses=regs_in(m) + [v])

    def store64(self, s) -> None:
        m = self.addr(s.ptr, 8)
        if isinstance(s.value, ir.Const) and _const_value(s.value) == 0:
            self.emit("str", [m, Reg(XZR, 64)], uses=regs_in(m))
            return
        v = self.gen(s.value)
        self.emit("str", [m, Reg(v, 64)], uses=regs_in(m) + [v])

    def ret_value(self, v: VReg) -> None:
        self.mov(X0, v)

    def add8(self, a: VReg, b: VReg) -> VReg:
        return self.v2("vadd", a, b)

    def sub8_into(self, dst: VReg, t: VReg) -> None:
        self.emit("vsub", [Reg(dst, 128), Reg(dst, 128), Reg(t, 128)], defs=[dst], uses=[dst, t])

    def foldable(self, e) -> bool:
        return False            # A64 has no memory operands


# ---------------------------------------------------------------- encoding
PSEUDO = {"label", "align", "coldbegin", "coldend", "rarebegin", "rareend", "cfi", "endprolog"}


def length(ins: Ins) -> int:
    if ins.op in PSEUDO:
        return 0
    if ins.op == "movimm":
        return 4 * len(mov_parts(ins.args[1].v, ins.args[0].w))
    if ins.op == "adrl":
        return 8
    if ins.op in ("ldr", "str") and isinstance(ins.args[1 if ins.op == "ldr" else 0], Mem) \
            and ins.args[1 if ins.op == "ldr" else 0].sym is not None:
        return 8            # adrp + the access
    return 4


def code_size(code: list) -> int:
    off = 0
    for ins in code:
        if ins.op == "align":
            off += align_pad(off, ins)
        off += length(ins)
    return off


def align_pad(off: int, ins: Ins) -> int:
    """Bytes of nop that align a loop head (`align` N, max): to N when that costs at most max
    bytes, else to 8, as on x86."""
    n, mx = ins.args[0].v, ins.args[1].v
    pad = (-off) % n
    return pad if pad <= mx else (-off) % 8


# branch ranges, in instructions
_RANGE = {"tbz": 1 << 13, "tbnz": 1 << 13, "cbz": 1 << 18, "cbnz": 1 << 18}


def check_ranges(name: str, code: list) -> list:
    """Every branch reaches its label (b.cond and cbz: +-1 MiB, tbz: +-32 KiB): A64 has no
    longer form, so a function too large for one fails here instead of in the assembler."""
    at, off = {}, 0
    for ins in code:
        if ins.op == "align":
            off += align_pad(off, ins)
        if ins.op == "label":
            at[ins.args[0].name] = off
        off += length(ins)
    off = 0
    for ins in code:
        if ins.op == "align":
            off += align_pad(off, ins)
        if (ins.op in CONDJUMPS or ins.op == "jmp") and isinstance(ins.args[0], Lab) \
                and ins.args[0].name in at:
            lim = 4 * _RANGE.get(ins.op, 1 << 18)
            if not -lim <= at[ins.args[0].name] - off < lim:
                raise Unsupported(f"{name}: {ins.op} out of range")
        off += length(ins)
    return code


# ---------------------------------------------------------------- register allocation hooks
# held back once anything spills: x16/x17 are the procedure-call scratch registers of AAPCS64
SCRATCH = {"g": ("x16", "x17", "x15"), "v": ("v31", "v30", "v29")}
MOVES = {"mov", "vmov"}
_STORES = {"str", "strb"}


def avx(code) -> bool:
    return False


def copy_ins(c: VReg, v: VReg, wide: bool) -> Ins:
    if v.cls == "g":
        return Ins("mov", [Reg(c, 64), Reg(v, 64)], [c], [v])
    return Ins("vmov", [Reg(c, 128), Reg(v, 128)], [c], [v])


def direct(ins: Ins, spilled) -> str:
    """As `x86.direct`: a move that touches one spilled register reads or writes its slot."""
    if ins.op not in MOVES or len(ins.args) != 2 or not all(isinstance(a, Reg) for a in ins.args):
        return ""
    d, s = ins.args[0].r, ins.args[1].r
    ds, ss = isinstance(d, VReg) and d in spilled, isinstance(s, VReg) and s in spilled
    if ss and not ds:
        return "load"
    if ds and not ss and (ins.op != "mov" or ins.args[0].w == 64):
        return "store"
    if ds and ss and (ins.op != "mov" or ins.args[0].w == 64):
        return "both"
    return ""


def rewrite(code, assign, spilled):
    """Physical registers for every virtual one; a spilled register lives in a 16-byte slot,
    loaded into a scratch register before an instruction that reads it and stored after one
    that writes it (`ldr`/`str` x or q)."""
    slot = {v: k for k, v in enumerate(sorted(spilled, key=lambda r: r.id))}
    out = []

    def phys(r):
        return assign[r] if isinstance(r, VReg) else r

    for ins in code:
        if not any(isinstance(r, VReg) and r in spilled for r in ins.uses + ins.defs):
            out.append(_subst(ins, assign))
            continue
        kind = direct(ins, spilled)
        if kind:
            d, s = ins.args
            w = 128 if d.r.cls == "v" else d.w
            if kind == "both":
                t = PHYS[SCRATCH[d.r.cls][0]]
                out.append(Ins("ldr", [Reg(t, w), Mem(spill=slot[s.r], w=w)], [t], []))
                out.append(Ins("str", [Mem(spill=slot[d.r], w=w), Reg(t, w)], [], [t]))
            elif kind == "load":
                pd = phys(d.r)
                out.append(Ins("ldr", [Reg(pd, w), Mem(spill=slot[s.r], w=w)], [pd], []))
            else:
                ps = phys(s.r)
                out.append(Ins("str", [Mem(spill=slot[d.r], w=w), Reg(ps, w)], [], [ps]))
            continue
        used = {"g": 0, "v": 0}
        local = dict(assign)
        pre, post = [], []
        for r in dict.fromkeys(ins.uses + ins.defs):
            if isinstance(r, VReg) and r in spilled:
                if used[r.cls] == len(SCRATCH[r.cls]):
                    raise NotImplementedError(f"{ins}: more spilled {r.cls} registers than scratch ones")
                s = PHYS[SCRATCH[r.cls][used[r.cls]]]
                used[r.cls] += 1
                local[r] = s
                w = 64 if r.cls == "g" else 128
                if r in ins.uses:
                    pre.append(Ins("ldr", [Reg(s, w), Mem(spill=slot[r], w=w)], [s], []))
                if r in ins.defs:
                    post.append(Ins("str", [Mem(spill=slot[r], w=w), Reg(s, w)], [], [s]))
        out += pre + [_subst(ins, local)] + post
    return reloads(out, "ldr", "str", _STORES), len(slot)


def peephole(code: list) -> list:
    out = [ins for ins in code if not _identity(ins)]
    # `cmp r, #0` then b.eq/b.ne: cbz/cbnz r (no flags)
    fused, k = [], 0
    while k < len(out):
        ins = out[k]
        if ins.op == "cmp" and k + 1 < len(out) and out[k + 1].op in ("je", "jne") \
                and isinstance(ins.args[1], Imm) and ins.args[1].v == 0:
            j = out[k + 1]
            fused.append(Ins("cbz" if j.op == "je" else "cbnz", [j.args[0], ins.args[0]],
                             j.defs, ins.uses))
            k += 2
            continue
        fused.append(ins)
        k += 1
    res = jump_cleanup(fused)
    return copy_back([ins for ins in reloads(res, "ldr", "str", _STORES) if not _identity(ins)])


def _identity(ins: Ins) -> bool:
    return ins.op in MOVES and len(ins.args) == 2 and all(isinstance(a, Reg) for a in ins.args) \
        and ins.args[0].r == ins.args[1].r and ins.args[0].w == ins.args[1].w \
        and (ins.args[0].w != 32)


X29, X30 = PHYS["x29"], PHYS["x30"]


def frame(code: list, abi: ABI, nslots: int) -> list:
    """The stack frame: from [sp, #0] x29/x30 when the function calls, the callee-saved
    x19-x28 and d8-d15 it writes, then the 16-byte spill slots (the saves first: `stp` reaches
    only 504 bytes). One `sub sp` and one `add sp`; the saves are `stp`/`str` at fixed offsets,
    with the `.cfi_*` steps of each."""
    used = {r for ins in code for r in ins.defs if isinstance(r, Phys)}
    calls = any(ins.op in ("call", "callind") for ins in code)
    saved = [PHYS[r] for r in abi.callee_saved if PHYS[r] in used]
    if calls:
        saved = [X29, X30] + saved
    vsaved = [PHYS[r] for r in abi.vec_callee_saved if PHYS[r] in used]
    gpr_off = 0
    vec_off = 8 * len(saved)
    vec_off += vec_off % 16 and 8
    spill_base = -(-(vec_off + 8 * len(vsaved)) // 16) * 16
    size = spill_base + 16 * nslots
    if size > 4095:
        raise NotImplementedError(f"a {size}-byte frame: larger frames are not generated")

    def fix(a):
        if isinstance(a, Mem) and a.spill is not None:
            return Mem(SP, disp=spill_base + 16 * a.spill, w=a.w)
        if isinstance(a, Mem) and a.arg is not None:
            return Mem(SP, disp=size + abi.stack_arg_offset(a.arg), w=a.w)
        return a

    body = [Ins(i.op, [fix(a) for a in i.args], i.defs, i.uses, i.clobbers, i.hint) for i in code]
    pro, epi = [], []
    if size:
        pro.append(Ins("sub", [Reg(SP, 64), Reg(SP, 64), Imm(size)]))
        pro.append(Ins("cfi", [f".cfi_def_cfa_offset {size}"]))
    pairs = [saved[k:k + 2] for k in range(0, len(saved), 2)]
    for k, pr in enumerate(pairs):
        off = gpr_off + 16 * k
        if len(pr) == 2:
            pro.append(Ins("stp", [Mem(SP, disp=off, w=64), Reg(pr[0], 64), Reg(pr[1], 64)]))
            epi.append(Ins("ldp", [Reg(pr[0], 64), Reg(pr[1], 64), Mem(SP, disp=off, w=64)]))
        else:
            pro.append(Ins("str", [Mem(SP, disp=off, w=64), Reg(pr[0], 64)]))
            epi.append(Ins("ldr", [Reg(pr[0], 64), Mem(SP, disp=off, w=64)]))
        for j, r in enumerate(pr):
            pro.append(Ins("cfi", [f".cfi_offset {r.name[1:]}, {off + 8 * j - size}"]))
    for k, r in enumerate(vsaved):
        off = vec_off + 8 * k
        pro.append(Ins("str", [Mem(SP, disp=off, w=64), Reg(r, 64)]))
        pro.append(Ins("cfi", [f".cfi_offset {64 + int(r.name[1:])}, {off - size}"]))  # DWARF v n
        epi.append(Ins("ldr", [Reg(r, 64), Mem(SP, disp=off, w=64)]))
    if size:
        epi.append(Ins("add", [Reg(SP, 64), Reg(SP, 64), Imm(size)]))
    # The first save at [sp] takes the `sub sp` in its writeback (`stp x29, x30, [sp, #-32]!`),
    # and its load the `add sp` (`ldp x29, x30, [sp], #32`), as Clang does: the pair form
    # reaches -512..504 bytes, the single str/ldr -256..255.
    first = pro[2] if size and pairs else None
    if first is not None and first.args[0].disp == 0 and size <= (504 if first.op == "stp" else 255):
        load = next(i for i in epi if i.op in ("ldp", "ldr") and i.args[-1].disp == 0)
        pro[0:3] = [Ins(first.op, [Mem(SP, disp=-size, w=64)] + first.args[1:], hint="pre"),
                    pro[1]]
        epi.remove(load)
        epi[-1] = Ins(load.op, load.args[:-1] + [Mem(SP, disp=size, w=64)], hint="post")
    out = pro
    for ins in body:
        if ins.op in ("ret", "tailcall"):     # a tail call leaves through the epilogue too
            out += epi
        out.append(ins)
    return out
