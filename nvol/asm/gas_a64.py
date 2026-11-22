"""GNU as printer for A64 (aarch64 ELF, AAPCS64): the machine IR of `a64.py`.

The MIR keeps the destination first; a store (`str`, `strb`, `stp`) has its memory operand first
and is printed with it last. Registers: `xN`/`wN` by the width of the operand, `qN`/`dN`/`bN`
for a vector register used as a scalar, and `vN.<arrangement>` in vector operations, where the
arrangement of each operand comes from `_VOPS`. Labels and symbols follow `gas.py`: `.L` labels
prefixed with their function, a local alias `L.name` for every global, `.hidden` for the
non-public ones, and `.size`."""
from __future__ import annotations

from .a64 import COND, JCC, align_pad, code_size, length, mov_parts
from .mir import Imm, Lab, Mem, Reg

# vector op -> (mnemonic, arrangement of each register operand)
_VOPS = {
    "vcmeq": ("cmeq", "16b", "16b", "16b"),
    "vorr": ("orr", "16b", "16b", "16b"),
    "vand": ("and", "16b", "16b", "16b"),
    "veor": ("eor", "16b", "16b", "16b"),
    "vbic": ("bic", "16b", "16b", "16b"),
    "vsub": ("sub", "16b", "16b", "16b"),
    "vuqsub": ("uqsub", "16b", "16b", "16b"),
    "vadd": ("add", "16b", "16b", "16b"),
    "vumin": ("umin", "16b", "16b", "16b"),
    "vumaxp": ("umaxp", "16b", "16b", "16b"),
    "vadd2d": ("add", "2d", "2d", "2d"),
    "vmov": ("mov", "16b", "16b"),
    "vushr": ("ushr", "16b", "16b"),
    "vsshr": ("sshr", "16b", "16b"),
    "vext": ("ext", "16b", "16b", "16b"),
    "vshrn": ("shrn", "8b", "8h"),
    "vuaddlp8h": ("uaddlp", "8h", "16b"),
    "vuaddlp4s": ("uaddlp", "4s", "8h"),
    "vuaddlp2d": ("uaddlp", "2d", "4s"),
    "vcnt8b": ("cnt", "8b", "8b"),
    "movi": ("movi", "16b"),
    "dup": ("dup", "16b", None),
    "dup2d": ("dup", "2d", None),
    "vaddp": ("addp", "16b", "16b", "16b"),
}
_SCALAR_V = {8: "b", 16: "h", 32: "s", 64: "d", 128: "q"}


def reg(r, w: int) -> str:
    n = r.name
    if n == "sp":
        return "sp" if w == 64 else "wsp"
    if n == "xzr":
        return "xzr" if w == 64 else "wzr"
    if r.cls == "v":
        return _SCALAR_V[w] + n[1:]
    return ("x" if w == 64 else "w") + n[1:]


def vreg(r, arr: str) -> str:
    return f"v{r.name[1:]}.{arr}"


class Printer:
    def __init__(self, globals_: set = frozenset()):
        self.globals = set(globals_)
        self.fn = ""

    def lab(self, name: str) -> str:
        if name.startswith("."):
            return f".L{self.fn}{name}"
        return self.sym(name)

    def sym(self, name: str) -> str:
        return f"L.{name}" if name in self.globals else name

    def mem(self, m: Mem, index: str = None) -> str:
        b = reg(m.base, 64)
        if index == "pre":               # writeback before the access: [sp, #-32]!
            return f"[{b}, #{m.disp}]!"
        if index == "post":              # writeback after it: [sp], #32
            return f"[{b}], #{m.disp}"
        if m.index is not None:
            sh = {1: "", 2: ", lsl #1", 4: ", lsl #2", 8: ", lsl #3"}[m.scale]
            return f"[{b}, {reg(m.index, 64)}{sh}]"
        return f"[{b}, #{m.disp}]" if m.disp else f"[{b}]"

    def operand(self, a) -> str:
        if isinstance(a, Reg):
            return reg(a.r, a.w)
        if isinstance(a, Imm):
            return f"#{a.v}"
        if isinstance(a, Lab):
            return self.lab(a.name)
        if isinstance(a, Mem):
            return self.mem(a)
        if isinstance(a, str):
            return a
        raise TypeError(a)

    def ins(self, ins) -> list:
        op, a = ins.op, ins.args
        if op in JCC:
            return [f"    b.{JCC[op]} {self.lab(a[0].name)}"]
        if op in ("cbz", "cbnz"):
            return [f"    {op} {self.operand(a[1])}, {self.lab(a[0].name)}"]
        if op in ("tbz", "tbnz"):
            return [f"    {op} {self.operand(a[1])}, #{a[2].v}, {self.lab(a[0].name)}"]
        if op in ("jmp", "tailjmp"):
            return [f"    b {self.lab(a[0].name)}"]
        if op == "tailcall":
            return [f"    b {self.lab(a[0].name)}"] if isinstance(a[0], Lab) else [f"    br {self.operand(a[0])}"]
        if op == "call":
            return [f"    bl {self.lab(a[0].name)}"]
        if op == "callind":
            return [f"    blr {self.operand(a[0])}"]
        if op == "ret":
            return ["    ret"]
        if op == "cfi":
            return [a[0]]
        if op == "movimm":
            d, w = self.operand(a[0]), a[0].w
            out = []
            for p in mov_parts(a[1].v, w):
                if p[0] == "orr":
                    out.append(f"    orr {d}, {'xzr' if w == 64 else 'wzr'}, #{p[1]}")
                else:
                    out.append(f"    {p[0]} {d}, #{p[1]}" + (f", lsl #{p[2]}" if p[2] else ""))
            return out
        if op == "adrl":
            d, s = self.operand(a[0]), self.sym(a[1].sym)
            return [f"    adrp {d}, {s}", f"    add {d}, {d}, :lo12:{s}"]
        if op in ("str", "strb", "strh"):
            return [f"    {op if ins.hint else _mem_op(op, a[0])} {self.operand(a[1])}, {self.mem(a[0], ins.hint)}"]
        if op in ("ldr", "ldrb", "ldrh", "ldrsw"):
            return [f"    {op if ins.hint else _mem_op(op, a[1])} {self.operand(a[0])}, {self.mem(a[1], ins.hint)}"]
        if op == "stp":
            return [f"    stp {self.operand(a[1])}, {self.operand(a[2])}, {self.mem(a[0], ins.hint)}"]
        if op == "ldp":
            return [f"    ldp {self.operand(a[0])}, {self.operand(a[1])}, {self.mem(a[2], ins.hint)}"]
        if op in ("add", "sub") and isinstance(a[2], Imm) and a[2].v >= 4096:
            return [f"    {op} {self.operand(a[0])}, {self.operand(a[1])}, #{a[2].v >> 12}, lsl #12"]
        if op == "csinv":
            return [f"    csinv {self.operand(a[0])}, {self.operand(a[1])}, {self.operand(a[2])}, {a[3]}"]
        if op == "csel":
            return [f"    csel {self.operand(a[0])}, {self.operand(a[1])}, {self.operand(a[2])}, {a[3]}"]
        if op == "cset":
            return [f"    cset {self.operand(a[0])}, {a[1]}"]
        if op == "vtbl":
            return [f"    tbl {vreg(a[0].r, '16b')}, {{{vreg(a[1].r, '16b')}}}, {vreg(a[2].r, '16b')}"]
        if op == "fmovdx":
            return [f"    fmov d{a[0].r.name[1:]}, {reg(a[1].r, 64)}"]
        if op == "fmovxd":
            return [f"    fmov {reg(a[0].r, 64)}, d{a[1].r.name[1:]}"]
        if op == "vaddvb":
            return [f"    addv b{a[0].r.name[1:]}, {vreg(a[1].r, '8b')}"]
        if op == "umovb":
            return [f"    umov {reg(a[0].r, 32)}, {vreg(a[1].r, 'b')}[0]"]
        if op == "umovd":
            return [f"    umov {reg(a[0].r, 64)}, {vreg(a[1].r, 'd')}[{a[2].v}]"]
        if op == "vaddpd":
            return [f"    addp d{a[0].r.name[1:]}, {vreg(a[1].r, '2d')}"]
        if op in _VOPS:
            mn, *arr = _VOPS[op]
            parts = []
            for k, x in enumerate(a):
                if isinstance(x, Reg) and k < len(arr) and arr[k] is not None:
                    parts.append(vreg(x.r, arr[k]))
                else:
                    parts.append(self.operand(x))
            return [f"    {mn} " + ", ".join(parts)]
        return [f"    {op} " + ", ".join(self.operand(x) for x in a)]

    def function(self, name: str, code: list, public: bool, export_all: bool = False) -> list:
        self.fn = name
        out = [f".globl {name}", f".type {name}, %function"]
        if not (public or export_all):
            out.append(f".hidden {name}")
        out += [f"{name}:", f"L.{name}:", ".cfi_startproc"]
        off = 0
        for ins in code:
            if ins.op == "label":
                out.append(f"{self.lab(ins.args[0].name)}:")
            elif ins.op == "align":
                pad = align_pad(off, ins)
                out += ["    nop"] * (pad // 4)
                off += pad
            elif ins.op in ("coldbegin", "coldend", "rarebegin", "rareend", "endprolog"):
                pass
            else:
                lines = self.ins(ins)
                out += lines
                off += length(ins)
        out += [".cfi_endproc", f".size {name}, .-{name}"]
        return out


def _mem_op(op: str, m: Mem) -> str:
    """ldur/stur when the offset is negative or not a multiple of the access size (a pre- or
    post-indexed access stays ldr/str: the unscaled forms have no writeback)."""
    if m.index is None and m.disp:
        size = max(1, m.w // 8) if m.w else 8
        if m.disp < 0 or m.disp % size:
            return {"ldr": "ldur", "str": "stur", "ldrb": "ldurb", "strb": "sturb",
                    "ldrh": "ldurh", "strh": "sturh", "ldrsw": "ldursw"}[op]
    return op


def module(functions: list, fmt: str = "elf64", consts: dict = None, tables: dict = None,
           fn_tables: dict = None, ptrs: dict = None, export_all: bool = False) -> str:
    """Same arguments as `gas.module` (ELF only; aarch64 dispatches nothing, so no `ptrs`)."""
    assert not ptrs, "aarch64 has no ifunc pointers"
    pr = Printer({n for n, _, _ in functions})
    lines = ["// Generated by NVOL (Native Vector Operations Library). Do not edit.",
             "// SPDX-License-Identifier: MIT OR Apache-2.0"]
    if consts or tables:
        lines.append(".section .rodata")
        for data, lab in (consts or {}).items():
            lines += [".p2align 5", f"{lab}:", "    .byte " + ", ".join(str(b) for b in data)]
        for name, data in (tables or {}).items():
            lines += [".p2align 6", f"nvol_tbl_{name}:"]
            lines += ["    .byte " + ", ".join(str(b) for b in data[i:i + 16])
                      for i in range(0, len(data), 16)]
    lines.append('.section .note.GNU-stack,"",@progbits')
    lines += [".text", ".p2align 4"]
    off = 0
    for name, code, public in functions:
        lines.append("")
        pad = (-off) % 16
        lines += ["    nop"] * (pad // 4)
        lines += pr.function(name, code, public, export_all)
        off += pad + code_size(code)
    for name, fns in (fn_tables or {}).items():
        lines += ["", ".p2align 3", f"nvol_fntbl_{name}:"]
        lines += [f"    .word {pr.sym(fn)} - nvol_fntbl_{name}" for fn in fns]
    return "\n".join(lines) + "\n"
