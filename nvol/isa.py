"""Target ISA descriptions and per-language lowering of primitives.

A lowering is a format string; `{0}`, `{1}` are the already-printed operands.
Operands that appear more than once in a template MUST be simple variables;
kernels guarantee this by let-binding (see `kernels/count.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ISA:
    name: str
    bytes: int
    arch: str                 # "x86_64", "aarch64", ...
    vec_type: dict            # lang -> vector type name
    target: dict              # lang -> function attribute ("" = baseline)
    lower: dict = field(default_factory=dict)  # (lang, prim) -> template
    # movemask result: x86 = u32 with 1 bit per lane; NEON = u64 with 4 bits
    # per lane (vshrn trick, as in memchr). Kernels scale lane counts by this.
    mask_bits: int = 32
    lane_bits: int = 1

    @property
    def align_mask(self) -> int:
        return self.bytes - 1


# Scalar (ISA independent) primitives, shared by every target.
SCALAR = {
    ("c", "first_offset"): "((size_t)nvol_ctz32({0}))",
    ("c", "last_offset"): "((size_t)(31u - nvol_clz32({0})))",
    ("c", "popcnt"): "((size_t)nvol_popcnt32({0}))",
    ("c", "cpu_avx2"): "nvol_cpu_avx2()",
    ("c", "load16u"): "nvol_load16u({0})",
    ("c", "load32u"): "nvol_load32u({0})",
    ("c", "load64"): "nvol_load64({0})",
    ("rust", "load16u"): "(({0} as *const u16).read_unaligned() as u32)",
    ("rust", "load32u"): "({0} as *const u32).read_unaligned()",
    ("rust", "load64"): "({0} as *const u64).read_unaligned()",
    ("rust", "first_offset"): "({0}.trailing_zeros() as usize)",
    ("rust", "last_offset"): "((31 - {0}.leading_zeros()) as usize)",
    ("rust", "popcnt"): "({0}.count_ones() as usize)",
    ("rust", "cpu_avx2"): "std::is_x86_feature_detected!(\"avx2\")",
    ("c", "cpu_ssse3"): "nvol_cpu_ssse3()",
    ("c", "cpu_avx2_pclmul_bmi1"): "nvol_cpu_avx2_pclmul_bmi1()",
    ("rust", "cpu_avx2_pclmul_bmi1"): "(std::is_x86_feature_detected!(\"avx2\") && std::is_x86_feature_detected!(\"pclmulqdq\") && std::is_x86_feature_detected!(\"bmi1\"))",
    ("rust", "cpu_ssse3"): "std::is_x86_feature_detected!(\"ssse3\")",
    ("c", "cpu_sse42"): "nvol_cpu_sse42()",
    ("rust", "cpu_sse42"): "std::is_x86_feature_detected!(\"sse4.2\")",
    ("c", "cpu_sse42_pclmul"): "nvol_cpu_sse42_pclmul()",
    ("rust", "cpu_sse42_pclmul"): "(std::is_x86_feature_detected!(\"sse4.2\") && std::is_x86_feature_detected!(\"pclmulqdq\"))",
    ("c", "first_offset64"): "((size_t)nvol_ctz64({0}))",
    ("rust", "first_offset64"): "({0}.trailing_zeros() as usize)",
    ("c", "tzcnt64"): "((size_t)nvol_tzcnt64({0}))",
    ("rust", "tzcnt64"): "({0}.trailing_zeros() as usize)",
    ("c", "ctz64_lax"): "((size_t)nvol_ctz64_lax({0}))",
    ("rust", "ctz64_lax"): "({0}.trailing_zeros() as usize)",
    ("c", "last_offset64"): "((size_t)(63u - nvol_clz64({0})))",
    ("rust", "last_offset64"): "((63 - {0}.leading_zeros()) as usize)",
    ("c", "popcnt64"): "((size_t)nvol_popcnt64({0}))",
    ("rust", "popcnt64"): "({0}.count_ones() as usize)",
}

SSE2 = ISA(
    name="sse2", bytes=16, arch="x86_64",
    vec_type={"c": "__m128i", "rust": "__m128i"},
    target={"c": "", "rust": "sse2"},
    lower={
        ("c", "splat"): "_mm_set1_epi8((char){0})",
        ("c", "vtable16"): "_mm_setr_epi8((char){0}, (char){1}, (char){2}, (char){3}, (char){4}, (char){5}, (char){6}, (char){7}, (char){8}, (char){9}, (char){10}, (char){11}, (char){12}, (char){13}, (char){14}, (char){15})",
        ("c", "vzero"): "_mm_setzero_si128()",
        ("c", "load_a"): "_mm_load_si128((const __m128i *)({0}))",
        ("c", "load_u"): "_mm_loadu_si128((const __m128i *)({0}))",
        ("c", "cmpeq"): "_mm_cmpeq_epi8({0}, {1})",
        ("c", "vor"): "_mm_or_si128({0}, {1})",
        ("c", "mask_or"): "_mm_or_si128({0}, {1})",
        ("c", "vand"): "_mm_and_si128({0}, {1})",
        ("c", "vxor"): "_mm_xor_si128({0}, {1})",
        ("c", "vandnot"): "_mm_andnot_si128({1}, {0})",
        ("c", "vsub8"): "_mm_sub_epi8({0}, {1})",
        ("c", "vsubs8"): "_mm_subs_epu8({0}, {1})",
        ("c", "vmin8"): "_mm_min_epu8({0}, {1})",
        ("c", "movemask"): "((uint32_t)_mm_movemask_epi8({0}))",
        # the casts of simdjson's to_bitmask (r0 through uint32_t, r1-r3 from int): with a
        # (uint32_t) on each, GCC orders the pmovmskb/or chain differently (+0.4 % on
        # copy_and_find, westmere target)
        ("c", "bmask64x4"): "(((uint64_t)(uint32_t)_mm_movemask_epi8({0})) | ((uint64_t)_mm_movemask_epi8({1}) << 16) | ((uint64_t)_mm_movemask_epi8({2}) << 32) | ((uint64_t)_mm_movemask_epi8({3}) << 48))",
        ("c", "sad0"): "_mm_sad_epu8({0}, _mm_setzero_si128())",
        ("c", "vadd64"): "_mm_add_epi64({0}, {1})",
        ("c", "hsum64"): "((size_t)(_mm_cvtsi128_si64({0}) + _mm_cvtsi128_si64(_mm_unpackhi_epi64({0}, {0}))))",
        ("rust", "splat"): "_mm_set1_epi8({0} as i8)",
        ("rust", "vtable16"): "_mm_setr_epi8({0} as i8, {1} as i8, {2} as i8, {3} as i8, {4} as i8, {5} as i8, {6} as i8, {7} as i8, {8} as i8, {9} as i8, {10} as i8, {11} as i8, {12} as i8, {13} as i8, {14} as i8, {15} as i8)",
        ("rust", "vzero"): "_mm_setzero_si128()",
        ("rust", "load_a"): "_mm_load_si128({0} as *const __m128i)",
        ("rust", "load_u"): "_mm_loadu_si128({0} as *const __m128i)",
        ("rust", "cmpeq"): "_mm_cmpeq_epi8({0}, {1})",
        ("rust", "vor"): "_mm_or_si128({0}, {1})",
        ("rust", "mask_or"): "_mm_or_si128({0}, {1})",
        ("rust", "vand"): "_mm_and_si128({0}, {1})",
        ("rust", "vxor"): "_mm_xor_si128({0}, {1})",
        ("rust", "vandnot"): "_mm_andnot_si128({1}, {0})",
        ("rust", "vsub8"): "_mm_sub_epi8({0}, {1})",
        ("rust", "vsubs8"): "_mm_subs_epu8({0}, {1})",
        ("rust", "vmin8"): "_mm_min_epu8({0}, {1})",
        ("rust", "movemask"): "(_mm_movemask_epi8({0}) as u32)",
        ("rust", "bmask64x4"): "(((_mm_movemask_epi8({0}) as u32 as u64)) | ((_mm_movemask_epi8({1}) as u32 as u64) << 16) | ((_mm_movemask_epi8({2}) as u32 as u64) << 32) | ((_mm_movemask_epi8({3}) as u32 as u64) << 48))",
        ("rust", "sad0"): "_mm_sad_epu8({0}, _mm_setzero_si128())",
        ("rust", "vadd64"): "_mm_add_epi64({0}, {1})",
        ("rust", "hsum64"): "((_mm_cvtsi128_si64({0}) as u64).wrapping_add(_mm_cvtsi128_si64(_mm_unpackhi_epi64({0}, {0})) as u64) as usize)",
        ("c", "load8v"): "_mm_loadl_epi64((const __m128i *)({0}))",
        ("c", "vmadd16"): "_mm_madd_epi16({0}, {1})",
        ("c", "lane32_0"): "((uint32_t)_mm_cvtsi128_si32({0}))",
        ("rust", "load8v"): "_mm_loadl_epi64({0} as *const __m128i)",
        ("rust", "vmadd16"): "_mm_madd_epi16({0}, {1})",
        ("rust", "lane32_0"): "(_mm_cvtsi128_si32({0}) as u32)",
    },
)

SSE128_TEDDY = {
    ("c", "vshr4"): "_mm_srli_epi16({0}, 4)",
    ("c", "vshuffle"): "_mm_shuffle_epi8({0}, {1})",
    ("c", "shin1"): "_mm_alignr_epi8({0}, {1}, 15)",
    ("c", "shin2"): "_mm_alignr_epi8({0}, {1}, 14)",
    ("c", "shin3"): "_mm_alignr_epi8({0}, {1}, 13)",
    ("c", "is_zero"): "(_mm_movemask_epi8(_mm_cmpeq_epi8({0}, _mm_setzero_si128())) == 0xFFFF)",
    ("c", "lane64_0"): "((uint64_t)_mm_cvtsi128_si64({0}))",
    ("c", "lane64_1"): "((uint64_t)_mm_cvtsi128_si64(_mm_unpackhi_epi64({0}, {0})))",
    ("rust", "vshr4"): "_mm_srli_epi16::<4>({0})",
    ("rust", "vshuffle"): "_mm_shuffle_epi8({0}, {1})",
    ("rust", "shin1"): "_mm_alignr_epi8::<15>({0}, {1})",
    ("rust", "shin2"): "_mm_alignr_epi8::<14>({0}, {1})",
    ("rust", "shin3"): "_mm_alignr_epi8::<13>({0}, {1})",
    ("rust", "is_zero"): "(_mm_movemask_epi8(_mm_cmpeq_epi8({0}, _mm_setzero_si128())) == 0xFFFF)",
    ("rust", "lane64_0"): "(_mm_cvtsi128_si64({0}) as u64)",
    ("rust", "lane64_1"): "(_mm_cvtsi128_si64(_mm_unpackhi_epi64({0}, {0})) as u64)",
}

SSSE3 = ISA(
    name="ssse3", bytes=16, arch="x86_64",
    vec_type={"c": "__m128i", "rust": "__m128i"},
    target={"c": "ssse3", "rust": "ssse3"},
    lower={**SSE2.lower, **SSE128_TEDDY,
           ("c", "vmaddubs"): "_mm_maddubs_epi16({0}, {1})",
           ("rust", "vmaddubs"): "_mm_maddubs_epi16({0}, {1})"},
)

# SSE4.2 implies SSSE3: its functions may use (and inline) the SSSE3 kernels
SSE42 = ISA(
    name="sse42", bytes=16, arch="x86_64",
    vec_type={"c": "__m128i", "rust": "__m128i"},
    target={"c": "sse4.2", "rust": "sse4.2,popcnt"},
    lower={**SSSE3.lower,
           # pcmpistri: _SIDD_UBYTE_OPS | _SIDD_CMP_EQUAL_ANY | _SIDD_LEAST_SIGNIFICANT | _SIDD_NEGATIVE_POLARITY
           ("c", "skip_set16"): "((uint32_t)_mm_cmpistri({0}, {1}, 0x10))",
           ("rust", "skip_set16"): "(_mm_cmpistri::<0x10>({0}, {1}) as u32)",
           ("c", "vpackus32"): "_mm_packus_epi32({0}, {1})",
           ("rust", "vpackus32"): "_mm_packus_epi32({0}, {1})"},
)

AVX2 = ISA(
    name="avx2", bytes=32, arch="x86_64",
    vec_type={"c": "__m256i", "rust": "__m256i"},
    target={"c": "avx2", "rust": "avx2,popcnt,pclmulqdq"},
    lower={
        ("c", "splat"): "_mm256_set1_epi8((char){0})",
        ("c", "vtable16"): "_mm256_setr_epi8((char){0}, (char){1}, (char){2}, (char){3}, (char){4}, (char){5}, (char){6}, (char){7}, (char){8}, (char){9}, (char){10}, (char){11}, (char){12}, (char){13}, (char){14}, (char){15}, (char){0}, (char){1}, (char){2}, (char){3}, (char){4}, (char){5}, (char){6}, (char){7}, (char){8}, (char){9}, (char){10}, (char){11}, (char){12}, (char){13}, (char){14}, (char){15})",
        ("c", "vzero"): "_mm256_setzero_si256()",
        ("c", "load_a"): "_mm256_load_si256((const __m256i *)({0}))",
        ("c", "load_u"): "_mm256_loadu_si256((const __m256i *)({0}))",
        ("c", "cmpeq"): "_mm256_cmpeq_epi8({0}, {1})",
        ("c", "vor"): "_mm256_or_si256({0}, {1})",
        ("c", "mask_or"): "_mm256_or_si256({0}, {1})",
        ("c", "vand"): "_mm256_and_si256({0}, {1})",
        ("c", "vxor"): "_mm256_xor_si256({0}, {1})",
        ("c", "vandnot"): "_mm256_andnot_si256({1}, {0})",
        ("c", "vsub8"): "_mm256_sub_epi8({0}, {1})",
        ("c", "vsubs8"): "_mm256_subs_epu8({0}, {1})",
        ("c", "vmin8"): "_mm256_min_epu8({0}, {1})",
        ("c", "movemask"): "((uint32_t)_mm256_movemask_epi8({0}))",
        ("c", "bmask64x2"): "((uint64_t)(uint32_t)_mm256_movemask_epi8({0}) | ((uint64_t)(uint32_t)_mm256_movemask_epi8({1}) << 32))",
        ("c", "prefix_xor64"): "((uint64_t)_mm_cvtsi128_si64(_mm_clmulepi64_si128(_mm_cvtsi64_si128((long long)({0})), _mm_set1_epi8((char)-1), 0)))",
        ("c", "sad0"): "_mm256_sad_epu8({0}, _mm256_setzero_si256())",
        ("c", "vadd64"): "_mm256_add_epi64({0}, {1})",
        ("c", "hsum64"): "((size_t)(_mm256_extract_epi64({0}, 0) + _mm256_extract_epi64({0}, 1) + _mm256_extract_epi64({0}, 2) + _mm256_extract_epi64({0}, 3)))",
        ("rust", "splat"): "_mm256_set1_epi8({0} as i8)",
        ("rust", "vtable16"): "_mm256_setr_epi8({0} as i8, {1} as i8, {2} as i8, {3} as i8, {4} as i8, {5} as i8, {6} as i8, {7} as i8, {8} as i8, {9} as i8, {10} as i8, {11} as i8, {12} as i8, {13} as i8, {14} as i8, {15} as i8, {0} as i8, {1} as i8, {2} as i8, {3} as i8, {4} as i8, {5} as i8, {6} as i8, {7} as i8, {8} as i8, {9} as i8, {10} as i8, {11} as i8, {12} as i8, {13} as i8, {14} as i8, {15} as i8)",
        ("rust", "vzero"): "_mm256_setzero_si256()",
        ("rust", "load_a"): "_mm256_load_si256({0} as *const __m256i)",
        ("rust", "load_u"): "_mm256_loadu_si256({0} as *const __m256i)",
        ("rust", "cmpeq"): "_mm256_cmpeq_epi8({0}, {1})",
        ("rust", "vor"): "_mm256_or_si256({0}, {1})",
        ("rust", "mask_or"): "_mm256_or_si256({0}, {1})",
        ("rust", "vand"): "_mm256_and_si256({0}, {1})",
        ("rust", "vxor"): "_mm256_xor_si256({0}, {1})",
        ("rust", "vandnot"): "_mm256_andnot_si256({1}, {0})",
        ("rust", "vsub8"): "_mm256_sub_epi8({0}, {1})",
        ("rust", "vsubs8"): "_mm256_subs_epu8({0}, {1})",
        ("rust", "vmin8"): "_mm256_min_epu8({0}, {1})",
        ("rust", "movemask"): "(_mm256_movemask_epi8({0}) as u32)",
        ("rust", "bmask64x2"): "((_mm256_movemask_epi8({0}) as u32 as u64) | ((_mm256_movemask_epi8({1}) as u32 as u64) << 32))",
        ("rust", "prefix_xor64"): "(_mm_cvtsi128_si64(_mm_clmulepi64_si128::<0>(_mm_cvtsi64_si128({0} as i64), _mm_set1_epi8(-1))) as u64)",
        ("rust", "sad0"): "_mm256_sad_epu8({0}, _mm256_setzero_si256())",
        ("rust", "vadd64"): "_mm256_add_epi64({0}, {1})",
        # Teddy: shin_k crosses the 128-bit lanes via permute2x128 (b high lane : a low lane)
        ("c", "vshr4"): "_mm256_srli_epi16({0}, 4)",
        ("c", "vshuffle"): "_mm256_shuffle_epi8({0}, {1})",
        ("c", "shin1"): "_mm256_alignr_epi8({0}, _mm256_permute2x128_si256({1}, {0}, 0x21), 15)",
        ("c", "shin2"): "_mm256_alignr_epi8({0}, _mm256_permute2x128_si256({1}, {0}, 0x21), 14)",
        ("c", "shin3"): "_mm256_alignr_epi8({0}, _mm256_permute2x128_si256({1}, {0}, 0x21), 13)",
        ("c", "hshin1"): "_mm256_alignr_epi8({0}, {1}, 15)",
        ("c", "hshin2"): "_mm256_alignr_epi8({0}, {1}, 14)",
        ("c", "hshin3"): "_mm256_alignr_epi8({0}, {1}, 13)",
        ("c", "is_zero"): "(_mm256_testz_si256({0}, {0}) != 0)",
        ("c", "lane64_0"): "((uint64_t)_mm256_extract_epi64({0}, 0))",
        ("c", "lane64_1"): "((uint64_t)_mm256_extract_epi64({0}, 1))",
        ("c", "lane64_2"): "((uint64_t)_mm256_extract_epi64({0}, 2))",
        ("c", "lane64_3"): "((uint64_t)_mm256_extract_epi64({0}, 3))",
        ("c", "load_half_bcast"): "_mm256_broadcastsi128_si256(_mm_loadu_si128((const __m128i *)({0})))",
        ("c", "swap_halves"): "_mm256_permute4x64_epi64({0}, 0x4E)",
        ("c", "unpacklo8"): "_mm256_unpacklo_epi8({0}, {1})",
        ("c", "unpackhi8"): "_mm256_unpackhi_epi8({0}, {1})",
        ("rust", "vshr4"): "_mm256_srli_epi16::<4>({0})",
        ("rust", "vshuffle"): "_mm256_shuffle_epi8({0}, {1})",
        ("rust", "shin1"): "_mm256_alignr_epi8::<15>({0}, _mm256_permute2x128_si256::<0x21>({1}, {0}))",
        ("rust", "shin2"): "_mm256_alignr_epi8::<14>({0}, _mm256_permute2x128_si256::<0x21>({1}, {0}))",
        ("rust", "shin3"): "_mm256_alignr_epi8::<13>({0}, _mm256_permute2x128_si256::<0x21>({1}, {0}))",
        ("rust", "hshin1"): "_mm256_alignr_epi8::<15>({0}, {1})",
        ("rust", "hshin2"): "_mm256_alignr_epi8::<14>({0}, {1})",
        ("rust", "hshin3"): "_mm256_alignr_epi8::<13>({0}, {1})",
        ("rust", "is_zero"): "(_mm256_testz_si256({0}, {0}) != 0)",
        ("rust", "lane64_0"): "(_mm256_extract_epi64::<0>({0}) as u64)",
        ("rust", "lane64_1"): "(_mm256_extract_epi64::<1>({0}) as u64)",
        ("rust", "lane64_2"): "(_mm256_extract_epi64::<2>({0}) as u64)",
        ("rust", "lane64_3"): "(_mm256_extract_epi64::<3>({0}) as u64)",
        ("rust", "load_half_bcast"): "_mm256_broadcastsi128_si256(_mm_loadu_si128({0} as *const __m128i))",
        ("rust", "swap_halves"): "_mm256_permute4x64_epi64::<0x4E>({0})",
        ("rust", "unpacklo8"): "_mm256_unpacklo_epi8({0}, {1})",
        ("rust", "unpackhi8"): "_mm256_unpackhi_epi8({0}, {1})",
        ("rust", "hsum64"): "((_mm256_extract_epi64::<0>({0}) as u64).wrapping_add(_mm256_extract_epi64::<1>({0}) as u64).wrapping_add(_mm256_extract_epi64::<2>({0}) as u64).wrapping_add(_mm256_extract_epi64::<3>({0}) as u64) as usize)",
    },
)

_N_C = {
    "splat": "vdupq_n_u8((uint8_t)({0}))",
    "vtable16": "vcombine_u8(vcreate_u8((uint64_t)({0}) | (uint64_t)({1}) << 8 | (uint64_t)({2}) << 16 | (uint64_t)({3}) << 24 | (uint64_t)({4}) << 32 | (uint64_t)({5}) << 40 | (uint64_t)({6}) << 48 | (uint64_t)({7}) << 56), vcreate_u8((uint64_t)({8}) | (uint64_t)({9}) << 8 | (uint64_t)({10}) << 16 | (uint64_t)({11}) << 24 | (uint64_t)({12}) << 32 | (uint64_t)({13}) << 40 | (uint64_t)({14}) << 48 | (uint64_t)({15}) << 56))",
    "vzero": "vdupq_n_u8(0)",
    "load_a": "vld1q_u8({0})",
    "load_u": "vld1q_u8({0})",
    "cmpeq": "vceqq_u8({0}, {1})",
    "vor": "vorrq_u8({0}, {1})",
    "mask_or": "vorrq_u8({0}, {1})",
    "vand": "vandq_u8({0}, {1})",
    "vxor": "veorq_u8({0}, {1})",
    "vandnot": "vbicq_u8({0}, {1})",
    "vsub8": "vsubq_u8({0}, {1})",
    "vsubs8": "vqsubq_u8({0}, {1})",
    "vmin8": "vminq_u8({0}, {1})",
    "movemask": "(vget_lane_u64(vreinterpret_u64_u8(vshrn_n_u16(vreinterpretq_u16_u8({0}), 4)), 0) & UINT64_C(0x8888888888888888))",
    "bmask64x4": "vgetq_lane_u64(vreinterpretq_u64_u8(vpaddq_u8(vpaddq_u8(vpaddq_u8(vandq_u8({0}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))), vandq_u8({1}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201))))), vpaddq_u8(vandq_u8({2}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))), vandq_u8({3}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))))), vpaddq_u8(vpaddq_u8(vandq_u8({0}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))), vandq_u8({1}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201))))), vpaddq_u8(vandq_u8({2}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))), vandq_u8({3}, vreinterpretq_u8_u64(vdupq_n_u64(UINT64_C(0x8040201008040201)))))))), 0)",
    "any_nonzero": "(vgetq_lane_u64(vreinterpretq_u64_u8(vpmaxq_u8({0}, {0})), 0) != 0)",
    "first_offset": "((size_t)(nvol_ctz64({0}) >> 2))",
    "last_offset": "((size_t)((63u - nvol_clz64({0})) >> 2))",
    "popcnt": "((size_t)nvol_popcnt64({0}))",
    "sad0": "vreinterpretq_u8_u64(vpaddlq_u32(vpaddlq_u16(vpaddlq_u8({0}))))",
    "vadd64": "vreinterpretq_u8_u64(vaddq_u64(vreinterpretq_u64_u8({0}), vreinterpretq_u64_u8({1})))",
    "hsum64": "((size_t)vaddvq_u64(vreinterpretq_u64_u8({0})))",
    "vshuffle": "vqtbl1q_u8({0}, {1})",
    "shin1": "vextq_u8({1}, {0}, 15)",
    "shin2": "vextq_u8({1}, {0}, 14)",
    "shin3": "vextq_u8({1}, {0}, 13)",
    "is_zero": "(vgetq_lane_u64(vreinterpretq_u64_u8(vpmaxq_u8({0}, {0})), 0) == 0)",
    "lane64_0": "vgetq_lane_u64(vreinterpretq_u64_u8({0}), 0)",
    "lane64_1": "vgetq_lane_u64(vreinterpretq_u64_u8({0}), 1)",
    "vnib_hi": "vshrq_n_u8({0}, 4)",
    "msb_lanes": "vreinterpretq_u8_s8(vshrq_n_s8(vreinterpretq_s8_u8({0}), 7))",
}
_N_RUST = {
    "splat": "vdupq_n_u8({0})",
    "vtable16": "vcombine_u8(vcreate_u8((({0} as u64)) | (({1} as u64) << 8) | (({2} as u64) << 16) | (({3} as u64) << 24) | (({4} as u64) << 32) | (({5} as u64) << 40) | (({6} as u64) << 48) | (({7} as u64) << 56)), vcreate_u8((({8} as u64)) | (({9} as u64) << 8) | (({10} as u64) << 16) | (({11} as u64) << 24) | (({12} as u64) << 32) | (({13} as u64) << 40) | (({14} as u64) << 48) | (({15} as u64) << 56)))",
    "vzero": "vdupq_n_u8(0)",
    "load_a": "vld1q_u8({0})",
    "load_u": "vld1q_u8({0})",
    "cmpeq": "vceqq_u8({0}, {1})",
    "vor": "vorrq_u8({0}, {1})",
    "mask_or": "vorrq_u8({0}, {1})",
    "vand": "vandq_u8({0}, {1})",
    "vxor": "veorq_u8({0}, {1})",
    "vandnot": "vbicq_u8({0}, {1})",
    "vsub8": "vsubq_u8({0}, {1})",
    "vsubs8": "vqsubq_u8({0}, {1})",
    "vmin8": "vminq_u8({0}, {1})",
    "movemask": "(vget_lane_u64::<0>(vreinterpret_u64_u8(vshrn_n_u16::<4>(vreinterpretq_u16_u8({0})))) & 0x8888888888888888u64)",
    "bmask64x4": "vgetq_lane_u64::<0>(vreinterpretq_u64_u8(vpaddq_u8(vpaddq_u8(vpaddq_u8(vandq_u8({0}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))), vandq_u8({1}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201)))), vpaddq_u8(vandq_u8({2}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))), vandq_u8({3}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))))), vpaddq_u8(vpaddq_u8(vandq_u8({0}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))), vandq_u8({1}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201)))), vpaddq_u8(vandq_u8({2}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))), vandq_u8({3}, vreinterpretq_u8_u64(vdupq_n_u64(0x8040201008040201))))))))",
    "any_nonzero": "(vgetq_lane_u64::<0>(vreinterpretq_u64_u8(vpmaxq_u8({0}, {0}))) != 0)",
    "first_offset": "(({0}.trailing_zeros() >> 2) as usize)",
    "last_offset": "(((63 - {0}.leading_zeros()) >> 2) as usize)",
    "popcnt": "({0}.count_ones() as usize)",
    "sad0": "vreinterpretq_u8_u64(vpaddlq_u32(vpaddlq_u16(vpaddlq_u8({0}))))",
    "vadd64": "vreinterpretq_u8_u64(vaddq_u64(vreinterpretq_u64_u8({0}), vreinterpretq_u64_u8({1})))",
    "hsum64": "(vaddvq_u64(vreinterpretq_u64_u8({0})) as usize)",
    "vshuffle": "vqtbl1q_u8({0}, {1})",
    "shin1": "vextq_u8::<15>({1}, {0})",
    "shin2": "vextq_u8::<14>({1}, {0})",
    "shin3": "vextq_u8::<13>({1}, {0})",
    "is_zero": "(vgetq_lane_u64::<0>(vreinterpretq_u64_u8(vpmaxq_u8({0}, {0}))) == 0)",
    "lane64_0": "vgetq_lane_u64::<0>(vreinterpretq_u64_u8({0}))",
    "lane64_1": "vgetq_lane_u64::<1>(vreinterpretq_u64_u8({0}))",
    "vnib_hi": "vshrq_n_u8::<4>({0})",
    "msb_lanes": "vreinterpretq_u8_s8(vshrq_n_s8::<7>(vreinterpretq_s8_u8({0})))",
}

NEON = ISA(
    name="neon", bytes=16, arch="aarch64",
    vec_type={"c": "uint8x16_t", "rust": "uint8x16_t"},
    target={"c": "", "rust": ""},           # baseline on aarch64
    lower={**{("c", k): v for k, v in _N_C.items()}, **{("rust", k): v for k, v in _N_RUST.items()}},
    mask_bits=64, lane_bits=4,
)

# x86 lowerings of the portable helpers added for NEON
for _isa, _p, _w in ((SSE2, "_mm", "si128"), (SSSE3, "_mm", "si128"), (SSE42, "_mm", "si128"),
                     (AVX2, "_mm256", "si256")):
    _isa.lower[("c", "any_nonzero")] = f"({_p}_movemask_epi8({{0}}) != 0)"
    _isa.lower[("rust", "any_nonzero")] = f"({_p}_movemask_epi8({{0}}) != 0)"
    _isa.lower[("c", "vnib_hi")] = f"{_p}_and_{_w}({_p}_srli_epi16({{0}}, 4), {_p}_set1_epi8(0x0F))"
    _isa.lower[("rust", "vnib_hi")] = f"{_p}_and_{_w}({_p}_srli_epi16::<4>({{0}}), {_p}_set1_epi8(0x0F))"

# vtail3: 0xFF in every lane but the last three, {0} {1} {2}
for _isa, _p, _n in ((SSE2, "_mm", 16), (SSSE3, "_mm", 16), (SSE42, "_mm", 16), (AVX2, "_mm256", 32)):
    _isa.lower[("c", "vtail3")] = (f"{_p}_setr_epi8(" + "-1, " * (_n - 3)
                                   + "(char){0}, (char){1}, (char){2})")
    _isa.lower[("rust", "vtail3")] = (f"{_p}_setr_epi8(" + "-1, " * (_n - 3)
                                      + "{0} as i8, {1} as i8, {2} as i8)")
NEON.lower[("c", "vtail3")] = ("vcombine_u8(vcreate_u8(UINT64_C(0xFFFFFFFFFFFFFFFF)), "
                               "vcreate_u8(UINT64_C(0xFFFFFFFFFF) | (uint64_t)({0}) << 40 | "
                               "(uint64_t)({1}) << 48 | (uint64_t)({2}) << 56))")
NEON.lower[("rust", "vtail3")] = ("vcombine_u8(vcreate_u8(u64::MAX), vcreate_u8(0xFF_FFFF_FFFFu64 | "
                                  "(({0} as u64) << 40) | (({1} as u64) << 48) | (({2} as u64) << 56)))")

# vector store (ir.StoreVec): {0} = mutable pointer, {1} = vector
for _isa, _c, _r in ((SSE2, "_mm_storeu_si128((__m128i *)({0}), {1})", "_mm_storeu_si128({0} as *mut __m128i, {1})"),
                     (SSSE3, "_mm_storeu_si128((__m128i *)({0}), {1})", "_mm_storeu_si128({0} as *mut __m128i, {1})"),
                     (SSE42, "_mm_storeu_si128((__m128i *)({0}), {1})", "_mm_storeu_si128({0} as *mut __m128i, {1})"),
                     (AVX2, "_mm256_storeu_si256((__m256i *)({0}), {1})", "_mm256_storeu_si256({0} as *mut __m256i, {1})"),
                     (NEON, "vst1q_u8({0}, {1})", "vst1q_u8({0}, {1})")):
    _isa.lower[("c", "store_u")] = _c
    _isa.lower[("rust", "store_u")] = _r

ISAS = {i.name: i for i in (SSE2, SSSE3, SSE42, AVX2, NEON)}
ARCH_ISAS = {"x86_64": ("sse2", "ssse3", "sse42", "avx2"), "aarch64": ("neon",)}


# lowerings for a function with extra target features (ir.Function.features), before its ISA's.
# tzcnt64 under BMI1 is the intrinsic, not `x ? ctz(x) : 64`: GCC knows that once blsr has
# cleared the last bit every later tzcnt is 64, and turns an unrolled indexer back into a branch
# per slot (simdjson's haswell kernel calls _tzcnt_u64 too)
FEATURE_LOWER = {
    ("c", "bmi1", "tzcnt64"): "((size_t)_tzcnt_u64({0}))",
    ("rust", "bmi1", "tzcnt64"): "(_tzcnt_u64({0}) as usize)",
    ("c", "bmi1", "ctz64_lax"): "((size_t)_tzcnt_u64({0}))",
    ("rust", "bmi1", "ctz64_lax"): "(_tzcnt_u64({0}) as usize)",
    # an SSE function with PCLMULQDQ (simdjson's westmere): the same 128-bit sequence as AVX2's
    ("c", "pclmul", "prefix_xor64"): "((uint64_t)_mm_cvtsi128_si64(_mm_clmulepi64_si128(_mm_cvtsi64_si128((long long)({0})), _mm_set1_epi8((char)-1), 0)))",
    ("rust", "pclmul", "prefix_xor64"): "(_mm_cvtsi128_si64(_mm_clmulepi64_si128::<0>(_mm_cvtsi64_si128({0} as i64), _mm_set1_epi8(-1))) as u64)",
}


def lowering(isa: "ISA | None", lang: str, prim: str, features: tuple = ()) -> str:
    """The function's features first, then the ISA's lowering (NEON overrides first_offset
    etc.), then scalar."""
    for f in features:
        if (lang, f, prim) in FEATURE_LOWER:
            return FEATURE_LOWER[(lang, f, prim)]
    key = (lang, prim)
    if isa is not None and key in isa.lower:
        return isa.lower[key]
    if key in SCALAR:
        return SCALAR[key]
    raise KeyError(f"no lowering for {prim!r} in {lang} on {isa.name if isa else 'scalar'}")


def mask_ty(isa: ISA):
    from .ir import U32, U64
    return U64 if isa.mask_bits == 64 else U32


def movemask(isa: ISA, v):
    """movemask with the ISA's mask type (u32 on x86, u64 on NEON)."""
    from .ir import Prim
    return Prim("movemask", [v], mask_ty(isa))


def all_lanes_mask(isa: ISA):
    from .ir import lift
    return lift((1 << isa.mask_bits) - 1, mask_ty(isa))


def prefix_xor(b, isa: "ISA | None", x):
    """simdjson's prefix_xor of a u64 (bit i = XOR of bits 0..i). One pclmulqdq where the ISA
    has it (AVX2 functions, which dispatch on cpu_avx2_pclmul_bmi1) or the function has the
    pclmul feature (SSE4.2, cpu_sse42_pclmul); elsewhere the six shift/xor steps of simdjson's
    arm64 and fallback kernels, let-bound so no step is computed twice."""
    from .ir import U64, Prim
    if isa is not None and (("c", "prefix_xor64") in isa.lower or "pclmul" in getattr(b, "features", ())):
        return Prim("prefix_xor64", [x], U64)
    v = b.var("px", x, U64)
    for k in (1, 2, 4, 8, 16, 32):
        b.set(v, v ^ (v << k))
    return v
