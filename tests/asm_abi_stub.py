"""A trampoline (NASM for x86-64, GNU as for AAPCS64) that calls a generated function the way the ABI says a caller may, and
checks what the ABI says the callee must keep:

    abi_call(fn, argv, out)   argv: 8 u64 arguments; out[0] = rax, out[1] = 0 when every
                              callee-saved register came back intact

Before the call it puts a distinct value in every callee-saved register (rbx, rbp, r12-r15, and
on Win64 rdi, rsi and xmm6-xmm15), passes arguments 5-8 (Win64) or 7-8 (System V) on the stack
and leaves the 32-byte shadow space on Win64. Narrow arguments arrive as the caller put them in
argv: the test fills their upper bits with garbage, which the callee must ignore."""

MAGIC = 0x5A17C0DE00000000


def _check(lines, reg, k):
    lines += [f"    mov rax, {MAGIC + k}", f"    cmp {reg}, rax", "    setne al", "    or r11b, al"]


def win64() -> str:
    gprs = ["rbx", "rbp", "rsi", "rdi", "r12", "r13", "r14", "r15"]
    L = ["default rel", "section .text", "global abi_call", "abi_call:"]
    L += [f"    push {r}" for r in gprs]
    # 8 pushes + return address = 72; 248 more keeps rsp 16-byte aligned at the call
    L += ["    sub rsp, 248",
          "    mov [rsp + 224], rcx", "    mov [rsp + 232], r8", "    mov rax, rdx"]
    for k in range(10):
        L.append(f"    movdqu [rsp + {64 + 16 * k}], xmm{6 + k}")
    for k in range(4):                          # arguments 5-8 above the shadow space
        L += [f"    mov r10, [rax + {32 + 8 * k}]", f"    mov [rsp + {32 + 8 * k}], r10"]
    for k, r in enumerate(gprs):
        L.append(f"    mov {r}, {MAGIC + k}")
    for k in range(10):
        L += [f"    mov r10, {MAGIC + 100 + k}", f"    movq xmm{6 + k}, r10"]
    L += ["    mov rcx, [rax]", "    mov rdx, [rax + 8]", "    mov r8, [rax + 16]",
          "    mov r9, [rax + 24]", "    call [rsp + 224]",
          "    mov r10, [rsp + 232]", "    mov [r10], rax", "    xor r11d, r11d"]
    for k, r in enumerate(gprs):
        _check(L, r, k)
    for k in range(10):
        L += [f"    movq rcx, xmm{6 + k}"]
        _check(L, "rcx", 100 + k)
    L += ["    mov [r10 + 8], r11"]
    for k in range(10):
        L.append(f"    movdqu xmm{6 + k}, [rsp + {64 + 16 * k}]")
    L += ["    add rsp, 248"] + [f"    pop {r}" for r in reversed(gprs)] + ["    ret", ""]
    return "\n".join(L)


def sysv() -> str:
    gprs = ["rbx", "rbp", "r12", "r13", "r14", "r15"]
    L = ["default rel", "section .note.GNU-stack noalloc noexec nowrite progbits",
         "section .text", "global abi_call", "abi_call:"]
    L += [f"    push {r}" for r in gprs]
    # 6 pushes + return address = 56; 40 more keeps rsp 16-byte aligned at the call
    L += ["    sub rsp, 40", "    mov [rsp + 16], rdi", "    mov [rsp + 24], rdx", "    mov rax, rsi",
          "    mov r10, [rax + 48]", "    mov [rsp], r10", "    mov r10, [rax + 56]", "    mov [rsp + 8], r10"]
    for k, r in enumerate(gprs):
        L.append(f"    mov {r}, {MAGIC + k}")
    L += ["    mov rdi, [rax]", "    mov rsi, [rax + 8]", "    mov rdx, [rax + 16]",
          "    mov rcx, [rax + 24]", "    mov r8, [rax + 32]", "    mov r9, [rax + 40]",
          "    call [rsp + 16]", "    mov r10, [rsp + 24]", "    mov [r10], rax", "    xor r11d, r11d"]
    for k, r in enumerate(gprs):
        _check(L, r, k)
    L += ["    mov [r10 + 8], r11", "    add rsp, 40"] + [f"    pop {r}" for r in reversed(gprs)] + \
         ["    ret", ""]
    return "\n".join(L)


def aapcs64() -> str:
    """The same trampoline for AAPCS64 (GNU as): arguments in x0-x7, a distinct value in each of
    x19-x28 and in the low 64 bits of v8-v15 (all AAPCS64 asks a callee to keep), checked after
    the call; x0 comes back in out[0]."""
    L = [".text", ".globl abi_call", ".type abi_call, %function", "abi_call:",
         "    sub sp, sp, #176",
         "    stp x29, x30, [sp, #0]", "    stp x19, x20, [sp, #16]", "    stp x21, x22, [sp, #32]",
         "    stp x23, x24, [sp, #48]", "    stp x25, x26, [sp, #64]", "    stp x27, x28, [sp, #80]",
         "    stp d8, d9, [sp, #96]", "    stp d10, d11, [sp, #112]", "    stp d12, d13, [sp, #128]",
         "    stp d14, d15, [sp, #144]", "    str x2, [sp, #160]", "    mov x16, x0", "    mov x17, x1"]
    for k in range(10):
        L += _a64_magic(f"x{19 + k}", MAGIC + k)
    for k in range(8):
        L += _a64_magic("x9", MAGIC + 100 + k) + [f"    fmov d{8 + k}, x9"]
    L += [f"    ldr x{k}, [x17, #{8 * k}]" for k in range(8)]
    L += ["    blr x16", "    ldr x2, [sp, #160]", "    str x0, [x2]", "    mov x11, #0"]
    for k in range(10):
        L += _a64_magic("x9", MAGIC + k) + [f"    cmp x{19 + k}, x9", "    cset x10, ne",
                                             "    orr x11, x11, x10"]
    for k in range(8):
        L += _a64_magic("x9", MAGIC + 100 + k) + [f"    fmov x12, d{8 + k}", "    cmp x12, x9",
                                                   "    cset x10, ne", "    orr x11, x11, x10"]
    L += ["    str x11, [x2, #8]",
          "    ldp x29, x30, [sp, #0]", "    ldp x19, x20, [sp, #16]", "    ldp x21, x22, [sp, #32]",
          "    ldp x23, x24, [sp, #48]", "    ldp x25, x26, [sp, #64]", "    ldp x27, x28, [sp, #80]",
          "    ldp d8, d9, [sp, #96]", "    ldp d10, d11, [sp, #112]", "    ldp d12, d13, [sp, #128]",
          "    ldp d14, d15, [sp, #144]", "    add sp, sp, #176", "    ret",
          '.section .note.GNU-stack,"",@progbits', ""]
    return "\n".join(L)


def _a64_magic(r: str, v: int) -> list:
    return [f"    movz {r}, #{v & 0xFFFF}"] + \
        [f"    movk {r}, #{(v >> s) & 0xFFFF}, lsl #{s}" for s in (16, 32, 48)]
