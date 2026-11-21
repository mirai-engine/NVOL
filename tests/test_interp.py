"""The kernels themselves (IR) against the oracle, under strict UB checks, for every
generated architecture (x86_64 and aarch64)."""
import pytest

from nvol.__main__ import check
from nvol.kernels import build_modules

MODULES = build_modules()


@pytest.mark.parametrize("arch", sorted(MODULES))
def test_interpreter_matches_oracle(arch):
    assert check(MODULES[arch], cases=20, seed=1234) == 0
