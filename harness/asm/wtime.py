"""NVOL's assembly against the C library compiled by MSVC (and by MinGW GCC) on Windows, by
wall-clock time: Windows exposes no hardware counters to a user program, so each build runs
`bench OP SIZE N PATH time` (the best of 9 runs of N calls) and prints nanoseconds per call.

    python harness/asm/wtime.py [ops] [sizes] [paths]
    defaults: every operation of bench.c; 64 4096; "1 0"

Builds (in a temporary directory): msvc = cl /O2 bench.c + generated/c/nvol.c; gcc = gcc -O2
the same; asm = ml64-free NASM object of generated/nasm + its nvol.c, compiled by cl.
Times on a shared machine move by several percent between runs: read the gaps, not the digits.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
OPS = "memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem byteset byteset_not pair teddy"


def msvc(work: str, cmd: str) -> None:
    vs = subprocess.run([VSWHERE, "-latest", "-products", "*", "-requires",
                         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                         "installationPath"], capture_output=True, text=True).stdout.strip()
    bat = os.path.join(work, "b.bat")
    with open(bat, "w") as fh:
        fh.write(f'@call "{os.path.join(vs, "VC", "Auxiliary", "Build", "vcvars64.bat")}" >nul\n{cmd}\n')
    r = subprocess.run(["cmd", "/c", bat], cwd=work, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stdout + r.stderr)


def build(work: str) -> dict:
    b = os.path.join(ROOT, "harness", "asm", "bench.c")
    gc, gn = os.path.join(ROOT, "generated", "c"), os.path.join(ROOT, "generated", "nasm")
    exe = {k: os.path.join(work, f"bench-{k}.exe") for k in ("msvc", "gcc", "asm")}
    msvc(work, f'cl /nologo /O2 /DNVOL_TESTING /I"{gc}" "{b}" "{gc}\\nvol.c" /Fe"{exe["msvc"]}"')
    subprocess.run(["gcc", "-O2", "-DNVOL_TESTING", f"-I{gc}", "-o", exe["gcc"], b,
                    os.path.join(gc, "nvol.c")], check=True)
    obj = os.path.join(work, "kernels.obj")
    subprocess.run(["nasm", "-f", "win64", os.path.join(gn, "nvol_x86_64_win64.asm"), "-o", obj],
                   check=True)
    msvc(work, f'cl /nologo /O2 /DNVOL_TESTING /I"{gn}" "{b}" "{gn}\\nvol.c" "{obj}" /Fe"{exe["asm"]}"')
    return exe


def main() -> int:
    ops = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else OPS).split()
    sizes = (sys.argv[2] if len(sys.argv) > 2 else "64 4096").split()
    paths = (sys.argv[3] if len(sys.argv) > 3 else "1 0").split()
    work = tempfile.mkdtemp()
    try:
        exe = build(work)
        print(f"{'op':16} {'size':>6} {'path':>4} {'asm ns':>9} {'msvc ns':>9} {'gcc ns':>9} {'asm/msvc':>9}")
        for op in ops:
            for size in sizes:
                n = str(max(2000, 33554432 // (int(size) + 64) // 4))
                for p in paths:
                    t = {k: float(subprocess.run([e, op, size, n, p, "time"], capture_output=True,
                                                 text=True, check=True).stdout) for k, e in exe.items()}
                    print(f"{op:16} {size:>6} {p:>4} {t['asm']:9.2f} {t['msvc']:9.2f} {t['gcc']:9.2f} "
                          f"{100 * (t['asm'] / t['msvc'] - 1):+8.1f}%")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
