"""A NASM trampoline that calls a generated function the way the ABI says a caller may, and
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
