"""GNU as printer, AT&T syntax: the same machine IR as `nasm.py` and `masm.py`.

AT&T against the Intel order of the MIR:
- operands in reverse order (source first, destination last), registers with `%`, immediates
  with `$`, memory as `disp(base,index,scale)` and RIP-relative as `sym+disp(%rip)`;
- the operand size is in the mnemonic only where no register fixes it (a store of an
  immediate, `jmp *mem`); zero and sign extensions are `movzbl`/`movzwl`/`movslq`;
- an indirect jump or call takes `*`; a 64-bit immediate that does not fit in 32 bits is
  `movabsq`.

`.L` labels are local to the file, not to a function, so each one is prefixed with its
function's name. Object formats: COFF (Win64, `-export:` in `.drectve` as in the other syntaxes)
and ELF (System V: `.type`, `.hidden` for non-public symbols, a local alias `L.name` for every
global, and the PLT for the C glue's CPU checks)."""
from __future__ import annotations

from . import encode
from .mir import Imm, Lab, Mem, Reg
from .nasm import reg

_SUFFIX = {8: "b", 16: "w", 32: "l", 64: "q"}
_OPS = {"trunc32": "mov", "callind": "call", "tailjmp": "jmp", "tailcall": "jmp"}


class Printer:
    def __init__(self, fmt: str, globals_: set = frozenset()):
        self.fmt = fmt
        self.globals = set(globals_)
        self.fn = ""

    def lab(self, name: str) -> str:
        if name.startswith("."):
            return f".L{self.fn}{name}"
        return self.sym(name)

    def sym(self, name: str, call: bool = False) -> str:
        if self.fmt != "elf64":
            return name
        if name in self.globals:
            return f"L.{name}"
        if call and name.startswith("nvol_cpu_"):
            return f"{name}@PLT"
        return name

    def operand(self, a, call: bool = False) -> str:
        if isinstance(a, Reg):
            return "%" + reg(a.r, a.w)
        if isinstance(a, Imm):
            return f"${a.v}"
        if isinstance(a, Lab):
            return self.lab(a.name) if not call else (self.lab(a.name) if a.name.startswith(".")
                                                      else self.sym(a.name, True))
        if isinstance(a, Mem):
            if a.sym is not None:
                d = f"+{a.disp}" if a.disp else ""
                return f"{self.sym(a.sym)}{d}(%rip)"
            inner = "%" + reg(a.base, 64) if a.base is not None else ""
            if a.index is not None:
                inner += ",%" + reg(a.index, 64) + (f",{a.scale}" if a.scale != 1 else "")
            return f"{a.disp if a.disp else ''}({inner})"
        raise TypeError(a)

    def ins(self, ins) -> str:
        op, args = _OPS.get(ins.op, ins.op), ins.args
        if ins.op in ("callind", "tailjmp", "tailcall") and isinstance(args[0], (Reg, Mem)):
            return f"    {op} *{self.operand(args[0])}"
        if ins.op in ("call", "tailjmp", "jmp", "tailcall") or op.startswith("j"):
            # the size encode.relax decided: GNU as would shrink a jump that NVOL kept near for
            # its alignment padding (it sees the padding as fixed bytes)
            size = {"short": "{disp8} ", "near": "{disp32} "}.get(ins.hint, "")
            return f"    {size}{op} {self.operand(args[0], call=True)}"
        if op == "movzx":
            src = args[1]
            w = src.w if isinstance(src, (Reg, Mem)) else 8
            op = "movz" + _SUFFIX[w] + _SUFFIX[args[0].w]
        elif op == "movsxd":
            op = "movslq"
        elif op == "mov" and isinstance(args[1], Imm) and isinstance(args[0], Reg) \
                and args[0].w == 64 and not -(1 << 31) <= _signed(args[1].v) < (1 << 31):
            op = "movabsq"
        elif any(isinstance(a, Mem) for a in args) and not any(isinstance(a, Reg) for a in args):
            m = next(a for a in args if isinstance(a, Mem))
            if m.w in _SUFFIX:
                op += _SUFFIX[m.w]
        text = ", ".join(self.operand(a) for a in reversed(args))
        return f"    {op} {text}".rstrip()

    def label(self, name: str, kind: str, public: bool, export_all: bool) -> list:
        out = [f".globl {name}"]
        if self.fmt == "elf64":
            out.append(f".type {name}, @{kind}")
            if not (public or export_all):
                out.append(f".hidden {name}")
            out += [f"{name}:", f"L.{name}:"]
        else:
            if kind == "function":
                out.append(f".def {name}; .scl 2; .type 32; .endef")
            out.append(f"{name}:")
        return out

    def function(self, name: str, code: list, public: bool, export_all: bool = False) -> list:
        self.fn = name
        out = self.label(name, "function", public, export_all)
        seh = self.fmt == "win64" and encode.unwind(code)    # .seh_*: GNU as writes the tables
        if seh:
            out.append(f".seh_proc {name}")
        # ELF: .cfi_* at the offsets encode.cfi gives (the same steps NASM writes as .eh_frame)
        steps = encode.cfi(code) if self.fmt == "elf64" else None
        if steps is not None:
            out.append(".cfi_startproc")
        si, off = 0, 0
        for ins in code:
            if ins.op not in ("label", "endprolog"):
                while steps and si < len(steps) and steps[si][0] == off:
                    out.append(_cfi(steps[si][1]))
                    si += 1
                off += encode.length(ins)
            if ins.op == "label":
                out.append(f"{self.lab(ins.args[0].name)}:")
            elif ins.op == "endprolog":
                if seh:
                    out.append(".seh_endprologue")
            elif ins.op == "align":
                out += ["    .byte " + ", ".join(str(b) for b in nop) for nop in encode.nops(ins.args[2].v)]
            else:
                out.append(self.ins(ins))
                if seh and ins.hint == "push":
                    out.append(f".seh_pushreg {self.operand(ins.args[0])}")
                elif seh and ins.hint == "alloc":
                    out.append(f".seh_stackalloc {ins.args[1].v}")
                elif seh and ins.hint == "savexmm":
                    out.append(f".seh_savexmm %{reg(ins.args[1].r, 128)}, {ins.args[0].disp}")
        while steps and si < len(steps):
            out.append(_cfi(steps[si][1]))
            si += 1
        if steps is not None:
            out.append(".cfi_endproc")
        if seh:
            out.append(".seh_endproc")
        if self.fmt == "elf64":
            out.append(f".size {name}, .-{name}")
        return out


def _cfi(step) -> str:
    if step[0] == "def_cfa_offset":
        return f".cfi_def_cfa_offset {step[1]}"
    if step[0] == "offset":
        return f".cfi_offset %{step[1]}, -{step[2]}"
    return ".cfi_remember_state" if step[0] == "remember" else ".cfi_restore_state"


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def module(functions: list, fmt: str, consts: dict = None, tables: dict = None,
           fn_tables: dict = None, ptrs: dict = None, export_all: bool = False) -> str:
    """Same arguments as `nasm.module`."""
    ptrs = ptrs or {}
    pr = Printer(fmt, {n for n, _, _ in functions} | set(ptrs))
    lines = ["# Generated by NVOL (Native Vector Operations Library). Do not edit.",
             "# SPDX-License-Identifier: MIT OR Apache-2.0"]
    if consts or tables:
        lines.append('.section .rdata,"dr"' if fmt == "win64" else ".section .rodata")
        for data, lab in (consts or {}).items():
            lines += [".p2align 5", f"{lab}:", "    .byte " + ", ".join(str(b) for b in data)]
        for name, data in (tables or {}).items():
            lines += [".p2align 6", f"nvol_tbl_{name}:"]
            lines += ["    .byte " + ", ".join(str(b) for b in data[i:i + 16])
                      for i in range(0, len(data), 16)]
    if ptrs:
        lines.append(".data")
        for p, target in ptrs.items():
            lines.append(".p2align 3")
            lines += pr.label(p, "object", False, export_all)
            lines.append(f"    .quad {pr.sym(target)}")
    if fmt == "elf64":
        lines.append('.section .note.GNU-stack,"",@progbits')
    exports = [n for n, _, public in functions if public or export_all]
    if fmt == "win64" and exports:
        lines.append('.section .drectve')
        lines += [f'    .ascii " -export:{n}"' for n in exports]
    lines += [".text", ".p2align 4"]
    off = 0
    for name, code, public in functions:
        lines.append("")
        pad = (-off) % 16
        lines += ["    .byte " + ", ".join(str(b) for b in nop) for nop in encode.nops(pad)]
        lines += pr.function(name, code, public, export_all)
        off += pad + encode.code_size(code)
    for name, fns in (fn_tables or {}).items():
        lines += ["", ".p2align 3", f"nvol_fntbl_{name}:"]
        lines += [f"    .long {pr.sym(fn)} - nvol_fntbl_{name}" for fn in fns]
    return "\n".join(lines) + "\n"
