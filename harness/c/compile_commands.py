"""Write compile_commands.json for the generated C (PENDING 3.2), so clangd, clang-check,
clang-tidy or Sourcetrail can index and navigate the emitted code.

    python harness/c/compile_commands.py [out.json]       (default: compile_commands.json at the repo root)

One entry per C translation unit of `generated/`: the portable C (`generated/c/nvol.c`) and
the glue of each assembly syntax (`generated/<nasm|masm|gas>/nvol.c`), with the flags the test
suite builds them with (tests/conftest.py). The paths are absolute, because a relative
"directory" is not resolved against the database's folder (clang-check: "Compile command not
found"); that is why the file is written by this tool and ignored by git instead of being
generated output.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLAGS = ["-std=c99", "-O2", "-Wall", "-Wextra", "-DNVOL_BUILD_DLL", "-DNVOL_TESTING"]


def entries(root: str = ROOT) -> list:
    out = []
    for sub in ("c", "nasm", "masm", "gas"):
        d = os.path.join(root, "generated", sub)
        src = os.path.join(d, "nvol.c")
        if os.path.exists(src):
            out.append({"directory": d.replace("\\", "/"), "file": src.replace("\\", "/"),
                        "arguments": ["cc", *FLAGS, "-c", src.replace("\\", "/")]})
    return out


def main(argv: list) -> int:
    path = argv[0] if argv else os.path.join(ROOT, "compile_commands.json")
    es = entries()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(es, fh, indent=2)
        fh.write("\n")
    print(f"wrote {path} ({len(es)} translation units)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
