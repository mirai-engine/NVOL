import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(scope="session")
def module():
    from nvol.kernels import build_module
    return build_module()


@pytest.fixture(scope="session")
def gen_dir(module, tmp_path_factory):
    from nvol.__main__ import generate
    out = tmp_path_factory.mktemp("gen")
    from nvol.kernels import build_modules
    generate(build_modules(), ["c", "cpp", "rust", "python"], str(out))
    return out


def _cc(native: bool = False):
    """The C compiler: NVOL_CC if set (clang runs every C build of the suite; cl, MSVC, only
    the native library of the `native` fixture, whose build knows its flags), else the first
    of gcc, clang and cc."""
    want = os.environ.get("NVOL_CC")
    if want and (want != "cl" or native):
        return want if want == "cl" or shutil.which(want) else None
    for c in ("gcc", "clang", "cc"):
        if shutil.which(c):
            return c
    return None


# NVOL_ARCH=aarch64 (harness/asm/run-a64.sh, under QEMU): the C and NVOL's aarch64 assembly
# (GNU as syntax, AAPCS64), built with the cross compiler NVOL_A64_CC
A64 = os.environ.get("NVOL_ARCH", "x86_64") == "aarch64"
A64_CC = shlex.split(os.environ.get("NVOL_A64_CC", "clang --target=aarch64-alpine-linux-musl"))
ABI = "aapcs64" if A64 else "win64" if sys.platform == "win32" else "sysv"
ARCH = "aarch64" if A64 else "x86_64"


def paths(x86: dict) -> dict:
    """The forced paths of a native suite (name -> nvol_test_set_avx2 value): on aarch64 there
    is a single one, NEON (the hook ignores its argument there)."""
    return {"neon": -1} if A64 else x86


def mask_positions(m: int) -> list:
    """The positions a candidate mask of the byteset kernels marks (nvol.h): bit i on x86_64,
    bit 4i+3 on aarch64 (the NEON movemask keeps one nibble per byte)."""
    if A64:
        return [i for i in range(16) if m >> (4 * i + 3) & 1]
    return [i for i in range(64) if m >> i & 1]


# NVOL's own assembly in each syntax: NASM and GNU as (Win64 or System V), MASM (Win64)
BACKENDS = ["c", "gas-a64"] if A64 else \
    ["c", "nasm"] + (["masm"] if sys.platform == "win32" else []) + ["gas"]


def _build_native(gen_dir, backend):
    """The generated C as a shared lib with the test hook; for "nasm", "masm" and "gas" the
    x86_64 kernels come from the assembly backend in that syntax and the C is only the glue
    (`asm_kernels`)."""
    cc = A64_CC + ["-fuse-ld=lld"] if A64 else [_cc(native=True)]
    if not cc[0] or (cc[0] != "cl" and not shutil.which(cc[0])):
        pytest.skip("no C compiler")
    if backend == "c":
        cdir = gen_dir / "c"
        objs = []
    elif backend == "gas-a64":
        from nvol.asm import library
        from nvol.backends import c as c_backend
        from nvol.kernels import build_modules
        mods = build_modules()
        cdir = gen_dir / f"c-{backend}"
        cdir.mkdir(exist_ok=True)
        for name, text in c_backend.generate(mods, "c", asm_kernels={"aarch64"}).items():
            (cdir / name).write_text(text, encoding="utf-8", newline="\n")
        src = cdir / "nvol_aarch64_aapcs64.s"
        src.write_text(library(mods["aarch64"], "aapcs64", "gas"), encoding="utf-8", newline="\n")
        obj = cdir / "nvol_kernels.o"
        subprocess.run(A64_CC + ["-c", "-o", str(obj), str(src)], check=True)
        objs = [str(obj)]
    else:
        syntax = backend
        from nvol.asm.masm import find_ml64
        tool = find_ml64() if syntax == "masm" else shutil.which("as" if syntax == "gas" else "nasm")
        if not tool:
            pytest.skip(f"no {syntax}")
        from nvol.asm import SYNTAX_EXT, fmt_of, library
        from nvol.backends import c as c_backend
        from nvol.kernels import build_modules
        mods = build_modules()
        cdir = gen_dir / f"c-{backend}"
        cdir.mkdir(exist_ok=True)
        for name, text in c_backend.generate(mods, "c", asm_kernels=True).items():
            (cdir / name).write_text(text, encoding="utf-8", newline="\n")
        src = cdir / f"nvol_x86_64_{ABI}{SYNTAX_EXT[syntax]}"
        src.write_text(library(mods["x86_64"], ABI, syntax), encoding="utf-8", newline="\n")
        obj = cdir / "nvol_kernels.o"
        if syntax == "nasm":
            subprocess.run([tool, "-f", fmt_of(ABI), str(src), "-o", str(obj)], check=True)
        elif syntax == "gas":
            subprocess.run([tool, "--64", "-o", str(obj), str(src)], check=True)
        else:
            subprocess.run([tool, "-nologo", "-c", f"-Fo{obj}", str(src)], check=True,
                           stdout=subprocess.DEVNULL)
        objs = [str(obj)]
    name = "nvol" if backend == "c" else f"nvol_{backend}"
    lib = cdir / (f"{name}.dll" if sys.platform == "win32" else f"lib{name}.so")
    if cc[0] == "cl":           # MSVC (PENDING 2.4): cl /W4 /WX in vcvars64's environment
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness" / "asm"))
        from build_msvc import build_dll
        if build_dll(str(cdir / "nvol.c"), objs, str(lib)) != 0:
            pytest.fail("cl failed")
        return lib
    cmd = cc + ["-O2", "-Wall", "-Wextra", "-Werror", "-std=c99", "-shared", "-DNVOL_BUILD_DLL",
           "-DNVOL_TESTING", "-o", str(lib), str(cdir / "nvol.c")] + objs
    if sys.platform == "win32":
        cmd.insert(1, "-static")      # no dependency on the MinGW runtime DLLs
    else:
        cmd.insert(1, "-fPIC")
    subprocess.run(cmd, check=True)
    return lib


@pytest.fixture(scope="session", params=BACKENDS)
def native(gen_dir, request):
    """The generated Python binding over the native library of one backend: every test that
    takes `native` runs against the compiled C and against NVOL's own assembly."""
    lib = _build_native(gen_dir, request.param)
    spec = importlib.util.spec_from_file_location(f"nvol_ops_{request.param}",
                                                  gen_dir / "python" / "nvol_ops.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    old = os.environ.get("NVOL_LIB")
    os.environ["NVOL_LIB"] = str(lib)
    try:
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            del os.environ["NVOL_LIB"]
        else:
            os.environ["NVOL_LIB"] = old
    mod._lib.nvol_test_set_avx2.argtypes = [mod.ctypes.c_int]
    return mod
