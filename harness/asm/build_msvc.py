"""Build a committed generated/<syntax> library with MSVC: the assembly object (already
assembled, by NASM, ml64 or GNU as) and the C glue compiled by cl (/W4 /WX), linked by link.exe
into a DLL. The compiler environment comes from vcvars64.bat of the newest Visual Studio with
the C++ tools (found with vswhere).

    python harness/asm/build_msvc.py <kernels.obj> <out.dll> [nasm|masm|gas]

link.exe reads the `-export:` directives of an object only from a `.drectve` section marked
IMAGE_SCN_LNK_INFO. NASM and ml64 mark it; GNU as (2.42, COFF) writes it as plain writable data
and cannot set the flag, so link.exe would build a DLL without exports. For such an object the
exports are read from the section and passed to link.exe in a .def file.
"""
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VSWHERE = os.environ.get("VSWHERE", r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
LNK_INFO = 0x200


def unread_exports(obj: str) -> list:
    """The `-export:` names of a COFF object's `.drectve` when link.exe would ignore it."""
    b = open(obj, "rb").read()
    nsec, opt = struct.unpack_from("<H", b, 2)[0], struct.unpack_from("<H", b, 16)[0]
    for i in range(nsec):
        h = 20 + opt + 40 * i
        if b[h:h + 8].rstrip(b"\0") != b".drectve":
            continue
        size, ptr = struct.unpack_from("<II", b, h + 16)
        flags = struct.unpack_from("<I", b, h + 36)[0]
        if flags & LNK_INFO:
            return []
        words = b[ptr:ptr + size].decode("ascii").split()
        return [w[len("-export:"):].strip('"') for w in words if w.startswith("-export:")]
    return []


def vs_path():
    """The newest Visual Studio with the C++ tools, or None."""
    if not os.path.exists(VSWHERE):
        return None
    vs = subprocess.run([VSWHERE, "-latest", "-products", "*", "-requires",
                         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                         "installationPath"], capture_output=True, text=True).stdout.strip()
    return vs or None


def build_dll(src: str, objs: list, out: str, flags: str = "/O2 /W4 /WX") -> int:
    """cl `src` (the C, with the test hook) plus `objs` into the DLL `out`, in vcvars64's
    environment: the exports of an object whose .drectve link.exe ignores go in a .def."""
    vs = vs_path()
    if vs is None:
        print("no MSVC (vswhere found no VC tools)")
        return 1
    out = os.path.abspath(out)
    work = os.path.dirname(out)
    exports = [n for o in objs for n in unread_exports(o)]
    link = ""
    if exports:
        print(f"{len(exports)} exports from .drectve sections link.exe ignores, passed in a .def file")
        dfile = os.path.join(work, "exports.def")
        with open(dfile, "w") as fh:
            fh.write("EXPORTS\n" + "".join(f"    {n}\n" for n in exports))
        link = f' /link /DEF:"{dfile}"'
    stem = os.path.splitext(os.path.basename(out))[0]
    bat = os.path.join(work, f"{stem}_msvc.bat")
    objs_q = " ".join(f'"{os.path.abspath(o)}"' for o in objs)
    with open(bat, "w") as fh:
        fh.write(f'@call "{os.path.join(vs, "VC", "Auxiliary", "Build", "vcvars64.bat")}" >nul || exit /b 1\n')
        fh.write(f'cl /nologo {flags} /DNVOL_BUILD_DLL /DNVOL_TESTING /LD "{src}" {objs_q} '
                 f'/Fo"{os.path.join(work, stem + "_msvc.obj")}" /Fe"{out}"{link} || exit /b 1\n')
    return subprocess.run(["cmd", "/c", bat], cwd=work, stdout=subprocess.DEVNULL).returncode


def main(obj: str, out: str, syntax: str = "nasm") -> int:
    return build_dll(os.path.join(ROOT, "generated", syntax, "nvol.c"), [obj], out)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
