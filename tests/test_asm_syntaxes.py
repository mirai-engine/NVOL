"""The assembly backend's syntaxes are one reference, not three: on this machine's ABI, every
syntax it can assemble gives the same machine code (nvol/asm/samebytes.py).

- NASM and GNU as: identical bytes, instruction by instruction (relocations masked).
- MASM (Windows): the same instructions and lengths; only the register-to-register direction
  may differ (`89 /r` against `8b /r`), which ml64 chooses and the source cannot.
- The function tables, the read-only constants and the unwind tables (Win64 .xdata/.pdata,
  System V .eh_frame) are the same bytes: NASM's unwind tables are NVOL's, ml64's and GNU as's
  come from their directives.
- Every instruction length NVOL computes (encode.length, with the jump sizes and alignment
  padding of encode.relax) is what GNU as assembled: the printers spell those jump sizes in
  every syntax."""
import shutil
import sys

import pytest

from nvol.asm import samebytes
from nvol.kernels import build_modules

ABI = "win64" if sys.platform == "win32" else "sysv"
MOD = build_modules()["x86_64"]


@pytest.fixture(scope="module")
def objs(tmp_path_factory):
    if not shutil.which("objdump"):
        pytest.skip("no objdump")
    built = samebytes.build(ABI, str(tmp_path_factory.mktemp("samebytes")), MOD)
    if len(built) < 2:
        pytest.skip(f"fewer than two assemblers here: {list(built)}")
    return {s: (samebytes.disassemble(o), samebytes.rodata(o), samebytes.unwind(o),
                samebytes.fn_tables(o, MOD)) for s, o in built.items()}


def _others(objs):
    return [s for s in objs if s != "nasm"]


def test_same_instructions(objs):
    ref = objs["nasm"][0][0]
    for other in _others(objs):
        st = samebytes.compare(ref, objs[other][0][0])
        assert st["diff"] == 0, (other, st["examples"][:10])
        if other == "gas":
            assert st["alt"] == 0, "NASM and GNU as must give identical bytes"


def test_same_data(objs):
    for other in _others(objs):
        assert objs["nasm"][3] == objs[other][3], (other, "function tables")
        assert objs["nasm"][1] == objs[other][1], (other, "read-only data")
        assert objs["nasm"][2] == objs[other][2], (other, "unwind tables (.xdata/.pdata, .eh_frame)")


def test_lengths_match_gnu_as(objs):
    if "gas" not in objs:
        pytest.skip("no GNU as")
    bad = samebytes.check_lengths(MOD, ABI, objs["gas"][0][0])
    assert not bad, bad[:10]
