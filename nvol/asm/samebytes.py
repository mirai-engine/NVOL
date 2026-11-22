"""Compare the machine code of the assembly backend's syntaxes: one reference, not three.

Each syntax's object is disassembled (`objdump -dr`, Intel syntax) function by function. The
bytes a relocation covers are masked (NASM resolves a reference against the section, MASM and
GNU as against the symbol: different addends, the same address after linking), and the padding
at the end of a function (before the function tables) is dropped: its bytes are not code.
Then every instruction is compared with the first syntax's:

- "same": identical bytes;
- "alt": the same instruction and length in the other register-to-register direction. x86
  encodes most of them both ways (`mov rax, rdx` is `48 89 d0` and `48 8b c2`); NASM and GNU
  as write the first, ml64 the second, and no syntax lets the source choose. These are the
  only differences allowed (`DIRECTION`);
- "diff": anything else: another instruction, another length, another alternative.

The function tables (offsets between functions), the read-only constants and the unwind
tables (Win64 .xdata and the size of .pdata; System V .eh_frame) are compared as bytes: NASM's
are written by NVOL (encode.unwind, encode.cfi), ml64's and GNU as's by the assemblers from
their directives. `check_lengths` compares `encode.length` (what NVOL decides, jumps included) with what
GNU as assembled, instruction by instruction.

    python -m nvol.asm.samebytes win64|sysv     # assembles the generator's output itself

aarch64 (`aapcs64`) has one syntax, GNU as, and two assemblers: binutils' `as` and Clang's
integrated one. The same `.s` must give the same bytes in both (every instruction, the
constants, the function tables), the same unwind rules (`cfa_rules`: the encoding of
`.eh_frame` is the assembler's choice), and each function the size `a64.code_size`
predicts: A64 has no relaxation, so a wrong prediction would misplace an `align` and every
branch range NVOL checks. `NVOL_A64_AS`, `NVOL_A64_CC` and `NVOL_A64_OBJDUMP` name the tools.

    python -m nvol.asm.samebytes aapcs64
"""
from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import sys
import tempfile

_FN = re.compile(r"^[0-9a-f]+ <([^>]+)>:$")
_INS = re.compile(r"^\s+([0-9a-f]+):\t([0-9a-f ]+?)\s*(?:\t(.*))?$")
_REL = re.compile(r"^\s+([0-9a-f]+): (\S+)\s+(\S+)")
_RELSIZE = {"IMAGE_REL_AMD64_REL32": 4, "IMAGE_REL_AMD64_ADDR64": 8, "IMAGE_REL_AMD64_ADDR32NB": 4,
            "R_X86_64_PC32": 4, "R_X86_64_PLT32": 4, "R_X86_64_64": 8, "R_X86_64_32": 4,
            "R_X86_64_GOTPCRELX": 4, "R_X86_64_REX_GOTPCRELX": 4}
# (NASM/GNU as opcode, ml64 opcode) of the same register-to-register operation
DIRECTION = {(0x89, 0x8B), (0x88, 0x8A), (0x39, 0x3B), (0x29, 0x2B), (0x31, 0x33), (0x21, 0x23),
             (0x09, 0x0B), (0x01, 0x03), (0x11, 0x13), (0x19, 0x1B)}


def _bytes(field: str) -> list:
    """The raw bytes objdump prints: x86 one byte per token; aarch64 one 32-bit word per
    instruction, printed as a number (little-endian in memory)."""
    out = []
    for t in field.split():
        v = int(t, 16)
        out += [v] if len(t) == 2 else list(v.to_bytes(len(t) // 2, "little"))
    return out


def disassemble(obj: str, objdump: str = "objdump", intel: bool = True):
    """({function: [(bytes with relocated bytes as None, text)]}, {table: bytes}) of the code."""
    out = subprocess.run([objdump, "-dr"] + (["-M", "intel"] if intel else []) + [obj],
                         capture_output=True, text=True, check=True).stdout
    funcs, cur, relocs, code = {}, None, [], False
    for line in out.splitlines():
        if line.startswith("Disassembly of section "):
            code = line.split()[-1].rstrip(":").startswith(".text")   # .text, MASM's .text$mn
            cur = None
            continue
        if not code:
            continue
        m = _FN.match(line)
        if m:
            name = m.group(1)
            # the end label of NASM's Win64 unwind range: what follows is not the function
            if name.endswith(".unwind_end"):
                cur = None
                continue
            # a local label NASM leaves as a symbol in COFF (fn.L2) or the ELF alias (L.fn)
            if "." in name:
                continue
            cur = funcs.setdefault(name, [])
            continue
        if cur is None:
            continue
        m = _INS.match(line)
        if m and m.group(3) is not None:
            cur.append([int(m.group(1), 16), _bytes(m.group(2)), m.group(3).strip()])
            continue
        if m and cur:                                   # a continuation line of raw bytes
            cur[-1][1] += _bytes(m.group(2))
            continue
        r = _REL.match(line)
        if r and (r.group(2) in _RELSIZE or r.group(2).startswith("R_AARCH64_")):
            # an aarch64 relocation patches a field of its 4-byte instruction
            relocs.append((int(r.group(1), 16), _RELSIZE.get(r.group(2), 4)))
    masked = set()
    for off, n in relocs:
        masked.update(range(off, off + n))
    res, tables = {}, {}
    for name, ins in funcs.items():
        items = [(tuple(None if o + k in masked else x for k, x in enumerate(b)), _text(t))
                 for o, b, t in ins]
        if name.startswith("nvol_fntbl_"):            # data: compared as bytes
            tables[name] = tuple(x for b, _ in items for x in b)
            continue
        while items and items[-1][1].startswith(("nop", "xchg ax,ax", "data16", "cs nop")):
            items.pop()                                 # alignment padding, not code
        res[name] = items
    return res, tables


def section(obj: str, names, objdump: str = "objdump") -> bytes:
    """The bytes of the object's sections with these names, concatenated."""
    out = subprocess.run([objdump, "-s", obj], capture_output=True, text=True, check=True).stdout
    data, take = bytearray(), False
    for line in out.splitlines():
        if line.startswith("Contents of section "):
            take = line.split()[-1].rstrip(":") in names
            continue
        if take and line.startswith(" "):
            body = line[1:]
            i = body.index(" ")                  # after the address (of any width)
            for word in body[i + 1:i + 36].split():
                data += bytes.fromhex(word)
    return bytes(data)


def rodata(obj: str, objdump: str = "objdump") -> bytes:
    """The read-only data (vector constants, byte tables) of an object."""
    return section(obj, (".rdata", ".rodata"), objdump)


def unwind(obj: str, objdump: str = "objdump"):
    """The unwind tables. Win64: the UNWIND_INFO bytes (.xdata, no relocations) and the size of
    .pdata (one 12-byte entry per function with a prologue; its words are relocations).
    ELF: .eh_frame with each FDE's pc-relative start (a relocation) zeroed."""
    xdata = section(obj, (".xdata",), objdump)
    if xdata:
        return xdata, len(section(obj, (".pdata",), objdump))
    eh = bytearray(section(obj, (".eh_frame",), objdump))
    k = 0
    while k + 8 <= len(eh):
        n = int.from_bytes(eh[k:k + 4], "little")
        if n == 0:
            break
        if int.from_bytes(eh[k + 4:k + 8], "little"):      # an FDE (a CIE has id 0)
            eh[k + 8:k + 12] = bytes(4)
        k += 4 + n
    return bytes(eh), None


def _text(t: str) -> str:
    t = re.sub(r"\s+#.*$", "", t)
    t = re.sub(r"<[^>]*>", "", t)
    t = re.sub(r"rip\+0x[0-9a-f]+", "rip+X", t)
    t = re.sub(r"\b(j[a-z]+|call)\s+[0-9a-f]+\b", r"\1 T", t)     # branch targets: offsets
    return re.sub(r"\s+", " ", t).strip()


def _opcode(b):
    """The first byte after the legacy prefixes and REX."""
    return next(x for x in b if x is not None and not 0x40 <= x <= 0x4F and x not in (0x66, 0xF2, 0xF3))


def _eq(b1, b2) -> bool:
    """Equal bytes, a relocated byte (None) on either side matching anything: NASM resolves a
    reference inside the section itself, ml64 and GNU as leave it to the linker."""
    return len(b1) == len(b2) and all(x is None or y is None or x == y for x, y in zip(b1, b2))


def compare(ref: dict, other: dict) -> dict:
    """Counts of same/alt/diff instructions, and examples of what is not "same"."""
    st = {"same": 0, "alt": 0, "diff": 0, "examples": []}
    for name, ins in ref.items():
        o = other.get(name)
        if o is None or len(o) != len(ins):
            st["diff"] += 1
            st["examples"].append((name, "missing" if o is None else f"{len(ins)} vs {len(o)} instructions"))
            continue
        for (b1, t1), (b2, t2) in zip(ins, o):
            if _eq(b1, b2):
                st["same"] += 1
            elif len(b1) == len(b2) and t1 == t2 and (_opcode(b1), _opcode(b2)) in DIRECTION:
                st["alt"] += 1
            else:
                st["diff"] += 1
                if len(st["examples"]) < 40:
                    st["examples"].append((name, t1, t2, _hex(b1), _hex(b2)))
    return st


def _hex(b) -> str:
    return " ".join("??" if x is None else f"{x:02x}" for x in b)


def _tool(syntax):
    from .masm import find_ml64
    return {"nasm": shutil.which("nasm"), "gas": shutil.which("as"), "masm": find_ml64()}[syntax]


def build(abi: str, work: str, mod=None) -> dict:
    """{syntax: object path}: the generator's output for one ABI in every syntax this machine has."""
    from . import SYNTAX_ABIS, SYNTAX_EXT, fmt_of, library
    if mod is None:
        from ..kernels import build_modules
        mod = build_modules()["x86_64"]
    objs = {}
    for syntax, abis in SYNTAX_ABIS.items():
        tool = _tool(syntax) if abi in abis else None
        if not tool:
            continue
        src = os.path.join(work, f"lib_{syntax}{SYNTAX_EXT[syntax]}")
        obj = os.path.join(work, f"lib_{syntax}.o")
        with open(src, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(library(mod, abi, syntax))
        cmd = {"nasm": [tool, "-f", fmt_of(abi), src, "-o", obj],
               "gas": [tool, "--64", "-o", obj, src],
               "masm": [tool, "-nologo", "-c", f"-Fo{obj}", src]}[syntax]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        objs[syntax] = obj
    return objs


def check_lengths(mod, abi: str, gas_funcs: dict) -> list:
    """Every instruction where encode.length (jump sizes from encode.relax) differs from what GNU
    as assembled: [(function, instruction, predicted, actual)]."""
    from . import compile_function, encode, resolver
    from .lower import Pool
    bad, pool = [], Pool()
    for f in mod.functions:
        if f.ifunc:
            if len(f.ifunc) == 1 and f.ifunc[0][0] is None:
                continue
            name, code = f.name + "_resolve", resolver(f, abi)
        else:
            name, code = f.name, compile_function(f, abi, pool, mod)
        mir = [i for i in code if i.op not in ("label", "endprolog")]
        got, k = gas_funcs[name], 0
        for ins in mir:
            if ins.op == "align":                    # its padding: NOP instructions
                n = 0
                while n < ins.args[2].v and k < len(got) and got[k][1].startswith(("nop", "xchg")):
                    n += len(got[k][0])
                    k += 1
                if n != ins.args[2].v:
                    bad.append((name, "align", ins.args[2].v, n))
                continue
            if k >= len(got):
                bad.append((name, "count", len(mir), len(got)))
                break
            b, t = got[k]
            k += 1
            if encode.length(ins) != len(b):
                bad.append((name, t, encode.length(ins), len(b)))
    return bad


def fn_tables(obj: str, mod, objdump: str = "objdump") -> dict:
    """{table: its bytes}, read from the code section at the table's symbol (the entries are
    constant offsets; objdump may file the table under a label that ends the function before
    it, so the disassembly is not used)."""
    syms = subprocess.run([objdump, "-t", obj], capture_output=True, text=True, check=True).stdout
    text = section(obj, (".text", ".text$mn"), objdump)
    out = {}
    for name, fns in mod.fn_tables.items():
        k = f"nvol_fntbl_{name}"
        line = next(l for l in syms.splitlines() if l.split() and l.split()[-1] == k)
        # ELF: "addr flags section size name"; COFF: "[n](sec ..)(..) 0xaddr name"
        hexes = [t for t in line.split() if t.startswith("0x")]
        addr = int(hexes[-1], 16) if line.startswith("[") else int(line.split()[0], 16)
        out[k] = text[addr:addr + 4 * len(fns)]
    return out


def tables_equal(a: dict, b: dict, mod=None) -> bool:
    return a == b


def cfa_rules(obj: str, objdump: str) -> list:
    """The unwind rules of .eh_frame as objdump interprets them (each FDE's pc range and its
    table of CFA and saved registers), without the encoding: GNU as writes the aarch64 CIE with
    code/data alignment factors 4/-8 and Clang with 1/-4, so the same directives give other
    bytes and other FDE lengths, and the same rules."""
    out = subprocess.run([objdump, "--dwarf=frames-interp", obj],
                         capture_output=True, text=True, check=True).stdout
    rules = []
    for line in out.splitlines():
        if "file format" in line or not line.strip():
            continue
        if " CIE " in line:
            rules.append("CIE " + line.split()[-1])             # ra=30
        elif " FDE " in line:
            rules.append("FDE " + line.split()[-1])             # pc=start..end
        else:
            rules.append(" ".join(line.split()))
    return rules


def main_a64() -> int:
    """GNU as (binutils) against Clang's integrated assembler on the aarch64 library."""
    import shlex
    from . import a64, compile_function, library
    from .lower import Pool
    from ..kernels import build_modules
    mod = build_modules()["aarch64"]
    gas = os.environ.get("NVOL_A64_AS", "aarch64-alpine-linux-musl-as")
    cc = shlex.split(os.environ.get("NVOL_A64_CC", "clang --target=aarch64-alpine-linux-musl"))
    objdump = os.environ.get("NVOL_A64_OBJDUMP", "aarch64-alpine-linux-musl-objdump")
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "lib.s")
        with open(src, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(library(mod, "aapcs64", "gas"))
        objs = {"gas": os.path.join(work, "gas.o"), "clang": os.path.join(work, "clang.o")}
        subprocess.run([gas, "-o", objs["gas"], src], check=True)
        subprocess.run(cc + ["-c", "-o", objs["clang"], src], check=True)
        dis = {k: disassemble(o, objdump, intel=False) for k, o in objs.items()}
        st = compare(dis["gas"][0], dis["clang"][0])
        same_tables = fn_tables(objs["gas"], mod, objdump) == fn_tables(objs["clang"], mod, objdump)
        same_ro = rodata(objs["gas"], objdump) == rodata(objs["clang"], objdump)
        same_eh = cfa_rules(objs["gas"], objdump) == cfa_rules(objs["clang"], objdump)
        print(f"aapcs64: gas vs clang: {st['same']} same, {st['diff']} diff; function tables "
              f"{'equal' if same_tables else 'DIFFER'}, read-only data {'equal' if same_ro else 'DIFFERS'}, "
              f".eh_frame {'equal' if same_eh else 'DIFFERS'}")
        for e in st["examples"][:12]:
            print("   ", e)
        bad, pool = [], Pool()
        for f in mod.functions:
            if f.ifunc:                       # a single NEON target: one `b`, no resolver
                continue
            want = a64.code_size(compile_function(f, "aapcs64", pool, mod))
            got = 4 * len(dis["gas"][0][f.name])
            if want != got:
                bad.append((f.name, want, got))
        print(f"aapcs64: a64.code_size vs GNU as: {len(bad)} mismatches")
        for e in bad[:12]:
            print("   ", e)
        return 1 if st["diff"] or bad or not (same_tables and same_ro and same_eh) else 0


def main(abi: str) -> int:
    if abi == "aapcs64":
        return main_a64()
    from ..kernels import build_modules
    mod = build_modules()["x86_64"]
    with tempfile.TemporaryDirectory() as work:
        objs = build(abi, work, mod)
        dis = {s: disassemble(o) for s, o in objs.items()}
        ro = {s: (rodata(o), unwind(o)) for s, o in objs.items()}
        names = list(objs)
        bad = 0
        ref = names[0]
        for other in names[1:]:
            st = compare(dis[ref][0], dis[other][0])
            same_tables = fn_tables(objs[ref], mod) == fn_tables(objs[other], mod)
            same_ro = ro[ref] == ro[other]       # constants, and the Win64 unwind tables
            print(f"{abi}: {ref} vs {other}: {st['same']} same, {st['alt']} alt (direction), "
                  f"{st['diff']} diff; function tables {'equal' if same_tables else 'DIFFER'}, "
                  f"read-only data {'equal' if same_ro else 'DIFFERS'}")
            for e in st["examples"][:12]:
                print("   ", e)
            bad += st["diff"] + (not same_tables) + (not same_ro)
        if "gas" in dis:
            lens = check_lengths(mod, abi, dis["gas"][0])
            print(f"{abi}: encode.length vs GNU as: {len(lens)} mismatches")
            for e in lens[:12]:
                print("   ", e)
            bad += len(lens)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "win64"))
