"""Executable specifications of the public operations (the test oracle).

Written as the most obvious possible Python, independent of the IR, so that a
bug in a kernel and a bug in its spec are unlikely to coincide.
"""
import codecs

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


# ---- simdjson stage 1 (kernels/json.py) ----------------------------------------------------
JSON_WS = b" \t\n\r"
# classify's operators: (c | 0x20) in ':', '{', ',', '}', which also takes [ ] and the control
# bytes 0x0C (FF) and 0x1A (SUB), as simdjson does (stage 2 rejects those two)
JSON_OP = {0x3A, 0x7B, 0x2C, 0x7D}


def utf8_valid(data: bytes) -> int:
    """1 when data is valid UTF-8 (RFC 3629), as Python's strict decoder says, else 0."""
    try:
        data.decode("utf-8", "strict")
        return 1
    except UnicodeDecodeError:
        return 0


def json_index(data: bytes, idx: int = 0, state=(0, 0, 0, 0)):
    """simdjson's stage 1 output, byte by byte, written from the definitions rather than from the
    mask arithmetic: (positions, (next_is_escaped, prev_in_string, prev_scalar, error)).

    A byte is escaped when the byte before it is a backslash that is not escaped itself (this
    holds inside and outside strings, as simdjson's escape scanner runs on the whole block). An
    unescaped quote toggles "in string"; the opening quote counts as inside, the closing one as
    outside. Outside strings, a structural position is an operator, or the first byte of a run
    of scalar bytes (neither whitespace nor operator); a quote ends a run, so the byte after a
    closing quote starts one. A control byte (<= 0x1F) inside a string sets the error."""
    esc, in_str, prev_scalar, err = state[0] & 1, state[1] != 0, state[2] & 1, state[3] != 0
    out = []
    for i, c in enumerate(data):
        escaped = esc
        esc = 1 if c == 0x5C and not escaped else 0
        quote = c == 0x22 and not escaped
        if quote:
            in_str = not in_str
        tail = in_str != quote             # in_string ^ quote: inside, closing quote excluded
        op = (c | 0x20) in JSON_OP
        scalar = not op and c not in JSON_WS
        if not tail and (op or (scalar and not prev_scalar)):
            out.append(idx + i)
        if c <= 0x1F and in_str:
            err = True
        prev_scalar = 1 if scalar and not quote else 0
    mask = (1 << 64) - 1
    # UTF-8 (simdjson's utf8_checker, from the start of the document): an invalid sequence is
    # an error at once; a code point the bytes end inside is "incomplete"
    dec = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        dec.decode(data, final=False)
        bad, inc = 0, int(dec.getstate()[0] != b"")
    except UnicodeDecodeError:
        bad, inc = 1, 0
    return out, (esc, mask if in_str else 0, prev_scalar, int(err), bad, inc, data[-3:])
