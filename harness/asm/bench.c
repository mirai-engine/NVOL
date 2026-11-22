/* One operation of the NVOL library, N times: for hardware counters (harness/asm/counters.sh)
 * or, with `time`, for a wall-clock timing where there are no counters (Windows, MSVC:
 * harness/asm/wtime.py). The same source is linked against the C library (GCC, Clang, MSVC)
 * or against NVOL's own assembly (the C glue in asm mode plus the kernels object).
 *
 *     bench OP SIZE N PATH [time]
 *     OP: memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem memmem2
 *         byteset byteset_not pair teddy
 *     PATH: -1 autodetect, 0 SSE2/SSSE3, 1 AVX2, 2 SSE4.2 without AVX2 (the test hook)
 *     time: run the N calls 9 times and print the best nanoseconds per call
 *
 * Every call scans the whole buffer (nothing matches): the kernels' steady state plus their
 * entry and tail at that size. Built with NVOL_TESTING defined. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef NVOL_BENCH_NO_C_HEADER          /* harness/parity/bench_cpp.cpp: the C++ backend */
#include "nvol.h"
#endif

#ifdef _MSC_VER
#  define ALIGNED64 __declspec(align(64))
#else
#  define ALIGNED64 __attribute__((aligned(64)))
#endif
#ifdef _WIN32
#  include <windows.h>
static double now_ns(void) {
    LARGE_INTEGER t, f;
    QueryPerformanceCounter(&t);
    QueryPerformanceFrequency(&f);
    return (double)t.QuadPart * 1e9 / (double)f.QuadPart;
}
#else
#  include <time.h>
static double now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec * 1e9 + (double)t.tv_nsec;
}
#endif

#ifdef NVOL_BENCH_NO_TEST_HOOK
static void nvol_test_set_avx2(int v) { (void)v; }
#else
void nvol_test_set_avx2(int v);
#endif

ALIGNED64 static uint8_t buf[(1 << 20) + 64];
ALIGNED64 static uint8_t spaces[(1 << 20) + 64];
static uint8_t cls[NVOL_BYTESET_CLASS_BYTES], ws[NVOL_BYTESET_CLASS_BYTES];
static uint8_t pair[NVOL_BYTESET_PAIR_BYTES];
static nvol_memmem_finder fin, fin2;
static uint8_t blob[4096];
static nvol_teddy ted;

static size_t run(const char *op, size_t size, size_t n) {
    volatile size_t sink = 0;
    const uint8_t *h = buf, *e = buf + size;
    size_t i;
    if (!strcmp(op, "memchr"))               for (i = 0; i < n; i++) sink += nvol_memchr('z', h, size);
    else if (!strcmp(op, "memrchr"))         for (i = 0; i < n; i++) sink += nvol_memrchr('z', h, size);
    else if (!strcmp(op, "memchr2"))         for (i = 0; i < n; i++) sink += nvol_memchr2('y', 'z', h, size);
    else if (!strcmp(op, "memchr3"))         for (i = 0; i < n; i++) sink += nvol_memchr3('x', 'y', 'z', h, size);
    else if (!strcmp(op, "count"))           for (i = 0; i < n; i++) sink += nvol_count('a', h, size);
    else if (!strcmp(op, "count_parity"))    for (i = 0; i < n; i++) sink += nvol_count_parity('a', h, size);
    else if (!strcmp(op, "first_non_ascii")) for (i = 0; i < n; i++) sink += nvol_first_non_ascii(h, size);
    else if (!strcmp(op, "memmem"))          for (i = 0; i < n; i++) sink += nvol_memmem_finder_find(&fin, h, size);
    else if (!strcmp(op, "memmem2"))         for (i = 0; i < n; i++) sink += nvol_memmem_finder_find(&fin2, h, size);
    else if (!strcmp(op, "byteset"))         for (i = 0; i < n; i++) sink += (size_t)nvol_find_byteset_raw(cls, h, e);
    else if (!strcmp(op, "byteset_not"))     for (i = 0; i < n; i++) sink += (size_t)nvol_find_not_byteset_raw(ws, spaces, spaces + size);
    else if (!strcmp(op, "pair"))            for (i = 0; i < n; i++) sink += (size_t)nvol_find_byteset_pair_raw(pair, h, e);
    else if (!strcmp(op, "teddy")) {
        nvol_teddy_match m;
        for (i = 0; i < n; i++) sink += (size_t)nvol_teddy_find_in(&ted, h, 0, size, &m);
    } else {
        fprintf(stderr, "unknown op %s\n", op);
        exit(2);
    }
    return sink;
}

int main(int argc, char **argv) {
    if (argc != 5 && !(argc == 6 && !strcmp(argv[5], "time"))) {
        fprintf(stderr, "usage: bench OP SIZE N PATH [time]\n");
        return 2;
    }
    const char *op = argv[1];
    size_t size = strtoul(argv[2], 0, 10), n = strtoul(argv[3], 0, 10);
    if (size > (1 << 20)) return 2;
    nvol_test_set_avx2(atoi(argv[4]));
    for (size_t i = 0; i < sizeof buf; i++) buf[i] = (uint8_t)('a' + i % 8);   /* a..h */
    memset(spaces, ' ', sizeof spaces);
    nvol_byteset_init((const uint8_t *)"xyz0", 4, cls);
    nvol_byteset_init((const uint8_t *)" \t\n\r", 4, ws);
    nvol_byteset_pair_init((const uint8_t *)"xy", 2, (const uint8_t *)"q", 1, 0, 1, pair);
    nvol_memmem_finder_init(&fin, (const uint8_t *)"needle12", 8);
    nvol_memmem_finder_init(&fin2, (const uint8_t *)"zq", 2);           /* ugrep's -c zq */
    static const char *pats[] = {"foo", "bar", "quux", "zebra", "xylo", "jump", "wizard", "yoyo"};
    const uint8_t *pp[8];
    size_t pl[8];
    for (int i = 0; i < 8; i++) { pp[i] = (const uint8_t *)pats[i]; pl[i] = strlen(pats[i]); }
    nvol_teddy_blob_write(blob, pp, pl, 8);
    if (!nvol_teddy_build(&ted, blob)) return 3;
    if (argc == 6) {
        double best = 1e300;
        run(op, size, n);                                  /* warm up */
        for (int r = 0; r < 9; r++) {
            double t0 = now_ns();
            run(op, size, n);
            double t = (now_ns() - t0) / (double)n;
            if (t < best) best = t;
        }
        printf("%.2f\n", best);
        return 0;
    }
    return run(op, size, n) == 12345 ? 1 : 0;
}
