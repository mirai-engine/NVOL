"""encode.length at the edges the library does not reach: hand-written instructions on every
boundary of the encoding (displacements of 127/128/-128/-129, bases that force a SIB byte or a
displacement, imm8 against imm32, the accumulator forms, byte registers that need REX, VEX
with 2 or 3 bytes), printed in every syntax, assembled, and each length compared with what the
assemblers produced; NASM and GNU as must also give the same bytes."""
import itertools
import os
import shutil
import subprocess
import sys

import pytest

from nvol.asm import encode, gas, masm, nasm, samebytes
from nvol.asm.masm import find_ml64
from nvol.asm.mir import PHYS, RSP, Imm, Ins, Mem, Reg

ABI = "win64" if sys.platform == "win32" else "sysv"
FMT = "win64" if ABI == "win64" else "elf64"
R = dict(PHYS, rsp=RSP)


def _g(n, w=64):
    return Reg(R[n], w)


def _v(n, w=256):
    return Reg(R[f"xmm{n}"], w)


def _cases() -> list:
    out = []
    bases = ["rax", "rbp", "r13", "rsp", "r12", "r9"]
    disps = [0, 1, 127, 128, -128, -129, 4096]
    for b, d in itertools.product(bases, disps):
        for idx in (None, "rcx", "r10"):
            m = Mem(R[b], R[idx] if idx else None, 1, d, w=64)
            out.append(Ins("mov", [_g("rdx"), m]))
            out.append(Ins("lea", [_g("r11"), Mem(R[b], R[idx] if idx else None, 1, d)]))
            out.append(Ins("movzx", [_g("rax", 32), Mem(R[b], R[idx] if idx else None, 1, d, w=8)]))
            out.append(Ins("vmovdqu", [_v(1), Mem(R[b], R[idx] if idx else None, 1, d)]))
            out.append(Ins("vmovdqu", [Mem(R[b], R[idx] if idx else None, 1, d), _v(9)]))
            out.append(Ins("movdqu", [_v(10, 128), Mem(R[b], R[idx] if idx else None, 1, d)]))
            out.append(Ins("mov", [Mem(R[b], R[idx] if idx else None, 1, d, w=8), Imm(7)]))
    out.append(Ins("movsxd", [_g("r8"), Mem(R["rax"], R["r9"], 4, 128, w=32)]))
    for op in ("add", "sub", "and", "or", "xor", "cmp"):
        for reg in ("rax", "rcx", "r9"):
            for w in (32, 64):
                for v in (1, 127, 128, -128, -129, 0x7FFFFFFF, -0x80000000):
                    out.append(Ins(op, [_g(reg, w), Imm(v)]))
                out.append(Ins(op, [_g(reg, w), _g("r12" if reg != "r12" else "rbx", w)]))
        out.append(Ins(op, [_g("rax", 32), Imm(0xFFFFFF80)]))
    for reg in ("rax", "r9"):
        for v in (0, 1, 0x7FFFFFFF, 0xFFFFFFFF):
            out.append(Ins("mov", [_g(reg, 32), Imm(v)]))
        for v in (-1, -0x80000000, 1 << 40, (1 << 64) - 2):
            out.append(Ins("mov", [_g(reg), Imm(v)]))
        out.append(Ins("mov", [Mem(R[reg], disp=8, w=64), Imm(-5)]))
        out.append(Ins("test", [_g(reg), _g(reg)]))
    for op in ("shl", "shr"):
        for reg in ("rax", "r9"):
            out += [Ins(op, [_g(reg), Imm(1)]), Ins(op, [_g(reg), Imm(5)]),
                    Ins(op, [_g(reg, 32), Imm(4)]), Ins(op, [_g(reg), _g("rcx", 8)])]
    for v in (3, 127, 128, 0x01010101):
        out += [Ins("imul", [_g("rax", 32), _g("r9", 32), Imm(v)]),
                Ins("imul", [_g("r9", 32), _g("rax", 32), Imm(v)])]
    out.append(Ins("imul", [_g("r10"), _g("rbx")]))
    for reg in ("rax", "rsi", "rdi", "rbp", "r8", "rbx"):
        out += [Ins("sete", [_g(reg, 8)]), Ins("movzx", [_g("rax", 32), _g(reg, 8)]),
                Ins("movzx", [_g("r9", 32), _g(reg, 8)]),
                Ins("mov", [Mem(R["rax"], disp=1, w=8), _g(reg, 8)]),
                Ins("push", [_g(reg)]), Ins("pop", [_g(reg)]),
                Ins("bsf", [_g(reg, 32), _g("r9", 32)]), Ins("popcnt", [_g(reg, 32), _g("r9", 32)]),
                Ins("cmovb", [_g(reg), _g("r9")]), Ins("callind", [_g(reg)])]
    for a, b in itertools.product((0, 6, 9, 14), repeat=2):
        out += [Ins("vmovdqa", [_v(a), _v(b)]), Ins("vmovdqu", [_v(a, 128), _v(b, 128)]),
                Ins("movdqa", [_v(a, 128), _v(b, 128)]), Ins("pcmpeqb", [_v(a, 128), _v(b, 128)]),
                Ins("vpcmpeqb", [_v(a), _v(b), _v(3)]), Ins("vpcmpeqb", [_v(a), _v(3), _v(b)]),
                Ins("vpshufb", [_v(a), _v(b), _v(2)]), Ins("vpbroadcastb", [_v(a), _v(b, 128)]),
                Ins("vpermq", [_v(a), _v(b), Imm(0x4E)]), Ins("vpsrlw", [_v(a), _v(b), Imm(4)]),
                Ins("vextracti128", [_v(a, 128), _v(b), Imm(1)]),
                Ins("vperm2i128", [_v(a), _v(b), _v(5), Imm(0x21)]),
                Ins("vpalignr", [_v(a), _v(5), _v(b), Imm(15)]),
                Ins("pshufd", [_v(a, 128), _v(b, 128), Imm(0)]), Ins("psrlw", [_v(a, 128), Imm(4)]),
                Ins("palignr", [_v(a, 128), _v(b, 128), Imm(15)]),
                Ins("pshufb", [_v(a, 128), _v(b, 128)]), Ins("vptest", [_v(a), _v(b)])]
    for g, x in itertools.product(("rax", "r9"), (1, 9)):
        out += [Ins("vmovd", [_v(x, 128), _g(g, 32)]), Ins("movd", [_v(x, 128), _g(g, 32)]),
                Ins("vmovq", [_g(g), _v(x, 128)]), Ins("movq", [_g(g), _v(x, 128)]),
                Ins("vpmovmskb", [_g(g, 32), _v(x)]), Ins("pmovmskb", [_g(g, 32), _v(x, 128)]),
                Ins("vpextrq", [_g(g), _v(x, 128), Imm(1)])]
    out += [Ins("vzeroupper", []), Ins("ret", [])]
    return out


SYNTAXES = [("nasm", shutil.which("nasm")), ("gas", shutil.which("as"))] + \
    ([("masm", find_ml64())] if ABI == "win64" else [])


def _assemble(syntax, tool, code, d):
    if syntax == "nasm":
        text = nasm.module([("f", code, True)], FMT)
        src, cmd = d / "e.asm", lambda s, o: [tool, "-f", FMT, str(s), "-o", str(o)]
    elif syntax == "gas":
        text = gas.module([("f", code, True)], FMT)
        src, cmd = d / "e.s", lambda s, o: [tool, "--64", "-o", str(o), str(s)]
    else:
        text = masm.module([("f", code, True)], FMT)
        src, cmd = d / "m.asm", lambda s, o: [tool, "-nologo", "-c", f"-Fo{o}", str(s)]
    src.write_text(text, encoding="utf-8")
    obj = d / f"e_{syntax}.o"
    subprocess.run(cmd(src, obj), check=True, stdout=subprocess.DEVNULL)
    return samebytes.disassemble(str(obj))[0]["f"]


@pytest.mark.parametrize("syntax,tool", [s for s in SYNTAXES if s[1]], ids=lambda x: str(x))
def test_lengths(syntax, tool, tmp_path):
    if not shutil.which("objdump"):
        pytest.skip("no objdump")
    code = _cases()
    got = _assemble(syntax, tool, code, tmp_path)
    assert len(got) == len(code), (len(got), len(code))
    bad = [(ins, encode.length(ins), len(b), t) for ins, (b, t) in zip(code, got)
           if encode.length(ins) != len(b) and encode.vex_store_form(ins) is None]
    assert not bad, bad[:10]


def test_nasm_gas_same_bytes(tmp_path):
    if not (shutil.which("nasm") and shutil.which("as") and shutil.which("objdump")):
        pytest.skip("needs nasm, as and objdump")
    code = _cases()
    a = _assemble("nasm", shutil.which("nasm"), code, tmp_path)
    b = _assemble("gas", shutil.which("as"), code, tmp_path)
    diff = [(t1, samebytes._hex(b1), samebytes._hex(b2)) for (b1, t1), (b2, t2) in zip(a, b)
            if not samebytes._eq(b1, b2)]
    assert not diff, diff[:10]
