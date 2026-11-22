"""x86-64: instruction selection (SSE2/SSSE3/SSE4.2/AVX2, VEX in AVX2 functions) on top of the
target-neutral lowering of `lower.py`, and the parts of register allocation that name x86
instructions: scratch registers, spill code, the peephole and the stack frame. `regalloc`
takes this module as its target `T`."""
from __future__ import annotations

from .. import ir
from .abi import ABI
from .lower import (CC, NEG, Lower, Unsupported, _bit_test, _cmp_width, _const_value, _imm32,
                    _masked_addr, _same, _target)
from .mir import (CONDJUMPS, PHYS, RSP, TERMINATORS, VECS, Imm, Ins, Lab, Mem, Phys, Reg, VReg,
                  regs_in)
from .regalloc import _subst, copy_back, jump_cleanup, reloads

# element-wise vector operations: one instruction each (legacy name; VEX adds "v")
VBIN = {"cmpeq": "pcmpeqb", "vor": "por", "mask_or": "por", "vand": "pand", "vxor": "pxor", "vsub8": "psubb",
        "vmin8": "pminub", "vadd64": "paddq", "vshuffle": "pshufb", "unpacklo8": "punpcklbw",
        "unpackhi8": "punpckhbw"}
SHIN = {"shin1": 15, "shin2": 14, "shin3": 13, "hshin1": 15, "hshin2": 14, "hshin3": 13}

RAX, RCX = PHYS["rax"], PHYS["rcx"]

POPCNT8 = bytes(bin(i).count("1") for i in range(256))   # (aligned 32 like every pool entry)

# vector operations whose last source may be a memory operand (VEX: any alignment; legacy SSE:
# 16-byte aligned only), and those whose two sources commute
_FOLD = {"pcmpeqb", "por", "pand", "psubb", "pminub", "paddq", "pshufb", "punpcklbw",
         "punpckhbw", "psadbw", "pxor"}
_COMMUTE = {"pcmpeqb", "por", "pand", "pminub", "paddq", "pxor"}
_LOADS = {"movdqa", "movdqu", "vmovdqa", "vmovdqu"}


class X86Lower(Lower):
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

    def tail_call(self, e) -> bool:
        """`return f(...)` with f out of line, of the same return type, and every argument in a
        register: the epilogue and a jump to f, which returns to our caller (what GCC does at
        -O2): no call, no return through this function. Through a function table, `jmp rax`
        (volatile in both ABIs, and never an argument register)."""
        if isinstance(e, ir.CallTable) and self.mod:
            g = self.mod.fn(self.mod.fn_tables[e.table][0])
            if g.ret is not self.f.ret or len(e.args) > len(self.abi.args):
                return False
            i = self.gen(e.idx)        # the interpreter's order: the index, then the arguments
            vals = [self.gen(a) for a in e.args]
            base = self.new()
            self.emit("lea", [Reg(base, 64), Mem(sym=f"nvol_fntbl_{e.table}")], defs=[base])
            uses = []
            for k, v in enumerate(vals):
                r = PHYS[self.abi.args[k]]
                self.mov(r, v)
                uses.append(r)
            # the target computed straight into rax (no copy)
            self.emit("movsxd", [Reg(RAX, 64), Mem(base, i, scale=4, w=32)], defs=[RAX], uses=[base, i])
            self.emit("add", [Reg(RAX, 64), Reg(base, 64)], defs=[RAX], uses=[RAX, base])
            if self.vex:
                self.emit("vzeroupper", [])
            self.emit("tailcall", [Reg(RAX, 64)], uses=uses + [RAX])
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
        if self.vex:
            self.emit("vzeroupper", [])
        t = _target(g)
        self.emit("tailcall", [t], uses=uses)
        self.code[-1].hint = "near" if isinstance(t, Lab) else None
        return True

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
        if op in ("add", "wadd") and e.ty is not ir.U8:
            # (x << k) + y, k in 1..3: one lea [y + x*2^k] (Clang's Rabin-Karp hash step
            # `lea edx, [r11 + rdx*2]`). A 32-bit lea keeps the low 32 bits and zeroes the
            # rest: the same as shl + add wrapping in u32
            for sh, other in ((e.a, e.b), (e.b, e.a)):
                if isinstance(sh, ir.Bin) and sh.op == "shl" and isinstance(sh.b, ir.Const) \
                        and 1 <= _const_value(sh.b) <= 3 and sh.ty is e.ty:
                    x = self.gen(sh.a) if sh is e.a else None
                    y = self.gen(other)
                    if x is None:
                        x = self.gen(sh.a)
                    d = self.new()
                    m = Mem(y, x, scale=1 << _const_value(sh.b))
                    w = 32 if e.ty is ir.U32 else 64
                    self.emit("lea", [Reg(d, w), m], defs=[d], uses=[y, x])
                    return d
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
        if op in ("and", "or", "xor"):
            # a u32 constant operand in a 32-bit instruction (its imm32 is the whole value; the
            # write zero-extends), and x ^ all-ones as `not`: SWAR masks such as 0x80808080 or
            # !x need no register then
            w = 32 if e.ty is ir.U32 else 64
            full = (1 << w) - 1
            d = self.copy(self.gen(e.a))
            if isinstance(e.b, ir.Const) and e.ty in (ir.U32, ir.U64, ir.USIZE):
                v = _const_value(e.b) & full
                if op == "xor" and v == full:
                    self.emit("not", [Reg(d, w)], defs=[d], uses=[d])
                    return d
                if w == 32:
                    s = Imm(v - (1 << 32) if v >= 1 << 31 else v)
                    self.emit(op, [Reg(d, 32), s], defs=[d], uses=[d])
                    return d
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
        if n in ("first_offset", "first_offset64", "last_offset", "last_offset64"):
            a = self.gen(e.args[0])
            d = self.new()
            w = 64 if n.endswith("64") else 32
            self.emit("bsr" if n.startswith("last") else "bsf", [Reg(d, w), Reg(a, w)],
                      defs=[d], uses=[a])
            return d
        if n == "popcnt":
            x = e.args[0]
            bits16 = isinstance(x, ir.Prim) and x.name == "movemask" and self.vw == 128
            return self.popcnt(self.gen(x), 16 if bits16 else 32)
        if n == "popcnt64":
            return self.popcnt(self.gen(e.args[0]), 64)
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
        128-bit vector), four for 32 bits, eight for 64; 7 or 13 instructions against 16 for the
        SWAR count of 32 bits."""
        d = self.new()
        w = 64 if bits == 64 else 32
        if any(i and i.name in ("sse42", "avx2") for i in (self.isa, self.outer)):
            self.emit("popcnt", [Reg(d, w), Reg(a, w)], defs=[d], uses=[a])
            return d
        tbl = self.new()
        self.emit("lea", [Reg(tbl, 64), Mem(sym=self.pool.get(POPCNT8))], defs=[tbl])
        first = True
        for k in range(bits // 8):
            idx = self.new()
            if k == 0:
                self.emit("movzx", [Reg(idx, 32), Reg(a, 8)], defs=[idx], uses=[a])
            else:
                self.emit("mov", [Reg(idx, w), Reg(a, w)], defs=[idx], uses=[a])
                self.emit("shr", [Reg(idx, w), Imm(8 * k)], defs=[idx], uses=[idx])
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
        if n == "vandnot":                      # pandn d, s: d = ~d & s, so b goes first
            a = self.gen(e.args[0])
            b = self.gen(e.args[1])
            return self.vop("pandn", b, a)
        if n in VBIN:
            a = self.gen(e.args[0])
            b = self.gen(e.args[1])
            return self.vop(VBIN[n], a, b)
        if n in ("bmask64x2", "bmask64x4"):
            # one pmovmskb per vector, each shifted to its byte offset in the block and OR'd
            step = 64 // len(e.args)
            m = None
            for k, v in enumerate(e.args):
                t = ir.prim("movemask", v).cast(ir.U64)
                t = t << (step * k) if k else t
                m = t if m is None else m | t
            return self.gen(m)
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
    def select(self, e: ir.Bin, x: ir.Expr, y: ir.Expr) -> VReg:
        """x when the comparison e holds, else y: y in the result, then cmovCC from x (the
        compare last, after the values, whose arithmetic sets the flags)."""
        late = isinstance(x, ir.Const) and _const_value(x) != 0   # a mov imm: flags kept
        if late:
            # GCC's form: y aside, the compare, then x straight into the result (the compared
            # value is dead by then, so the result can take its register) and cmov from y
            t = self.gen(y)
        else:
            d = self.copy(self.gen(y))
            t = self.gen(x)
        a = self.gen(e.a)
        s = self.rhs(e.b)
        w = _cmp_width(e, s)
        if isinstance(s, Imm) and s.v == 0 and e.op in ("eq", "ne"):
            self.emit("test", [Reg(a, w), Reg(a, w)], uses=[a])
        else:
            self.emit("cmp", [Reg(a, w), Reg(s.r, w) if isinstance(s, Reg) else s],
                      uses=[a] + regs_in(s))
        if late:
            d = self.const(_const_value(x))      # (not zero: a mov, never the xor)
            self.emit("cmov" + NEG[CC[e.op]], [Reg(d, 64), Reg(t, 64)], defs=[d], uses=[d, t])
        else:
            self.emit("cmov" + CC[e.op], [Reg(d, 64), Reg(t, 64)], defs=[d], uses=[d, t])
        return d

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
            # (unless it is only passed on to a callee that extends it)
            if p.ty in (ir.U8, ir.BOOL, ir.U32) and self.forwarded_only(p):
                self.emit("mov", [Reg(d, 64), src], defs=[d], uses=regs_in(src))
            elif p.ty is ir.U8 or p.ty is ir.BOOL:
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

    # ---------------------------------------------------------------- statement hooks
    def store8(self, s) -> None:
        m = self.addr(s.ptr, 8)
        if isinstance(s.value, ir.Const):
            self.emit("mov", [m, Imm(_const_value(s.value) & 0xFF)], uses=regs_in(m))
        else:
            v = self.gen(s.value)
            self.emit("mov", [m, Reg(v, 8)], uses=regs_in(m) + [v])

    def store_vec(self, s) -> None:
        m = self.addr(s.ptr)
        v = self.gen(s.value)
        self.emit("vmovdqu" if self.vex else "movdqu", [m, Reg(v, self.vw)],
                  uses=regs_in(m) + [v])

    def store32(self, s) -> None:
        m = self.addr(s.ptr, 32)
        if isinstance(s.value, ir.Const):
            self.emit("mov", [m, Imm(_const_value(s.value) & 0xFFFFFFFF)], uses=regs_in(m))
        else:
            v = self.gen(s.value)
            self.emit("mov", [m, Reg(v, 32)], uses=regs_in(m) + [v])

    def store64(self, s) -> None:
        m = self.addr(s.ptr, 64)
        v = self.rhs(s.value)
        self.emit("mov", [m, v], uses=regs_in(m) + regs_in(v))

    def ret_value(self, v: VReg) -> None:
        self.mov(RAX, v)

    def add8(self, a: VReg, b: VReg) -> VReg:
        return self.vop("paddb", a, b)

    def sub8_into(self, dst: VReg, t: VReg) -> None:
        w = self.vw
        if self.vex:
            self.emit("vpsubb", [Reg(dst, w), Reg(dst, w), Reg(t, w)], defs=[dst], uses=[dst, t])
        else:
            self.emit("psubb", [Reg(dst, w), Reg(t, w)], defs=[dst], uses=[dst, t])


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


# ---------------------------------------------------------------- register allocation hooks
# held back once anything spills: an instruction reads at most three registers of a class (a
# store to [base + index] of a register); rbx is never named by the ABI or the lowering
SCRATCH = {"g": ("r11", "r10", "rbx"), "v": ("xmm15", "xmm14", "xmm13")}


_MOVES = {"mov", "movdqa", "vmovdqa"}


def _direct(ins: Ins, spilled) -> str:
    """"load" / "store" when a register-to-register move touches one spilled register and can
    read or write its slot itself (no scratch register, one instruction), else ""."""
    if ins.op not in _MOVES or len(ins.args) != 2 or not all(isinstance(a, Reg) for a in ins.args):
        return ""
    d, s = ins.args[0].r, ins.args[1].r
    ds, ss = isinstance(d, VReg) and d in spilled, isinstance(s, VReg) and s in spilled
    if ss and not ds:
        return "load"
    # a 32-bit mov zero-extends; a 32-bit store would leave the slot's upper half stale
    if ds and not ss and (ins.op != "mov" or ins.args[0].w == 64):
        return "store"
    if ds and ss and (ins.op != "mov" or ins.args[0].w == 64):
        return "both"
    return ""


def _rewrite(code, assign, spilled):
    """Physical registers for every virtual one; spill code around instructions that touch a
    spilled register (loaded into a scratch register before, stored back after). A move that
    touches one spilled register reads or writes the slot itself."""
    slot = {v: k for k, v in enumerate(sorted(spilled, key=lambda r: r.id))}
    avx = _avx(code)
    out = []
    for ins in code:
        if not any(isinstance(r, VReg) and r in spilled for r in ins.uses + ins.defs):
            out.append(_subst(ins, assign))
            continue
        kind = _direct(ins, spilled)
        if kind:
            d, s = ins.args
            vec = d.r.cls == "v"
            op = ("vmovdqu" if avx else "movdqu") if vec else "mov"
            w = (256 if avx else 128) if vec else d.w
            if kind == "both":
                t = PHYS[SCRATCH[d.r.cls][0]]
                out.append(Ins(op, [Reg(t, w), Mem(spill=slot[s.r], w=0 if vec else w)], [t], []))
                out.append(Ins(op, [Mem(spill=slot[d.r], w=0 if vec else 64), Reg(t, w)], [], [t]))
            elif kind == "load":
                pd = assign[d.r] if isinstance(d.r, VReg) else d.r
                out.append(Ins(op, [Reg(pd, w), Mem(spill=slot[s.r], w=0 if vec else w)], [pd], []))
            else:
                ps = assign[s.r] if isinstance(s.r, VReg) else s.r
                out.append(Ins(op, [Mem(spill=slot[d.r], w=0 if vec else 64), Reg(ps, w)], [], [ps]))
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
                w = 64 if r.cls == "g" else 256
                mop = "mov" if r.cls == "g" else "vmovdqu" if _avx(code) else "movdqu"
                wv = 64 if r.cls == "g" else (256 if mop == "vmovdqu" else 128)
                if r in ins.uses:
                    pre.append(Ins(mop, [Reg(s, wv), Mem(spill=slot[r], w=0)], [s], []))
                if r in ins.defs:
                    post.append(Ins(mop, [Mem(spill=slot[r], w=0), Reg(s, wv)], [], [s]))
        out += pre + [_subst(ins, local)] + post
    return reloads(out, "mov", "mov", _SPILL_MOVES), len(slot)


_SPILL_MOVES = {"mov", "movdqu", "vmovdqu"}


def _avx(code) -> bool:
    """A VEX function (it ends in vzeroupper): its spills and saves are VEX too."""
    return any(ins.op == "vzeroupper" for ins in code)




def copy_ins(c: VReg, v: VReg, avx: bool) -> Ins:
    """A register copy `c <- v` (a split's copy)."""
    if v.cls == "g":
        return Ins("mov", [Reg(c, 64), Reg(v, 64)], [c], [v])
    w = 256 if avx else 128
    return Ins("vmovdqa" if avx else "movdqa", [Reg(c, w), Reg(v, w)], [c], [v])


def _peephole(code: list) -> list:
    # identity moves go (a 32-bit `mov` stays: it zeroes the upper half). A 128-bit VEX move to
    # itself would zero bits 255:128 too, which no lowering relies on: a 256-bit value is never
    # read after a 128-bit move of the same register; a lowering that did would need it kept.
    out = []
    for i, ins in enumerate(code):
        if ins.op in ("mov", "movdqa", "vmovdqa") and isinstance(ins.args[0], Reg) \
                and isinstance(ins.args[1], Reg) and ins.args[0].r == ins.args[1].r \
                and ins.args[0].w == ins.args[1].w and (ins.args[0].w == 64 or ins.op != "mov"):
            continue
        out.append(ins)
    # flags: an arithmetic on r, then `test r, r` / `cmp r, 0` and a je/jne/ja/jbe, needs no
    # test (the arithmetic sets ZF; after test or cmp 0, ja is jne and jbe is je). A
    # `lea r, [r + k]` there becomes `add r, k` to set them
    fused = []
    k = 0
    while k < len(out):
        ins = out[k]
        if k + 2 < len(out) and _fusible(ins, out[k + 1], out[k + 2]):
            if ins.op == "lea":
                m = ins.args[1]
                ins = Ins("add", [ins.args[0], Imm(m.disp)], ins.defs, ins.uses, ins.clobbers)
            j = out[k + 2]
            op = {"ja": "jne", "jbe": "je"}.get(j.op, j.op)
            fused += [ins, Ins(op, j.args, j.defs, j.uses, j.clobbers)]
            k += 3
            continue
        fused.append(ins)
        k += 1
    out = fused
    # `lea r, [r + k]` before a compare (which writes the flags anyway) as `add r, k`, and
    # `sub r, -128` for k = 128: an 8-bit immediate (4 bytes, against 7 for lea's disp32), as
    # GCC writes a 128-byte loop step
    for k in range(len(out) - 1):
        ins, nxt = out[k], out[k + 1]
        if ins.op == "lea" and nxt.op in ("cmp", "test") and isinstance(ins.args[0], Reg)                 and ins.args[0].w == 64:
            m = ins.args[1]
            if isinstance(m, Mem) and m.base == ins.args[0].r and m.index is None                     and m.sym is None and m.spill is None and m.arg is None and m.disp:
                op, v = ("sub", -m.disp) if m.disp == 128 else ("add", m.disp)
                out[k] = Ins(op, [ins.args[0], Imm(v)], ins.defs, ins.uses, ins.clobbers)
    res = jump_cleanup(out)
    # (with the labels gone, more stores and reloads of a slot share a block)
    return copy_back([ins for ins in reloads(res, "mov", "mov", _SPILL_MOVES) if not _identity(ins)])


def _identity(ins: Ins) -> bool:
    return ins.op == "mov" and len(ins.args) == 2 and all(isinstance(a, Reg) for a in ins.args)         and ins.args[0].r == ins.args[1].r and ins.args[0].w == ins.args[1].w == 64


_SETS_ZF = {"add", "sub", "and", "or", "xor"}


def _fusible(ins: Ins, test: Ins, jcc: Ins) -> bool:
    if jcc.op not in ("je", "jne", "ja", "jbe") or not test.args or not ins.args:
        return False
    if test.op == "test":
        a, b = test.args
        if not (isinstance(a, Reg) and isinstance(b, Reg) and a.r == b.r and a.w == b.w):
            return False
    elif test.op == "cmp":
        a, b = test.args
        if not (isinstance(a, Reg) and isinstance(b, Imm) and b.v == 0):
            return False
    else:
        return False
    d = ins.args[0]
    if not (isinstance(d, Reg) and d.r == a.r):
        return False
    # a 32-bit operation zero-extends: its ZF is that of the whole 64-bit register
    if ins.op in _SETS_ZF and (d.w == a.w or (d.w == 32 and a.w == 64)):
        return True
    if d.w != a.w:
        return False
    if ins.op == "lea" and a.w == 64:            # lea r, [r + k] -> add r, k
        m = ins.args[1]
        return isinstance(m, Mem) and m.base == a.r and m.index is None and m.sym is None \
            and m.spill is None and m.arg is None
    return False


def _frame(code: list, abi: ABI, nslots: int) -> list:
    used = {r for ins in code for r in ins.defs if isinstance(r, Phys)}
    saved = [PHYS[r] for r in abi.callee_saved if PHYS[r] in used]
    vsaved = [PHYS[r] for r in abi.vec_callee_saved if PHYS[r] in used]
    calls = any(ins.op in ("call", "callind") for ins in code)
    shadow = abi.shadow if calls else 0
    # outgoing stack arguments ([rsp + shadow + 8k] at the call) sit below the spill slots
    out = max([a.disp + 8 for ins in code for a in ins.args
               if isinstance(a, Mem) and a.base == RSP], default=0)
    spill_off = max(shadow, out)
    # the xmm saves at a multiple of 16 from rsp: Win64 unwind info stores the offset / 16
    vsave_off = -(-(spill_off + 32 * nslots) // 16) * 16
    size = vsave_off + 16 * len(vsaved)
    npush = len(saved)
    # rsp is 16-byte aligned at every call this function makes (and its frame stays aligned)
    if (calls or size) and (8 + 8 * npush + size) % 16:
        size += 8
    if size >= 4096:
        # Win64 needs a stack probe (__chkstk) to grow past a guard page, and the unwind info
        # here covers frames under 512 KiB: no function comes near; say so if one ever does
        raise NotImplementedError(f"a {size}-byte frame: stack probes are not generated")

    def fix(a):
        if isinstance(a, Mem) and a.spill is not None:
            return Mem(_RSP, disp=spill_off + 32 * a.spill, w=a.w)
        if isinstance(a, Mem) and a.arg is not None:
            return Mem(_RSP, disp=size + 8 * npush + abi.stack_arg_offset(a.arg), w=a.w)
        return a

    body = [Ins(i.op, [fix(a) for a in i.args], i.defs, i.uses, i.clobbers, i.hint) for i in code]
    # the prologue, each step marked for the unwind info (Win64 .pdata/.xdata)
    pro = [Ins("push", [Reg(r, 64)], hint="push") for r in saved]
    if size:
        pro.append(Ins("sub", [Reg(_RSP, 64), Imm(size)], hint="alloc"))
    mv = "vmovdqu" if _avx(code) else "movdqu"      # no legacy SSE inside an AVX function
    for k, r in enumerate(vsaved):
        pro.append(Ins(mv, [Mem(_RSP, disp=vsave_off + 16 * k, w=0), Reg(r, 128)], hint="savexmm"))
    if pro:
        pro.append(Ins("endprolog", []))
    epi = []
    for k, r in enumerate(vsaved):
        epi.append(Ins(mv, [Reg(r, 128), Mem(_RSP, disp=vsave_off + 16 * k, w=0)]))
    if size:
        epi.append(Ins("add", [Reg(_RSP, 64), Imm(size)], hint="unalloc"))
    epi += [Ins("pop", [Reg(r, 64)], hint="pop") for r in reversed(saved)]
    out = pro
    for ins in body:
        if ins.op in ("ret", "tailcall"):     # a tail call leaves through the epilogue too
            out += epi
        out.append(ins)
    return out


_RSP = RSP

MOVES = _MOVES
direct = _direct
rewrite = _rewrite
peephole = _peephole
frame = _frame
avx = _avx
