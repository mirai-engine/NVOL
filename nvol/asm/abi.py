"""x86-64 calling conventions: System V (Linux, macOS, BSD) and Win64 (Windows)."""
from __future__ import annotations

from dataclasses import dataclass

from .mir import GPRS, PHYS, VECS


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

    def caller_saved(self) -> list:
        return [PHYS[r] for r in GPRS if r not in self.callee_saved] + \
               [PHYS[v] for v in VECS if v not in self.vec_callee_saved]

    def arg_reg(self, i: int):
        return PHYS[self.args[i]] if i < len(self.args) else None

    def stack_arg_offset(self, i: int) -> int:
        """Offset of argument i from rsp at function entry (return address at 0)."""
        return 8 + self.shadow + 8 * (i - len(self.args))


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

ABIS = {"sysv": SYSV, "win64": WIN64}
