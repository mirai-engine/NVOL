"""Assembly backends: the IR lowered to x86-64 machine code by NVOL itself, with no compiler in
between. `lower` (IR -> machine IR), `regalloc` (registers, stack frame) and `abi` (System V,
Win64) are shared; `nasm`, `masm` and `gas` (GNU as, AT&T) print it. Every syntax prints the
same machine IR, so they are one implementation, not three.

A library is every IR function of the module (kernels, entries, public functions), the ifunc
dispatchers and their resolvers, and the tables. The C glue (`c.py` with `asm_kernels=True`)
provides the CPU checks the resolvers call and everything that is not IR."""
from __future__ import annotations

from .. import ir
from . import encode, gas, masm, nasm, x86
from .abi import ABIS
from .lower import Pool, lower
from .mir import PHYS, RSP, Imm, Ins, Lab, Mem, Reg
from .regalloc import allocate


def compile_function(f, abi_name: str, pool: Pool = None, mod: ir.Module = None) -> list:
    """Lowered, allocated, and every jump sized (encode.relax)."""
    return encode.relax(allocate(lower(f, ABIS[abi_name], pool, mod), ABIS[abi_name], x86))


def fmt_of(abi_name: str) -> str:
    return "win64" if abi_name == "win64" else "elf64"


PRINTERS = {"nasm": nasm, "masm": masm, "gas": gas}
# the ABIs each syntax's assembler targets, and the file extension
SYNTAX_ABIS = {"nasm": ("win64", "sysv"), "masm": ("win64",), "gas": ("win64", "sysv")}
SYNTAX_EXT = {"nasm": ".asm", "masm": ".asm", "gas": ".s"}


def asm_module(functions: list, abi_name: str, mod: ir.Module = None, syntax: str = "nasm") -> str:
    """Assembly text for the given IR functions alone (all exported; calls need `mod`)."""
    pool = Pool()
    code = [(f.name, compile_function(f, abi_name, pool, mod), True) for f in functions]
    return PRINTERS[syntax].module(code, fmt_of(abi_name), pool.consts,
                                   mod.tables if mod else None, export_all=True)


def nasm_module(functions: list, abi_name: str, mod: ir.Module = None) -> str:
    return asm_module(functions, abi_name, mod, "nasm")


def resolver(f: ir.Function, abi_name: str) -> list:
    """f's ifunc resolver: the first target whose CPU check holds (the C glue's cached
    `nvol_cpu_*`), stored in f's pointer, then a jump to it with the arguments untouched."""
    abi = ABIS[abi_name]
    regs = [PHYS[r] for r in abi.args]
    # the argument registers, then rsp 16-byte aligned with the shadow space below it
    pad = abi.shadow
    while (8 + 8 * len(regs) + pad) % 16:
        pad += 8
    rax = PHYS["rax"]
    code = [Ins("push", [Reg(r, 64)], hint="push") for r in regs]
    code += [Ins("sub", [Reg(RSP, 64), Imm(pad)], hint="alloc"), Ins("endprolog", [])]
    conds = [(c, t) for c, t in f.ifunc if c is not None]
    default = [t for c, t in f.ifunc if c is None][0]
    for k, (c, t) in enumerate(conds):
        assert isinstance(c, ir.Prim) and c.name.startswith("cpu_"), (f.name, c)
        code += [Ins("call", [Lab(f"nvol_{c.name}")]),
                 Ins("test", [Reg(rax, 32), Reg(rax, 32)]),
                 Ins("jne", [Lab(f".T{k}")])]
    done = Lab(".done")
    code += [Ins("lea", [Reg(rax, 64), Mem(sym=default)]), Ins("jmp", [done])]
    for k, (c, t) in enumerate(conds):
        code += [Ins("label", [Lab(f".T{k}")]), Ins("lea", [Reg(rax, 64), Mem(sym=t)]),
                 Ins("jmp", [done])]
    code += [Ins("label", [done]), Ins("mov", [Mem(sym=f"{f.name}_ptr", w=64), Reg(rax, 64)]),
             Ins("add", [Reg(RSP, 64), Imm(pad)], hint="unalloc")]
    code += [Ins("pop", [Reg(r, 64)], hint="pop") for r in reversed(regs)]
    # through the pointer, not `jmp rax`: an epilogue the Win64 unwinder recognizes
    code.append(Ins("tailjmp", [Mem(sym=f"{f.name}_ptr", w=64)]))
    return encode.relax(code)


def library(mod: ir.Module, abi_name: str, syntax: str = "nasm", export_all: bool = False) -> str:
    """The whole module as one assembly file. export_all: every function a dynamic symbol
    (tests call the kernels one by one), not only the public ones."""
    assert abi_name in SYNTAX_ABIS[syntax], (syntax, abi_name)
    pool = Pool()
    funcs, ptrs = [], {}
    for f in mod.functions:
        if f.ifunc and len(f.ifunc) == 1 and f.ifunc[0][0] is None:   # nothing to choose
            # near: a jump to another global function, never shortened (by any assembler)
            funcs.append((f.name, [Ins("tailjmp", [Lab(f.ifunc[0][1])], hint="near")], True))
        elif f.ifunc:
            ptr, res = f"{f.name}_ptr", f"{f.name}_resolve"
            ptrs[ptr] = res
            funcs.append((f.name, [Ins("tailjmp", [Mem(sym=ptr, w=64)])], True))
            funcs.append((res, resolver(f, abi_name), False))
        else:
            funcs.append((f.name, compile_function(f, abi_name, pool, mod), f.public))
    return PRINTERS[syntax].module(funcs, fmt_of(abi_name), pool.consts, mod.tables,
                                   mod.fn_tables, ptrs, export_all=export_all)


def nasm_library(mod: ir.Module, abi_name: str, export_all: bool = False) -> str:
    return library(mod, abi_name, "nasm", export_all)
