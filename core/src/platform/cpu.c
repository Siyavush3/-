/* Определение возможностей процессора для диспетчеризации SIMD-ядер.
 * AVX/AVX2/AVX-512 считаются доступными, только если их регистры сохраняет ОС (XCR0):
 * наличие инструкции в CPUID без поддержки ОС приведёт к падению на первой же инструкции. */
#include <stdint.h>

#include "arm112/arm112.h"

#if defined(_M_X64) || defined(_M_IX86) || defined(__x86_64__) || defined(__i386__)
#define A112_X86 1
#endif

#if defined(A112_X86)
#if defined(_MSC_VER)
#include <immintrin.h>
#include <intrin.h>

static void cpuid(uint32_t leaf, uint32_t sub, uint32_t r[4])
{
    int t[4];
    __cpuidex(t, (int)leaf, (int)sub);
    for (int i = 0; i < 4; i++)
        r[i] = (uint32_t)t[i];
}

static uint64_t xgetbv0(void)
{
    return (uint64_t)_xgetbv(0);
}
#else
#include <cpuid.h>

static void cpuid(uint32_t leaf, uint32_t sub, uint32_t r[4])
{
    unsigned a = 0, b = 0, c = 0, d = 0;
    if (!__get_cpuid_count(leaf, sub, &a, &b, &c, &d))
        a = b = c = d = 0;
    r[0] = a;
    r[1] = b;
    r[2] = c;
    r[3] = d;
}

static uint64_t xgetbv0(void)
{
    uint32_t lo, hi;
    __asm__ __volatile__("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
    return ((uint64_t)hi << 32) | lo;
}
#endif
#endif /* A112_X86 */

uint32_t a112_cpu_features(void)
{
    uint32_t features = 0;
#if defined(A112_X86)
    uint32_t r[4];
    cpuid(0, 0, r);
    const uint32_t max_leaf = r[0];
    if (max_leaf < 1)
        return 0;

    cpuid(1, 0, r);
    const uint32_t ecx1 = r[2];
    const int sse42 = (ecx1 >> 20) & 1;
    const int fma = (ecx1 >> 12) & 1;
    const int osxsave = (ecx1 >> 27) & 1;
    const int avx = (ecx1 >> 28) & 1;

    const uint64_t xcr0 = osxsave ? xgetbv0() : 0;
    const int ymm_saved = (xcr0 & 0x06) == 0x06; /* XMM + YMM */
    const int zmm_saved = (xcr0 & 0xE6) == 0xE6; /* + opmask, ZMM_Hi256, Hi16_ZMM */

    if (sse42)
        features |= A112_CPU_SSE42;
    if (avx && ymm_saved && fma)
        features |= A112_CPU_FMA;
    if (max_leaf >= 7) {
        cpuid(7, 0, r);
        const uint32_t ebx7 = r[1];
        if (avx && ymm_saved && ((ebx7 >> 5) & 1))
            features |= A112_CPU_AVX2;
        if (zmm_saved && ((ebx7 >> 16) & 1))
            features |= A112_CPU_AVX512F;
    }
#endif
    return features;
}
