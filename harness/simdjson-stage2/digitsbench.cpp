// Copyright The Mirai Authors. SPDX-License-Identifier: MIT OR Apache-2.0
//
// simdjson's eight-digit routines against NVOL's, both inlined into the same loop, for
// hardware counters (PENDING 37). simdjson's are its own (5.2.0), called in the namespace of
// each implementation from a function with that implementation's target: this unit includes
// the amalgamated simdjson.cpp, where every implementation's code is (simdjson.h holds only the
// built-in one). NVOL's come from its C++ header, whose ISA functions are inline.
//
//   digitsbench count IMPL MODE N   N passes over 4096 eight-digit numbers
//     IMPL: nvol-sse42@haswell, simdjson-haswell, nvol-sse42@westmere, simdjson-westmere,
//           nvol-swar, simdjson-fallback (parse_eight_digits); nvol-is, simdjson-is
//           (is_made_of_eight_digits_fast, in haswell's target)
//     MODE: tput (independent calls, summed) | chain (acc = acc * 10^8 + f(p), as
//           parse_fraction_digits does)
//   digitsbench same                every implementation agrees with the others on the numbers
//                                   and on 256 x 8 non-digit variants (exit 1 if not)
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "simdjson.cpp"
#include "nvol.hpp"

using nvol::raw::nvol_is_eight_digits;
using nvol::raw::nvol_parse_eight_digits_scalar;
using nvol::raw::nvol_parse_eight_digits_sse42;

static const int COUNT = 4096;
#define HASWELL __attribute__((target("avx2,bmi,pclmul,lzcnt,popcnt")))
#define WESTMERE __attribute__((target("sse4.2,pclmul,popcnt")))

// one pass: f on each number; a noinline function per (IMPL, MODE), the call inlined in it
#define PASS(name, attr, call)                                                               \
    __attribute__((noinline)) attr static uint64_t tput_##name(const uint8_t *b) {          \
        uint64_t s = 0;                                                                     \
        for (int i = 0; i < COUNT; i++) s += call(b + 8 * i);                                \
        return s;                                                                           \
    }                                                                                       \
    __attribute__((noinline)) attr static uint64_t chain_##name(const uint8_t *b) {         \
        uint64_t s = 0;                                                                     \
        for (int i = 0; i < COUNT; i++) s = s * 100000000 + call(b + 8 * i);                 \
        return s;                                                                           \
    }

PASS(nvol_sse42_haswell, HASWELL, nvol_parse_eight_digits_sse42)
PASS(simdjson_haswell, HASWELL, simdjson::haswell::numberparsing::parse_eight_digits_unrolled)
PASS(nvol_sse42_westmere, WESTMERE, nvol_parse_eight_digits_sse42)
PASS(simdjson_westmere, WESTMERE, simdjson::westmere::numberparsing::parse_eight_digits_unrolled)
PASS(nvol_swar, , nvol_parse_eight_digits_scalar)
PASS(simdjson_fallback, , simdjson::fallback::numberparsing::parse_eight_digits_unrolled)
PASS(nvol_is, HASWELL, nvol_is_eight_digits)
PASS(simdjson_is, HASWELL, simdjson::haswell::numberparsing::is_made_of_eight_digits_fast)

struct Impl {
    const char *name;
    uint64_t (*tput)(const uint8_t *);
    uint64_t (*chain)(const uint8_t *);
};
#define IMPL(s, n) {s, tput_##n, chain_##n}
static const Impl IMPLS[] = {
    IMPL("nvol-sse42@haswell", nvol_sse42_haswell), IMPL("simdjson-haswell", simdjson_haswell),
    IMPL("nvol-sse42@westmere", nvol_sse42_westmere), IMPL("simdjson-westmere", simdjson_westmere),
    IMPL("nvol-swar", nvol_swar), IMPL("simdjson-fallback", simdjson_fallback),
    IMPL("nvol-is", nvol_is), IMPL("simdjson-is", simdjson_is),
};

// the numbers, 8 bytes each, then 16 bytes of padding (simdjson's SIMD routine loads 16)
static std::vector<uint8_t> numbers() {
    std::vector<uint8_t> b(8 * COUNT + 16, '0');
    std::mt19937_64 rng(37);
    for (int i = 0; i < 8 * COUNT; i++) b[i] = (uint8_t)('0' + rng() % 10);
    return b;
}

static const Impl *find(const char *name) {
    for (const Impl &m : IMPLS)
        if (!std::strcmp(m.name, name)) return &m;
    std::fprintf(stderr, "unknown IMPL %s\n", name);
    std::exit(2);
}

HASWELL static int same_is(uint8_t *p) {
    return (int)nvol_is_eight_digits(p) == (int)simdjson::haswell::numberparsing::is_made_of_eight_digits_fast(p);
}

static int same() {
    std::vector<uint8_t> b = numbers();
    int bad = 0;
    for (const Impl &m : IMPLS) {
        uint64_t t = m.tput(b.data()), c = m.chain(b.data());
        uint64_t t0 = IMPLS[1].tput(b.data()), c0 = IMPLS[1].chain(b.data());
        bool is = std::strstr(m.name, "-is") != nullptr;
        if (is ? t != (uint64_t)COUNT : (t != t0 || c != c0)) {
            std::printf("MISMATCH %s\n", m.name);
            bad = 1;
        }
    }
    uint8_t v[24];
    for (int i = 0; i < 8; i++)
        for (int c = 0; c < 256; c++) {
            std::memset(v, '5', sizeof v);
            v[i] = (uint8_t)c;
            if (!same_is(v)) {
                std::printf("MISMATCH is_eight_digits byte %d = %d\n", i, c);
                bad = 1;
            }
        }
    std::printf("%s\n", bad ? "differ" : "same");
    return bad;
}

int main(int argc, char **argv) {
    if (argc == 2 && !std::strcmp(argv[1], "same")) return same();
    if (argc != 5 || std::strcmp(argv[1], "count")) {
        std::fputs("usage: digitsbench count IMPL tput|chain N | digitsbench same\n", stderr);
        return 2;
    }
    const Impl *m = find(argv[2]);
    bool chain = !std::strcmp(argv[3], "chain");
    long n = std::atol(argv[4]);
    std::vector<uint8_t> b = numbers();
    uint64_t s = 0;
    for (long k = 0; k < n; k++) {
        s += chain ? m->chain(b.data()) : m->tput(b.data());
        asm volatile("" : : "r"(b.data()) : "memory");   // no pass folded into the next
    }
    std::printf("%llu\n", (unsigned long long)s);
    return 0;
}
