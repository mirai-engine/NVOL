/* Byte-set search: NVOL (exact nibble lookup, cost independent of N) vs the
 * technique in ugrep 7.8.5 / RE/flex matcher_avx2.cpp (N cmpeq + or per vector),
 * for "absent" scans (no hit: pure throughput) and with sparse hits.
 *
 *   gcc -O2 -mavx2 -I generated/c harness/ugrep-e2e/setbench.c generated/c/nvol.c -o setbench
 *
 * The ugrep-style loops below follow simd_advance_pattern_pinN_one_avx2 and
 * _pma_avx2 without the predict_match call (they stop at the first candidate,
 * like NVOL's kernels), so both sides do the same job. */
#include <immintrin.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "nvol.h"

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

/* ugrep-style: first byte of [s, e) in chr[0..n) (n <= 16) */
/* always_inline + a constant n at every call site: the loops over i unroll, one
 * specialized body per N, as ugrep gets from its ADV_PAT_PIN_ONE macros */
static inline __attribute__((always_inline))
const uint8_t *chain_one_n(const uint8_t *chr, const int n, const uint8_t *s, const uint8_t *e)
{
  __m256i v[16];
  _Pragma("GCC unroll 16") for (int i = 0; i < n; i++) v[i] = _mm256_set1_epi8((char)chr[i]);
  while (s <= e - 32) {
    __m256i x = _mm256_loadu_si256((const __m256i *)s);
    __m256i q = _mm256_cmpeq_epi8(v[0], x);
    _Pragma("GCC unroll 16") for (int i = 1; i < n; i++) q = _mm256_or_si256(q, _mm256_cmpeq_epi8(v[i], x));
    uint32_t m = (uint32_t)_mm256_movemask_epi8(q);
    if (m) return s + __builtin_ctz(m);
    s += 32;
  }
  for (; s < e; s++)
    for (int i = 0; i < n; i++) if (*s == chr[i]) return s;
  return NULL;
}

/* ugrep-style: first p with p[i1] in a[0..n) and p[i2] in b[0..n) */
static inline __attribute__((always_inline))
const uint8_t *chain_pair_n(const uint8_t *a, const uint8_t *b, const int n, size_t i1, size_t i2,
                             const uint8_t *s, const uint8_t *e)
{
  __m256i va[16], vb[16];
  _Pragma("GCC unroll 16") for (int i = 0; i < n; i++) { va[i] = _mm256_set1_epi8((char)a[i]); vb[i] = _mm256_set1_epi8((char)b[i]); }
  size_t reach = i1 > i2 ? i1 : i2;
  while (s + reach + 32 <= e) {
    __m256i x1 = _mm256_loadu_si256((const __m256i *)(s + i1));
    __m256i x2 = _mm256_loadu_si256((const __m256i *)(s + i2));
    __m256i q1 = _mm256_cmpeq_epi8(va[0], x1), q2 = _mm256_cmpeq_epi8(vb[0], x2);
    _Pragma("GCC unroll 16") for (int i = 1; i < n; i++) {
      q1 = _mm256_or_si256(q1, _mm256_cmpeq_epi8(va[i], x1));
      q2 = _mm256_or_si256(q2, _mm256_cmpeq_epi8(vb[i], x2));
    }
    uint32_t m = (uint32_t)_mm256_movemask_epi8(_mm256_and_si256(q1, q2));
    if (m) return s + __builtin_ctz(m);
    s += 32;
  }
  for (; s + reach < e; s++) {
    int h1 = 0, h2 = 0;
    for (int i = 0; i < n; i++) { h1 |= s[i1] == a[i]; h2 |= s[i2] == b[i]; }
    if (h1 && h2) return s;
  }
  return NULL;
}

#define SPECIALIZE(N)   static const uint8_t *chain_one_##N(const uint8_t *c, const uint8_t *s, const uint8_t *e) { return chain_one_n(c, N, s, e); }   static const uint8_t *chain_pair_##N(const uint8_t *a, const uint8_t *b, size_t i1, size_t i2, const uint8_t *s, const uint8_t *e)   { return chain_pair_n(a, b, N, i1, i2, s, e); }
SPECIALIZE(2) SPECIALIZE(3) SPECIALIZE(4) SPECIALIZE(5) SPECIALIZE(6) SPECIALIZE(7) SPECIALIZE(8) SPECIALIZE(16)

static const uint8_t *chain_one(const uint8_t *c, int n, const uint8_t *s, const uint8_t *e)
{
  switch (n) { case 2: return chain_one_2(c, s, e); case 3: return chain_one_3(c, s, e); case 4: return chain_one_4(c, s, e); case 5: return chain_one_5(c, s, e); case 6: return chain_one_6(c, s, e); case 7: return chain_one_7(c, s, e);
               case 8: return chain_one_8(c, s, e); default: return chain_one_16(c, s, e); }
}

static const uint8_t *chain_pair(const uint8_t *a, const uint8_t *b, int n, size_t i1, size_t i2, const uint8_t *s, const uint8_t *e)
{
  switch (n) { case 2: return chain_pair_2(a, b, i1, i2, s, e); case 3: return chain_pair_3(a, b, i1, i2, s, e);
               case 4: return chain_pair_4(a, b, i1, i2, s, e); case 5: return chain_pair_5(a, b, i1, i2, s, e); case 6: return chain_pair_6(a, b, i1, i2, s, e); case 7: return chain_pair_7(a, b, i1, i2, s, e); case 8: return chain_pair_8(a, b, i1, i2, s, e);
               default: return chain_pair_16(a, b, i1, i2, s, e); }
}

/* count every hit by restarting after each one, as ugrep's advance loop does */
#define SCAN(EXPR) do { const uint8_t *p = h, *r; size_t cnt = 0; \
  while ((r = (EXPR)) != NULL) { cnt++; p = r + 1; } sink += cnt; } while (0)

static volatile size_t sink;

static double best_of(int rounds, double (*f)(void *), void *ctx)
{
  double b = 1e30;
  for (int i = 0; i < rounds; i++) { double t = f(ctx); if (t < b) b = t; }
  return b;
}

typedef struct { const uint8_t *h; size_t len; int n; const uint8_t *set1, *set2; size_t i1, i2;
                 uint8_t cls[NVOL_BYTESET_CLASS_BYTES], pst[NVOL_BYTESET_PAIR_BYTES]; int iters; } Ctx;

static double t_nvol_one(void *v) { Ctx *c = v; const uint8_t *h = c->h, *e = h + c->len; double t = now();
  for (int k = 0; k < c->iters; k++) SCAN(nvol_find_byteset_raw(c->cls, p, e)); return now() - t; }
static double t_chain_one(void *v) { Ctx *c = v; const uint8_t *h = c->h, *e = h + c->len; double t = now();
  for (int k = 0; k < c->iters; k++) SCAN(chain_one(c->set1, c->n, p, e)); return now() - t; }
static double t_nvol_pair(void *v) { Ctx *c = v; const uint8_t *h = c->h, *e = h + c->len; double t = now();
  for (int k = 0; k < c->iters; k++) SCAN(nvol_find_byteset_pair_raw(c->pst, p, e)); return now() - t; }
static double t_chain_pair(void *v) { Ctx *c = v; const uint8_t *h = c->h, *e = h + c->len; double t = now();
  for (int k = 0; k < c->iters; k++) SCAN(chain_pair(c->set1, c->set2, c->n, c->i1, c->i2, p, e)); return now() - t; }

int main(void)
{
  const size_t sizes[] = {64 << 10, 1 << 20};
  const int ns[] = {2, 3, 4, 5, 6, 7, 8, 16};
  /* the haystack avoids bytes >= 0x80 and the set members; "sparse" plants a hit every ~4 KiB */
  uint8_t set1[16], set2[16];
  for (int i = 0; i < 16; i++) { set1[i] = (uint8_t)(0x80 + i * 7); set2[i] = (uint8_t)(0xC0 + i * 3); }
  printf("GB/s, best of 15 (interleaved A/B); delta = NVOL vs ugrep-style compare chain\n");
  for (int sparse = 0; sparse < 2; sparse++)
    for (size_t si = 0; si < 2; si++) {
      size_t len = sizes[si];
      uint8_t *h = malloc(len + 64);
      uint64_t x = 0x9E3779B97F4A7C15ull;
      for (size_t i = 0; i < len + 64; i++) { x ^= x << 13; x ^= x >> 7; x ^= x << 17; h[i] = (uint8_t)(0x20 + x % 0x5F); }
      if (sparse) for (size_t i = 0; i + 64 < len; i += 4096) { h[i + 3] = set1[(i >> 12) % 16 % 2]; h[i + 3 + 5] = set2[0]; }
      for (int ni = 0; ni < 8; ni++) {
        Ctx c = {h, len, ns[ni], set1, set2, 3, 8, {0}, {0}, (int)((64u << 20) / len)};
        nvol_byteset_init(set1, (size_t)c.n, c.cls);
        nvol_byteset_pair_init(set1, (size_t)c.n, set2, (size_t)c.n, c.i1, c.i2, c.pst);
        double a = 1e30, b = 1e30, p = 1e30, q = 1e30;
        for (int r = 0; r < 15; r++) {   /* interleaved, alternating order */
          if (r & 1) { b = fmin(b, t_chain_one(&c)); a = fmin(a, t_nvol_one(&c)); q = fmin(q, t_chain_pair(&c)); p = fmin(p, t_nvol_pair(&c)); }
          else       { a = fmin(a, t_nvol_one(&c)); b = fmin(b, t_chain_one(&c)); p = fmin(p, t_nvol_pair(&c)); q = fmin(q, t_chain_pair(&c)); }
        }
        double gb = (double)len * c.iters / 1e9;
        printf("%-6s %7zu KiB  N=%-2d  one: nvol %6.2f  chain %6.2f  %+6.1f%%   pair: nvol %6.2f  chain %6.2f  %+6.1f%%\n",
               sparse ? "sparse" : "absent", len >> 10, c.n, gb / a, gb / b, (b / a - 1) * 100, gb / p, gb / q, (q / p - 1) * 100);
      }
      free(h);
    }
  (void)best_of;
  return 0;
}
