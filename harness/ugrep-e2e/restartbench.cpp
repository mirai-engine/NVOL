// Pair-set candidate loop in isolation: ugrep's own AVX2 loop (simd_advance_pattern_pin2_pma_avx2)
// against NVOL's k2 kernels, with an opaque predict() standing in for predict_match. Separates the
// cost of the candidate search from the rest of ugrep. "nvol mask" returns every chunk with
// candidates and is called again (its setup each time); "nvol visit" hands them to a visitor that
// the compiler inlines into the kernel's loop.
//
//   g++ -O2 -std=gnu++17 -I<NVOL>/generated/cpp restartbench.cpp -o restartbench
//   ./restartbench <file> <set1> <set2> <lcs> [rounds]       e.g.  en64.txt HW o 4
#include <nvol.hpp>
#include <immintrin.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <algorithm>

using namespace nvol::raw;

static const uint8_t *g_buf;
static size_t g_end;
__attribute__((noinline)) static bool predict(size_t k) {   // opaque: rejects every candidate
  asm volatile("" : : "r"(k) : "memory");
  return g_buf[k] == 0xFF && g_buf[k + 1] == 0xFE;
}

// ugrep 7.8.5, simd_advance_pattern_pin2_pma_avx2 with lcp = 0 (needles chr[0..2) at 0, chr[2..4) at lcs)
__attribute__((target("avx2"), noinline)) static size_t ugrep_loop(const char *chr, size_t lcs) {
  size_t hits = 0;
  const char *buf = reinterpret_cast<const char*>(g_buf);
  __m256i v0 = _mm256_set1_epi8(chr[0]), v1 = _mm256_set1_epi8(chr[1]);
  __m256i w0 = _mm256_set1_epi8(chr[2]), w1 = _mm256_set1_epi8(chr[3]);
  const char *s = buf, *e = buf + g_end - lcs;
  while (s <= e - 32) {
    __m256i a = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(s));
    __m256i b = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(s + lcs));
    __m256i ea = _mm256_or_si256(_mm256_cmpeq_epi8(v0, a), _mm256_cmpeq_epi8(v1, a));
    __m256i eb = _mm256_or_si256(_mm256_cmpeq_epi8(w0, b), _mm256_cmpeq_epi8(w1, b));
    uint32_t mask = _mm256_movemask_epi8(_mm256_and_si256(ea, eb));
    while (mask != 0) {
      size_t k = s - buf + __builtin_ctz(mask);
      if (predict(k)) return k;
      ++hits;
      mask &= mask - 1;
    }
    s += 32;
  }
  return hits;
}

// NVOL, one chunk per call (the _mask kernels), switch on the selector, as in the first patch
__attribute__((target("avx2"), noinline)) static size_t nvol_mask_loop(const uint8_t *st) {
  size_t hits = 0;
  uint64_t out[2];
  const uint8_t *s = g_buf, *e = g_buf + g_end;
  while (s < e) {
    const uint8_t *at = nvol_find_byteset_pair_mask_avx2_k2(st, s, e, reinterpret_cast<uint8_t*>(out));
    if (!at) break;
    uint64_t m = out[0];
    do {
      size_t k = at - g_buf + __builtin_ctzll(m);
      if (predict(k)) return k;
      ++hits;
      m &= m - 1;
    } while (m);
    s = at + out[1];
  }
  return hits;
}

// NVOL visitor kernel (generated): every chunk with candidates goes to the visitor, inlined here
struct VisitCtx { size_t hits, found; };
static inline int visit_fn(void *c, const uint8_t *at, uint64_t m) {
  VisitCtx *x = static_cast<VisitCtx*>(c);
  do {
    size_t k = at - g_buf + __builtin_ctzll(m);
    if (predict(k)) { x->found = k; return 1; }
    ++x->hits;
    m &= m - 1;
  } while (m);
  return 0;
}

__attribute__((target("avx2"), noinline)) static size_t nvol_visit_loop(const uint8_t *st) {
  VisitCtx c = {0, 0};
  const nvol_visitor v = {visit_fn, &c};
  const uint8_t *at = nvol_find_byteset_pair_visit_avx2_k2(st, g_buf, g_buf + g_end, &v);
  return at ? c.found : c.hits;
}

int main(int argc, char **argv) {
  if (argc < 5) { std::fprintf(stderr, "usage: restartbench file set1 set2 lcs [rounds]\n"); return 2; }
  FILE *f = std::fopen(argv[1], "rb");
  std::vector<uint8_t> v;
  { uint8_t tmp[1 << 16]; size_t n; while ((n = std::fread(tmp, 1, sizeof tmp, f)) > 0) v.insert(v.end(), tmp, tmp + n); }
  std::fclose(f);
  v.resize(v.size() + 64, 0);
  g_buf = v.data(); g_end = v.size() - 64;
  const char *s1 = argv[2], *s2 = argv[3];
  size_t lcs = std::strtoul(argv[4], 0, 10);
  int rounds = argc > 5 ? std::atoi(argv[5]) : 15;
  char chr[4] = {s1[0], s1[1] ? s1[1] : s1[0], s2[0], s2[1] ? s2[1] : s2[0]};
  uint8_t st[NVOL_BYTESET_PAIR_BYTES];
  nvol_byteset_pair_init(reinterpret_cast<const uint8_t*>(s1), std::strlen(s1),
                         reinterpret_cast<const uint8_t*>(s2), std::strlen(s2), 0, lcs, st);
  std::vector<double> t[3];
  size_t r[3];
  for (int i = 0; i < rounds; ++i) {
    for (int j = 0; j < 3; ++j) {
      int w = (i + j) % 3;
      auto a = std::chrono::steady_clock::now();
      r[w] = w == 0 ? ugrep_loop(chr, lcs) : w == 1 ? nvol_mask_loop(st) : nvol_visit_loop(st);
      auto b = std::chrono::steady_clock::now();
      t[w].push_back(std::chrono::duration<double, std::milli>(b - a).count());
    }
  }
  const char *nm[3] = {"ugrep loop", "nvol mask", "nvol visit"};
  for (int w = 0; w < 3; ++w) {
    std::sort(t[w].begin(), t[w].end());
    std::printf("%-11s candidates %8zu  min %7.2f ms  median %7.2f ms\n", nm[w], r[w], t[w][0], t[w][t[w].size() / 2]);
  }
}
