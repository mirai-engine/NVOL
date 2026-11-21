"""Static instruction count of NVOL's aarch64 assembly against Clang -O2 of the generated C.

QEMU runs aarch64 code but does not time it, so until there is ARM hardware (PENDING item 31)
the only comparison with a compiler is static: the instructions of each public function in the
disassembly. In the C the kernels are inlined into each public entry; in the assembly a public
function with a single NEON target is one `b` to it, and a wrapper may end in a tail call
(`b` to another function), so each function counts with the functions it tail-calls.
Fewer instructions is not faster by itself: what runs is the loop, and the loop costs need
ARM hardware. (A loop's length cannot be read off the layout either: the cold exits sit
before the loop head, inside the span of its backward branch.)

    python3 harness/asm/a64_static.py [--md]       # in Alpine, with clang and binutils-aarch64

NVOL_A64_CC (default clang --target=aarch64-alpine-linux-musl) and NVOL_A64_OBJDUMP
(aarch64-alpine-linux-musl-objdump) name the tools.
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from nvol.asm import library  # noqa: E402
from nvol.kernels import build_modules  # noqa: E402

_FN = re.compile(r"^([0-9a-f]+) <([^>]+)>:$")
_INS = re.compile(r"^\s+([0-9a-f]+):\t[0-9a-f]{8}\s+\t?(\S+)\s*(.*)$")


def functions(obj, objdump):
    """{symbol: [(address, mnemonic, operands)]} of the code, padding NOPs at the end dropped."""
    out = subprocess.run([objdump, "-d", obj], capture_output=True, text=True, check=True).stdout
    funcs, cur = {}, None
    for line in out.splitlines():
        m = _FN.match(line)
        if m:
            name = m.group(2)
            cur = None if name.startswith(("L.", ".L", "$")) else funcs.setdefault(name, [])
            continue
        m = _INS.match(line)
        if m and cur is not None:
            cur.append((int(m.group(1), 16), m.group(2), m.group(3)))
    for ins in funcs.values():
        while ins and ins[-1][1] == "nop":
            ins.pop()
    return funcs


def count(funcs, name, seen=()) -> int:
    """Instructions of a function plus those of every function it tail-calls (`b <other>`)."""
    n = len(funcs[name])
    for _, mn, ops in funcs[name]:
        t = re.search(r"<(L\.)?([^>+]+)>", ops) if mn == "b" else None
        callee = t.group(2) if t else None
        if callee and callee != name and callee in funcs and callee not in seen:
            n += count(funcs, callee, seen + (name,))
    return n


def main(md: bool) -> int:
    cc = shlex.split(os.environ.get("NVOL_A64_CC", "clang --target=aarch64-alpine-linux-musl"))
    objdump = os.environ.get("NVOL_A64_OBJDUMP", "aarch64-alpine-linux-musl-objdump")
    mod = build_modules()["aarch64"]
    target = {f.name: f.ifunc[0][1] for f in mod.functions if f.ifunc}
    public = [f.name for f in mod.functions if f.public]
    with tempfile.TemporaryDirectory() as w:
        s = os.path.join(w, "nvol.s")
        with open(s, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(library(mod, "aapcs64", "gas"))
        subprocess.run(cc + ["-c", "-o", os.path.join(w, "asm.o"), s], check=True)
        subprocess.run(cc + ["-O2", "-std=c99", "-c", "-o", os.path.join(w, "c.o"),
                             os.path.join(ROOT, "generated", "c", "nvol.c")], check=True)
        a = functions(os.path.join(w, "asm.o"), objdump)
        c = functions(os.path.join(w, "c.o"), objdump)
    rows, ta, tc = [], 0, 0
    for name in public:
        if name not in c:
            continue
        x, y = count(a, target.get(name, name)), count(c, name)
        rows.append((name, x, y))
        ta, tc = ta + x, tc + y
    if md:
        print("| function | NVOL asm | clang -O2 | Δ |")
        print("|---|---|---|---|")
        for n, x, y in rows:
            print(f"| `{n}` | {x} | {y} | {100 * (x - y) / y:+.1f} % |")
        print(f"| **total ({len(rows)})** | {ta} | {tc} | {100 * (ta - tc) / tc:+.1f} % |")
    else:
        for n, x, y in rows:
            print(f"{n:48} {x:6} {y:6} {100 * (x - y) / y:+7.1f} %")
        print(f"total ({len(rows)}): asm {ta}, clang {tc}, {100 * (ta - tc) / tc:+.1f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main("--md" in sys.argv))
