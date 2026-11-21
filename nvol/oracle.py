"""Executable specifications of the public operations (the test oracle).

Written as the most obvious possible Python, independent of the IR, so that a
bug in a kernel and a bug in its spec are unlikely to coincide.
"""
NONE = (1 << 64) - 1


def find(needles, hay: bytes) -> int:
    for i, x in enumerate(hay):
        if x in needles:
            return i
    return NONE


def rfind(needles, hay: bytes) -> int:
    for i in range(len(hay) - 1, -1, -1):
        if hay[i] in needles:
            return i
    return NONE


def count(needle: int, hay: bytes) -> int:
    return sum(1 for x in hay if x == needle)


def first_non_ascii(hay: bytes) -> int:
    for i, x in enumerate(hay):
        if x >= 0x80:
            return i
    return NONE


def memmem(hay: bytes, needle: bytes) -> int:
    """Index of the first occurrence of needle in hay (0 for an empty needle)."""
    i = hay.find(needle)
    return NONE if i < 0 else i


def teddy_buildable(patterns, avx2=True, ssse3=True, arch="x86_64") -> bool:
    """aho-corasick 1.1.5 packed Builder::build -> teddy::Builder::build is Some?"""
    if not patterns or any(len(p) == 0 for p in patterns) or len(patterns) > 64:
        return False
    if arch == "aarch64":
        return True         # NEON branch logs its limits but never rejects
    if not (avx2 or ssse3):
        return False
    m = min(4, min(len(p) for p in patterns))
    return not (m == 1 and len(patterns) > 16)


def leftmost_first(patterns, hay: bytes, start: int = 0, end=None):
    """(pattern id, start, end) of the leftmost-first match in hay[start:end], or None."""
    end = len(hay) if end is None else end
    for at in range(start, end):
        for pid, p in enumerate(patterns):
            if at + len(p) <= end and hay[at:at + len(p)] == p:
                return pid, at, at + len(p)
    return None


def expected(api_name: str, needles, hay: bytes) -> int:
    if api_name.startswith("nvol_memrchr"):
        return rfind(needles, hay)
    if api_name.startswith("nvol_memchr"):
        return find(needles, hay)
    if api_name.startswith("nvol_count"):
        return count(needles[0], hay)
    if api_name == "nvol_first_non_ascii":
        return first_non_ascii(hay)
    raise KeyError(api_name)
