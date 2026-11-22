"""NASM printer: Intel syntax, `default rel`.

ELF shared objects: a global symbol can be interposed, so a direct reference to it cannot be
resolved at link time. Every global symbol of the module gets a local alias (`L.name`) right
after its label, and the module's own code references the alias; symbols defined elsewhere (the
C glue's CPU checks) are called through the PLT. Non-public symbols are `hidden`.
Win64: public functions are exported with `-export:` directives in `.drectve`, as MSVC does for
`__declspec(dllexport)`."""
from __future__ import annotations

from . import encode
from .mir import Imm, Lab, Mem, Phys, Reg

_G = {
    "rax": ("rax", "eax", "ax", "al"), "rbx": ("rbx", "ebx", "bx", "bl"),
    "rcx": ("rcx", "ecx", "cx", "cl"), "rdx": ("rdx", "edx", "dx", "dl"),
    "rsi": ("rsi", "esi", "si", "sil"), "rdi": ("rdi", "edi", "di", "dil"),
    "rbp": ("rbp", "ebp", "bp", "bpl"), "rsp": ("rsp", "esp", "sp", "spl"),
}
for _i in range(8, 16):
    _G[f"r{_i}"] = (f"r{_i}", f"r{_i}d", f"r{_i}w", f"r{_i}b")
_W = {64: 0, 32: 1, 16: 2, 8: 3}
_SIZE = {8: "byte", 16: "word", 32: "dword", 64: "qword", 128: "oword", 256: "yword"}
# what each pseudo-op prints as
_OPS = {"trunc32": "mov", "callind": "call", "tailjmp": "jmp", "tailcall": "jmp"}


def reg(r: Phys, w: int) -> str:
    if r.cls == "v":
        return ("ymm" if w == 256 else "xmm") + r.name[3:]
    return _G[r.name][_W[w]]


class Printer:
    def __init__(self, fmt: str, globals_: set = frozenset()):
        self.fmt = fmt
        self.globals = set(globals_)

    def sym(self, name: str, call: bool = False) -> str:
        if self.fmt != "elf64" or name.startswith("."):
            return name
        if name in self.globals:
            return f"L.{name}"
        if call and name.startswith("nvol_cpu_"):
            return f"{name} wrt ..plt"
        return name

    def operand(self, a, call: bool = False) -> str:
        if isinstance(a, Reg):
            return reg(a.r, a.w)
        if isinstance(a, Imm):
            return str(a.v)
        if isinstance(a, Lab):
            return self.sym(a.name, call)
        if isinstance(a, Mem):
            size = (_SIZE[a.w] + " ") if a.w else ""
            if a.sym is not None:
                d = f" + {a.disp}" if a.disp else ""
                return f"{size}[rel {self.sym(a.sym)}{d}]"
            parts = []
            if a.base is not None:
                parts.append(reg(a.base, 64))
            if a.index is not None:
                parts.append(reg(a.index, 64) + (f"*{a.scale}" if a.scale != 1 else ""))
            s = " + ".join(parts)
            if a.disp:
                s += f" - {-a.disp}" if a.disp < 0 else f" + {a.disp}"
            return f"{size}[{s}]"
        raise TypeError(a)

    def label(self, name: str, kind: str, public: bool, export_all: bool) -> list:
        out = []
        if self.fmt == "elf64":
            vis = "" if public or export_all else " hidden"
            # the size (from the alias to the end label): profilers attribute samples by it
            size = f" (L.{name}.cfi_end - {name})" if kind == "function" else ""
            out.append(f"global {name}:{kind}{vis}{size}")
            out += [f"{name}:", f"L.{name}:"]
        else:
            out += [f"global {name}", f"{name}:"]
        return out

    def function(self, name: str, code: list, public: bool, export_all: bool = False) -> list:
        out = self.label(name, "function", public, export_all)
        for ins in code:
            if ins.op == "label":
                out.append(f"{ins.args[0].name}:")
                continue
            if ins.op == "endprolog":    # the unwind tables are written after the code
                continue
            if ins.op == "align":
                out += [f"    db {', '.join(hex(b) for b in nop)}" for nop in encode.nops(ins.args[2].v)]
                continue
            op = _OPS.get(ins.op, ins.op)
            raw = encode.vex_store_form(ins)
            if raw:          # the 2-byte-VEX store form, which NASM does not pick by itself
                out.append(f"    db {', '.join(hex(b) for b in raw)}    ; {op} "
                           + ", ".join(self.operand(a) for a in ins.args))
                continue
            call = ins.op in ("call", "tailjmp", "tailcall")
            args = ", ".join(self.operand(a, call) for a in ins.args)
            if ins.hint in ("short", "near"):    # encode.relax's size: NASM does not always find it
                args = f"strict {ins.hint} {args}"      # (without strict, -Ox may still shrink a near)
            out.append(f"    {op} {args}".rstrip())
        if self.fmt == "win64" and encode.unwind(code):
            out.append(".unwind_end:")
        if self.fmt == "elf64":
            out.append(".cfi_end:")      # the FDE's address range ends here
        return out


def function(name: str, code: list, public: bool = True) -> list:
    return Printer("win64").function(name, code, public)


def module(functions: list, fmt: str, consts: dict = None, tables: dict = None,
           fn_tables: dict = None, ptrs: dict = None, export_all: bool = False) -> str:
    """functions: [(name, code, public)]; consts: {bytes: label} (read-only vectors); tables:
    {name: bytes} (read-only byte tables, `nvol_tbl_<name>`); fn_tables: {name: [functions]}
    (`nvol_fntbl_<name>`: 32-bit offsets from the table, in .text); ptrs: {pointer: initial
    target} (writable function pointers, the ifunc caches). fmt: "win64" or "elf64"."""
    ptrs = ptrs or {}
    pr = Printer(fmt, {n for n, _, _ in functions} | set(ptrs))
    lines = ["; Generated by NVOL (Native Vector Operations Library). Do not edit.",
             "; SPDX-License-Identifier: MIT OR Apache-2.0",
             "default rel"]
    externs = sorted({a.name for _, code, _ in functions for ins in code if ins.op != "label"
                      for a in ins.args if isinstance(a, Lab) and not a.name.startswith(".")
                      and a.name not in pr.globals})
    lines += [f"extern {n}" for n in externs]
    if consts or tables:
        lines.append("section .rdata" if fmt == "win64" else "section .rodata")
        for data, lab in (consts or {}).items():
            lines.append("align 32, db 0")     # zeros: NASM pads with nops by default
            lines.append(f"{lab}: db " + ", ".join(str(b) for b in data))
        for name, data in (tables or {}).items():
            lines.append("align 64, db 0")
            lines.append(f"nvol_tbl_{name}:")
            for i in range(0, len(data), 16):
                lines.append("    db " + ", ".join(str(b) for b in data[i:i + 16]))
    if ptrs:
        lines.append("section .data")
        for p, target in ptrs.items():
            lines.append("align 8, db 0")
            lines += pr.label(p, "data", False, export_all)
            lines.append(f"    dq {pr.sym(target)}")
    if fmt == "elf64":
        lines.append('section .note.GNU-stack noalloc noexec nowrite progbits')
    exports = [n for n, _, public in functions if public or export_all]
    if fmt == "win64" and exports:
        lines.append("section .drectve info")
        for n in exports:
            lines.append(f'    db "-export:{n} "')
    lines.append("section .text align=64")
    off = 0
    for name, code, public in functions:
        lines.append("")
        pad = (-off) % encode.FUNC_ALIGN   # every function 64-aligned, with NVOL's own NOP bytes
        lines += [f"    db {', '.join(hex(b) for b in nop)}" for nop in encode.nops(pad)]
        lines += pr.function(name, code, public, export_all)
        off += pad + encode.code_size(code)
    for name, fns in (fn_tables or {}).items():
        lines += ["", "align 8", f"nvol_fntbl_{name}:"]
        lines += [f"    dd {pr.sym(fn)} - nvol_fntbl_{name}" for fn in fns]
    if fmt == "win64":
        lines += _unwind_tables(functions)
    else:
        lines += _eh_frame(functions)
    return "\n".join(lines) + "\n"


def _eh_frame(functions: list) -> list:
    """System V .eh_frame: the CIE, then one FDE per function with the steps of encode.cfi,
    the layout GNU as gives its `.cfi_*` (NASM has no CFI directives). The function's address
    is pc-relative (`L.name - $`, R_X86_64_PC32 against the local alias)."""
    out = ["", "section .eh_frame progbits alloc noexec nowrite align=8",
           "nvol_cie:", "    db " + ", ".join(str(b) for b in encode.CIE)]
    total = len(encode.CIE)
    for k, (name, code, _) in enumerate(functions):
        prog = encode.fde_program(encode.cfi(code))
        n = encode.fde_length(prog)
        if k == len(functions) - 1:     # GNU as pads the last FDE to end the section 8-aligned
            n += (-(total + 4 + n)) % 8
        total += 4 + n
        pad = n - (4 + 4 + 4 + 1 + len(prog))
        # one field per line: in a `dd` with several values `$` is the start of the line
        out += [f"    dd {n}", "    dd $ - nvol_cie", f"    dd L.{name} - $",
                f"    dd L.{name}.cfi_end - L.{name}",
                "    db " + ", ".join(str(b) for b in bytes([0]) + prog + bytes(pad))]
    return out


def _unwind_tables(functions: list) -> list:
    """Win64 .xdata (UNWIND_INFO, from encode.unwind) and .pdata (begin, end, unwind info) of
    every function with a prologue: NASM has no SEH directives, so the tables are data."""
    infos = [(name, encode.unwind(code)) for name, code, _ in functions]
    infos = [(n, i) for n, i in infos if i]
    if not infos:
        return []
    out = ["", "section .xdata rdata align=4"]
    for name, info in infos:
        out += ["align 4, db 0", f"nvol_unwind_{name}:",
                "    db " + ", ".join(str(b) for b in encode.xdata(info))]
    out += ["", "section .pdata rdata align=4"]
    for name, _ in infos:
        out.append(f"    dd {name} wrt ..imagebase, {name}.unwind_end wrt ..imagebase, "
                   f"nvol_unwind_{name} wrt ..imagebase")
    return out
