"""Every backend exports the same byte-set constants, with the generator's values
(the e2e patches size their states and pick kernels with them)."""
import re

import pytest

from nvol.__main__ import generate
from nvol.kernels import build_modules
from nvol.kernels.byteset import CONSTANTS

PATTERNS = {
    "c/nvol.h": r"#define NVOL_BYTESET_{n} (\d+)\b",
    "cpp/nvol.hpp": r"#define NVOL_BYTESET_{n} (\d+)\b",
    "rust/nvol.rs": r"pub const NVOL_BYTESET_{n}: usize = (\d+);",
    "odin/nvol.odin": r"^BYTESET_{n} :: (\d+)\b",
    "python/nvol_ops.py": r"^BYTESET_{n} = (\d+)\b",
}


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    out = tmp_path_factory.mktemp("consts")
    generate(build_modules(), ["c", "cpp", "rust", "odin", "python"], str(out))
    return {rel: (out / rel).read_text(encoding="utf-8") for rel in PATTERNS}


@pytest.mark.parametrize("rel", sorted(PATTERNS))
def test_byteset_constants(files, rel):
    text = files[rel]
    for name, value, _doc in CONSTANTS:
        found = {int(v) for v in re.findall(PATTERNS[rel].format(n=name), text, re.M)}
        assert found == {value}, (rel, name, value, found)
