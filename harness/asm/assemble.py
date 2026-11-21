"""Assemble one file of NVOL's assembly backend with its syntax's assembler.

    python harness/asm/assemble.py nasm|masm|gas <file> <out.obj> [win64|elf64]

NASM and GNU as (`as`) from PATH; MASM is ml64 from $ML64, PATH or the newest MSVC toolset
(vswhere).
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def main(syntax: str, src: str, obj: str, fmt: str = "win64") -> int:
    if syntax == "nasm":
        tool = shutil.which("nasm")
        cmd = [tool, "-f", fmt, src, "-o", obj]
    elif syntax == "gas":
        tool = shutil.which("as")
        cmd = [tool, "--64", "-o", obj, src]
    elif syntax == "masm":
        from nvol.asm.masm import find_ml64
        tool = find_ml64()
        cmd = [tool, "-nologo", "-c", f"-Fo{obj}", src]
    else:
        print(f"unknown syntax {syntax!r}")
        return 1
    if not tool:
        print(f"no assembler for {syntax}")
        return 1
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
