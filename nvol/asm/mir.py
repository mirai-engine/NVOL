"""Machine IR: x86-64 or A64 instructions over virtual and physical registers.

The lowering (`lower.py`) turns an IR function into a flat list of `Ins`; the allocator
(`regalloc.py`) replaces every `VReg` with a `Phys`; a printer (`nasm.py`, later MASM and GNU as)
spells the result. Each `Ins` names the registers it reads (`uses`) and writes (`defs`), so the
allocator never has to know what an opcode means.

Operands are in Intel order (destination first). A register operand is `Reg(reg, w)`: the same
register read or written at width `w` (8/16/32/64 bits for integers, 128/256 for vectors).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

GPRS = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11",
        "r12", "r13", "r14", "r15"]
VECS = [f"xmm{i}" for i in range(16)]
# A64: x0-x30 (x29 the frame pointer, x30 the link register) and v0-v31
A64_GPRS = [f"x{i}" for i in range(31)]
A64_VECS = [f"v{i}" for i in range(32)]


@dataclass(frozen=True)
class Phys:
    name: str           # the 64-bit name for integers ("rax"), "xmmN" for vectors
    cls: str            # "g" or "v"

    def __repr__(self) -> str:
        return self.name


PHYS = {n: Phys(n, "g") for n in GPRS}
PHYS.update({n: Phys(n, "v") for n in VECS})
PHYS.update({n: Phys(n, "g") for n in A64_GPRS})
PHYS.update({n: Phys(n, "v") for n in A64_VECS})
RSP = Phys("rsp", "g")      # never allocated: frames and outgoing stack arguments
SP = Phys("sp", "g")        # A64, never allocated
XZR = Phys("xzr", "g")      # A64 zero register, read only


class VReg:
    __slots__ = ("cls", "id")

    def __init__(self, cls: str, id: int):
        self.cls, self.id = cls, id

    def __repr__(self) -> str:
        return f"%{self.cls}{self.id}"


RegId = Union[VReg, Phys]


@dataclass
class Reg:
    r: RegId
    w: int


@dataclass
class Imm:
    v: int


@dataclass
class Mem:
    """[base + index*scale + disp], or [rel sym + disp] when sym is set. `w` is the access
    width in bits (0 when the other operand already fixes it). `arg` marks an incoming stack
    argument: its displacement is fixed once the frame is known."""
    base: Optional[RegId] = None
    index: Optional[RegId] = None
    scale: int = 1
    disp: int = 0
    sym: Optional[str] = None
    w: int = 0
    arg: Optional[int] = None
    spill: Optional[int] = None     # spill slot number (resolved with the frame)


@dataclass
class Lab:
    name: str


@dataclass
class Ins:
    op: str
    args: list
    defs: list = field(default_factory=list)
    uses: list = field(default_factory=list)
    # registers a call destroys (caller-saved set of the ABI); empty for everything else
    clobbers: list = field(default_factory=list)
    # a jump's size, decided by encode.relax: "short" (rel8) or "near" (rel32)
    hint: Optional[str] = None

    def __repr__(self) -> str:
        return f"{self.op} {self.args}"


def regs_in(op) -> list:
    """Registers an operand reads: a register itself, or a memory operand's base and index."""
    if isinstance(op, Reg):
        return [op.r]
    if isinstance(op, Mem):
        return [r for r in (op.base, op.index) if r is not None]
    return []


# instructions that end a basic block. A conditional jump names its label first; A64's cbz/cbnz
# (label, register) and tbz/tbnz (label, register, bit) test a register instead of the flags
JUMPS = {"jmp"}
CONDJUMPS = {"jb", "jae", "jbe", "ja", "je", "jne", "jz", "jnz", "js", "jns",
             "cbz", "cbnz", "tbz", "tbnz"}
TERMINATORS = {"ret", "jmp", "tailjmp", "tailcall"}
# the jump taken when the other one is not
INVERT = {"jb": "jae", "jae": "jb", "jbe": "ja", "ja": "jbe", "je": "jne", "jne": "je",
          "js": "jns", "jns": "js", "cbz": "cbnz", "cbnz": "cbz", "tbz": "tbnz", "tbnz": "tbz"}
