// Copyright The Mirai Authors. SPDX-License-Identifier: MIT OR Apache-2.0
//
// simdjson's backslash_and_quote::copy_and_find against nvol_json_copy_and_find_*, both
// inlined into the same loop, for hardware counters (PENDING 37). simdjson's is its own
// (5.2.0, from the amalgamated simdjson.cpp this unit includes), called in each
// implementation's namespace from a function with that implementation's target.
//
//   stringsbench count IMPL N FILE   N passes over FILE in 32-byte steps, each step copied to a
//                                    buffer and its masks summed
//     IMPL: nvol-avx2, simdjson-haswell, nvol-sse2@westmere, simdjson-westmere
//   stringsbench same FILE           every implementation gives the same masks and copy
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "simdjson.cpp"
#include "nvol.hpp"

#define HASWELL __attribute__((target("avx2,bmi,pclmul,lzcnt,popcnt")))
#define WESTMERE __attribute__((target("sse4.2,pclmul,popcnt")))

template <class BQ> static inline uint64_t bq(BQ r) { return r.bs_bits | (uint64_t)r.quote_bits << 32; }

#define PASS(name, attr, expr)                                                                \
    __attribute__((noinline)) attr static uint64_t pass_##name(const uint8_t *s, size_t steps, \
                                                               uint8_t *d) {                  \
        uint64_t m = 0;                                                                      \
        for (size_t i = 0; i < steps; i++) {                                                 \
            const uint8_t *src = s + 32 * i;                                                 \
            uint8_t *dst = d + 32 * i;                                                       \
            m += expr;                                                                       \
        }                                                                                    \
        return m;                                                                            \
    }

PASS(nvol_avx2, HASWELL, nvol::raw::nvol_json_copy_and_find_avx2(src, dst))
PASS(simdjson_haswell, HASWELL, bq(simdjson::haswell::backslash_and_quote{}.copy_and_find(src, dst)))
PASS(nvol_sse2_westmere, WESTMERE, nvol::raw::nvol_json_copy_and_find_sse2(src, dst))
PASS(simdjson_westmere, WESTMERE, bq(simdjson::westmere::backslash_and_quote{}.copy_and_find(src, dst)))

struct Impl {
    const char *name;
    uint64_t (*pass)(const uint8_t *, size_t, uint8_t *);
};
static const Impl IMPLS[] = {{"nvol-avx2", pass_nvol_avx2}, {"simdjson-haswell", pass_simdjson_haswell},
                             {"nvol-sse2@westmere", pass_nvol_sse2_westmere},
                             {"simdjson-westmere", pass_simdjson_westmere}};

static std::vector<uint8_t> load(const char *path, size_t &steps) {
    FILE *f = std::fopen(path, "rb");
    if (!f) { std::perror(path); std::exit(2); }
    std::fseek(f, 0, SEEK_END);
    size_t len = (size_t)std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    steps = len / 32;
    std::vector<uint8_t> buf(32 * steps + 64);
    if (std::fread(buf.data(), 1, 32 * steps, f) != 32 * steps) { std::perror(path); std::exit(2); }
    std::fclose(f);
    return buf;
}

int main(int argc, char **argv) {
    size_t steps;
    if (argc == 3 && !std::strcmp(argv[1], "same")) {
        std::vector<uint8_t> s = load(argv[2], steps);
        std::vector<uint8_t> d0(s.size()), d(s.size());
        uint64_t m0 = IMPLS[1].pass(s.data(), steps, d0.data());
        int bad = 0;
        for (const Impl &m : IMPLS) {
            std::fill(d.begin(), d.end(), 0);
            if (m.pass(s.data(), steps, d.data()) != m0 || std::memcmp(d.data(), d0.data(), 32 * steps)) {
                std::printf("MISMATCH %s\n", m.name);
                bad = 1;
            }
        }
        std::printf("%s %s\n", argv[2], bad ? "differ" : "same");
        return bad;
    }
    if (argc != 5 || std::strcmp(argv[1], "count")) {
        std::fputs("usage: stringsbench count IMPL N FILE | stringsbench same FILE\n", stderr);
        return 2;
    }
    const Impl *m = nullptr;
    for (const Impl &x : IMPLS)
        if (!std::strcmp(x.name, argv[2])) m = &x;
    if (!m) { std::fprintf(stderr, "unknown IMPL %s\n", argv[2]); return 2; }
    long n = std::atol(argv[3]);
    std::vector<uint8_t> s = load(argv[4], steps);
    std::vector<uint8_t> d(s.size());
    uint64_t sum = 0;
    for (long k = 0; k < n; k++) {
        sum += m->pass(s.data(), steps, d.data());
        asm volatile("" : : "r"(d.data()) : "memory");
    }
    std::printf("%llu\n", (unsigned long long)sum);
    return 0;
}
