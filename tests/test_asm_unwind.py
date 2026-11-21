"""Stack unwinding through NVOL's assembly: a C++ visitor throws from inside a visitor kernel
and main catches it. The exception has to unwind through the kernel's own frame (pushes,
stack allocation, saved xmm6-15 on Win64), which only works when the object describes that
frame to the unwinder: `.pdata`/`.xdata` on Win64, `.eh_frame` (DWARF CFI) on System V.

One executable per syntax, built with g++ from the generated library (the assembly object
and the C glue in asm mode), run on every forced CPU path."""
import shutil
import subprocess
import sys

import pytest

from nvol.asm import SYNTAX_EXT, library
from nvol.asm.masm import find_ml64
from nvol.backends import c as c_backend
from nvol.kernels import build_modules

CPP = r"""
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include "nvol.h"
extern "C" void nvol_test_set_avx2(int v);   /* the test hook (NVOL_TESTING) */

static int calls = 0;
static int throw_visitor(void *ctx, const uint8_t *chunk, uint64_t mask) {
    (void)ctx; (void)chunk; (void)mask;
    ++calls;
    throw std::runtime_error("from the visitor");
}

int main() {
    const uint8_t s1[] = "ab", s2[] = "cd";
    static uint8_t pair[NVOL_BYTESET_PAIR_BYTES];
    nvol_byteset_pair_init(s1, 2, s2, 2, 0, 1, pair);
    static uint8_t hay[4096];
    memset(hay, 'z', sizeof hay);
    hay[1000] = 'a'; hay[1001] = 'c';
    nvol_visitor v = {throw_visitor, nullptr};
    for (int mode : {1, 2, 0}) {             /* AVX2, SSE4.2, the baseline */
        nvol_test_set_avx2(mode);
        try {
            nvol_find_byteset_pair_visit_raw(pair, hay, hay + sizeof hay, &v);
            std::printf("mode %d: returned without throwing\n", mode);
            return 1;
        } catch (const std::runtime_error &e) {
            std::printf("mode %d: caught '%s'\n", mode, e.what());
        }
    }
    return calls == 3 ? 0 : 2;
}
"""

ABI = "win64" if sys.platform == "win32" else "sysv"
FMT = "win64" if ABI == "win64" else "elf64"
SYNTAXES = [("nasm", shutil.which("nasm")), ("gas", shutil.which("as"))] + \
    ([("masm", find_ml64())] if ABI == "win64" else [])


@pytest.fixture(scope="module")
def glue(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("no g++")
    d = tmp_path_factory.mktemp("unwind")
    mods = build_modules()
    for name, text in c_backend.generate(mods, "c", asm_kernels=True).items():
        (d / name).write_text(text, encoding="utf-8")
    (d / "main.cpp").write_text(CPP, encoding="utf-8")
    subprocess.run(["gcc", "-O2", "-c", "-DNVOL_TESTING", "-o", str(d / "glue.o"), str(d / "nvol.c")], check=True)
    return d, mods["x86_64"]


@pytest.mark.parametrize("syntax,tool", SYNTAXES, ids=[s for s, _ in SYNTAXES])
def test_exception_through_kernel(glue, syntax, tool):
    if not tool:
        pytest.skip(f"no {syntax}")
    d, mod = glue
    src = d / f"k_{syntax}{SYNTAX_EXT[syntax]}"
    src.write_text(library(mod, ABI, syntax), encoding="utf-8")
    obj = d / f"k_{syntax}.o"
    cmd = {"nasm": [tool, "-f", FMT, str(src), "-o", str(obj)],
           "gas": [tool, "--64", "-o", str(obj), str(src)],
           "masm": [tool, "-nologo", "-c", f"-Fo{obj}", str(src)]}[syntax]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    exe = d / f"unwind_{syntax}.exe"
    subprocess.run(["g++", "-O1", "-static", "-I", str(d), "-o", str(exe), str(d / "main.cpp"),
                    str(d / "glue.o"), str(obj)], check=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (syntax, r.returncode, r.stdout, r.stderr)
