"""Calling conventions: x86-64 System V (Linux, macOS, BSD) and Win64 (Windows), and AAPCS64
(aarch64 Linux)."""
from __future__ import annotations

from dataclasses import dataclass

from .mir import A64_GPRS, A64_VECS, GPRS, PHYS, VECS


@dataclass(frozen=True)
class ABI:
    name: str
    args: tuple             # integer argument registers, in order
    callee_saved: tuple     # integer registers a function must preserve
    vec_callee_saved: tuple  # vector registers a function must preserve (low 128 bits)
    shadow: int             # bytes the caller reserves above the return address
    # allocation order: caller-saved first (free to use), callee-saved last (cost a save)
    gpr_order: tuple
    vec_order: tuple
    gprs: tuple = tuple(GPRS)   # every integer register of the architecture
    vecs: tuple = tuple(VECS)
    stack_base: int = 8         # bytes between rsp at entry and the first stack argument
    ret: str = "rax"            # the integer result register

    def caller_saved(self) -> list:
        return [PHYS[r] for r in self.gprs if r not in self.callee_saved] + \
               [PHYS[v] for v in self.vecs if v not in self.vec_callee_saved]

    def arg_reg(self, i: int):
        return PHYS[self.args[i]] if i < len(self.args) else None

    def stack_arg_offset(self, i: int) -> int:
        """Offset of argument i from the stack pointer at function entry (on x86 the return
        address is at 0)."""
        return self.stack_base + self.shadow + 8 * (i - len(self.args))


SYSV = ABI(
    name="sysv",
    args=("rdi", "rsi", "rdx", "rcx", "r8", "r9"),
    callee_saved=("rbx", "rbp", "r12", "r13", "r14", "r15"),
    vec_callee_saved=(),
    shadow=0,
    gpr_order=("rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11",
               "rbx", "rbp", "r12", "r13", "r14", "r15"),
    vec_order=tuple(VECS),
)

WIN64 = ABI(
    name="win64",
    args=("rcx", "rdx", "r8", "r9"),
    callee_saved=("rbx", "rbp", "rdi", "rsi", "r12", "r13", "r14", "r15"),
    vec_callee_saved=tuple(f"xmm{i}" for i in range(6, 16)),
    shadow=32,
    gpr_order=("rax", "rcx", "rdx", "r8", "r9", "r10", "r11",
               "rsi", "rdi", "rbx", "rbp", "r12", "r13", "r14", "r15"),
    vec_order=tuple(VECS),
)

# x18 is the platform register (never used); x29 and x30 are saved with the frame when a function
# calls; v8-v15 keep only their low 64 bits across a call, so a call destroys every vector
# register for the allocator (`call_out`) while the frame saves d8-d15 when the body writes them
AAPCS64 = ABI(
    name="aapcs64",
    args=tuple(f"x{i}" for i in range(8)),
    callee_saved=tuple(f"x{i}" for i in range(19, 29)),
    vec_callee_saved=tuple(f"v{i}" for i in range(8, 16)),
    shadow=0,
    gpr_order=tuple(f"x{i}" for i in list(range(9, 16)) + list(range(8)) + [8, 16, 17]
                    + list(range(19, 29))),
    vec_order=tuple(f"v{i}" for i in list(range(8)) + list(range(16, 32)) + list(range(8, 16))),
    gprs=tuple(A64_GPRS),
    vecs=tuple(A64_VECS),
    stack_base=0,
    ret="x0",
)

ABIS = {"sysv": SYSV, "win64": WIN64, "aapcs64": AAPCS64}
