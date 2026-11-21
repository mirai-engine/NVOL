"""`generated/` must be exactly what the generator writes today.

The other tests regenerate into a temporary directory, while the Rust and Odin
harnesses and the end-to-end patches build the committed `generated/`: a stale
`generated/` would pass pytest and still ship. Fix a failure with
`python -m nvol gen --out generated`.
"""
import difflib
import os

from nvol.__main__ import LANGS, generate
from nvol.kernels import build_modules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATED = os.path.join(ROOT, "generated")
SOURCE_EXT = (".c", ".h", ".hpp", ".rs", ".py", ".odin", ".asm", ".s")


def _committed():
    out = set()
    for d, _, names in os.walk(GENERATED):
        for n in names:
            if n.endswith(SOURCE_EXT):
                out.add(os.path.relpath(os.path.join(d, n), GENERATED).replace(os.sep, "/"))
    return out


def test_generated_matches_generator(tmp_path):
    written = generate(build_modules(), LANGS, str(tmp_path))
    fresh = {os.path.relpath(p, tmp_path).replace(os.sep, "/") for p in written}
    problems = []
    for rel in sorted(fresh):
        new = (tmp_path / rel).read_bytes()
        path = os.path.join(GENERATED, rel)
        if not os.path.exists(path):
            problems.append(f"{rel}: missing from generated/")
            continue
        old = open(path, "rb").read()
        if old != new:
            diff = difflib.unified_diff(old.decode("utf-8", "replace").splitlines(),
                                        new.decode("utf-8", "replace").splitlines(),
                                        f"generated/{rel}", "generator", lineterm="", n=1)
            problems.append(f"{rel}: differs\n" + "\n".join(list(diff)[:20]))
    for rel in sorted(_committed() - fresh):
        problems.append(f"{rel}: in generated/ but no longer generated")
    assert not problems, ("generated/ is out of date (python -m nvol gen --out generated):\n"
                          + "\n".join(problems))
