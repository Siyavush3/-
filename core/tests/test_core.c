/*
 * Тесты ядра без внешних зависимостей.
 *   test_core                    — только функции, не требующие таблицы
 *   test_core <classifier.ekp>   — плюс маршрутизация по кейсам боевого стенда
 * Полная сверка с эталоном на всех кодах — tests/test_ekp_native.py.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "arm112/arm112.h"

#if defined(_WIN32)
/* На Windows argv и fopen работают в ANSI-кодировке, а путь к таблице может быть
 * кириллическим (C:\Users\Иван\...). Переводим всё в UTF-8 / UTF-16 явно. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <shellapi.h>

static char *to_utf8(const wchar_t *w)
{
    const int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *s = (char *)malloc((size_t)n);
    if (s)
        WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL);
    return s;
}

static wchar_t *to_wide(const char *s)
{
    const int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    wchar_t *w = (wchar_t *)malloc((size_t)n * sizeof(wchar_t));
    if (w)
        MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n);
    return w;
}

static FILE *fopen_u8(const char *path, const char *mode)
{
    wchar_t *wp = to_wide(path), *wm = to_wide(mode);
    FILE *f = (wp && wm) ? _wfopen(wp, wm) : NULL;
    free(wp);
    free(wm);
    return f;
}

static int remove_u8(const char *path)
{
    wchar_t *wp = to_wide(path);
    const int r = wp ? _wremove(wp) : -1;
    free(wp);
    return r;
}

static char **utf8_argv(int *argc)
{
    wchar_t **w = CommandLineToArgvW(GetCommandLineW(), argc);
    char **a = (char **)calloc((size_t)*argc + 1, sizeof(char *));
    for (int i = 0; a && i < *argc; i++)
        a[i] = to_utf8(w[i]);
    LocalFree(w);
    return a;
}
#else
#define fopen_u8 fopen
#define remove_u8 remove
#endif

static int checks, failures;

#define CHECK(cond)                                                                    \
    do {                                                                               \
        checks++;                                                                      \
        if (!(cond)) {                                                                 \
            failures++;                                                                \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);   \
        }                                                                              \
    } while (0)

#define COUNT(a) ((int)(sizeof(a) / sizeof((a)[0])))

static int flag_bit(const a112_ekp *e, const char *name)
{
    for (uint32_t i = 0; i < a112_ekp_flag_count(e); i++)
        if (strcmp(a112_ekp_flag_name(e, i), name) == 0)
            return (int)i;
    return -1;
}

static uint32_t flags_of(const a112_ekp *e, const char *const *names, int n)
{
    uint32_t mask = 0;
    for (int i = 0; i < n; i++) {
        const int bit = flag_bit(e, names[i]);
        CHECK(bit >= 0);
        if (bit >= 0)
            mask |= 1u << bit;
    }
    return mask;
}

static int has_service(const a112_ekp *e, const a112_route *r, const char *name)
{
    for (uint32_t s = 0; s < a112_ekp_service_count(e); s++)
        if (((r->services[s >> 6] >> (s & 63)) & 1) && strcmp(a112_ekp_service_name(e, s), name) == 0)
            return 1;
    return 0;
}

/* Маршрут должен совпасть с ожидаемым набором служб (порядок не важен). */
static void expect_route(const a112_ekp *e, uint32_t code, const char *const *flags, int n_flags,
                         const char *const *want, int n_want)
{
    a112_route r;
    const int rc = a112_ekp_route(e, code, flags_of(e, flags, n_flags), &r);
    CHECK(rc == A112_OK);
    if (rc != A112_OK)
        return;
    CHECK(r.n_services == (uint32_t)n_want);
    for (int i = 0; i < n_want; i++) {
        checks++;
        if (!has_service(e, &r, want[i])) {
            failures++;
            fprintf(stderr, "code %u: missing service #%d\n", (unsigned)code, i);
        }
    }
}

static void test_without_table(void)
{
    a112_ekp *e = (a112_ekp *)&checks; /* заведомо не NULL: open обязан обнулить при ошибке */
    CHECK(a112_version()[0] != '\0');
    CHECK(a112_strerror(A112_ERR_FORMAT) != NULL);
    CHECK(a112_ekp_open(NULL, &e) == A112_ERR_ARG);
    CHECK(a112_ekp_open("definitely-missing.ekp", &e) == A112_ERR_IO);
    CHECK(e == NULL); /* при ошибке выходной указатель обнуляется */

    a112_route r;
    CHECK(a112_ekp_route(NULL, 1, 0, &r) == A112_ERR_ARG);
    CHECK(a112_ekp_flag_name(NULL, 0) == NULL);

    const uint32_t cpu = a112_cpu_features();
    printf("cpu:%s%s%s%s\n", (cpu & A112_CPU_SSE42) ? " sse4.2" : "", (cpu & A112_CPU_AVX2) ? " avx2" : "",
           (cpu & A112_CPU_FMA) ? " fma" : "", (cpu & A112_CPU_AVX512F) ? " avx512f" : "");
}

/* Кейсы из data/fixtures/stand_routing.json — кадры боевого стенда. */
static void test_stand_cases(const a112_ekp *e)
{
    const char *nd[] = {"НД"};
    const char *base[] = {"Служба 101", "ЦОДД", "ОАТИ"};
    expect_route(e, 1010101, nd, COUNT(nd), base, COUNT(base)); /* кадр 19 */

    const char *nd_ul[] = {"НД", "ТОННЕЛЬ", "УЛ"};
    const char *with_cemp[] = {"Служба 101", "ЦЭМП", "ЦОДД", "ОАТИ"};
    expect_route(e, 1010101, nd_ul, COUNT(nd_ul), with_cemp, COUNT(with_cemp)); /* кадр 22 */

    const char *all[] = {"НД", "ТОННЕЛЬ", "УЛ", "ПРАВОНАРУШЕНИЕ", "МЕДПОМОЩЬ", "ЭВАКУАЦИЯ", "ГАЗ"};
    const char *six[] = {"Служба 101", "Служба 104", "Служба 102", "ЦЭМП", "ЦОДД", "ОАТИ"};
    expect_route(e, 1010101, all, COUNT(all), six, COUNT(six)); /* кадр 28 */

    const char *only101[] = {"Служба 101"};
    expect_route(e, 1011100, NULL, 0, only101, COUNT(only101)); /* кадр 29, запах гари */

    a112_route r;
    CHECK(a112_ekp_route(e, 1010101, 0, &r) == A112_OK);
    CHECK(r.territorial != 0); /* территориальные ОИВ назначены — адрес их уточнит */
    CHECK(a112_ekp_route(e, 24010000, 0, &r) == A112_OK); /* восьмизначный код, группа 24 «БПЛА» */
    CHECK(a112_ekp_route(e, 1, 0, &r) == A112_ERR_NOT_FOUND);
    if (a112_ekp_flag_count(e) < 32)
        CHECK(a112_ekp_route(e, 1010101, 1u << a112_ekp_flag_count(e), &r) == A112_ERR_ARG);
}

static long file_size(FILE *f)
{
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    return n;
}

static int write_file(const char *path, const unsigned char *data, size_t n)
{
    FILE *f = fopen_u8(path, "wb");
    if (!f)
        return 0;
    const size_t w = fwrite(data, 1, n, f);
    fclose(f);
    return w == n;
}

/* Испорченный, усечённый и пустой файлы не должны открываться. */
static void test_corruption(const char *path)
{
    FILE *f = fopen_u8(path, "rb");
    CHECK(f != NULL);
    if (!f)
        return;
    const long n = file_size(f);
    unsigned char *buf = (unsigned char *)malloc((size_t)n);
    CHECK(buf && fread(buf, 1, (size_t)n, f) == (size_t)n);
    fclose(f);
    if (!buf)
        return;

    char tmp[4096];
    snprintf(tmp, sizeof tmp, "%s.corrupt", path);
    a112_ekp *e = NULL;

    buf[n / 2] ^= 0x5A; /* один байт в середине — ловит контрольная сумма */
    CHECK(write_file(tmp, buf, (size_t)n));
    CHECK(a112_ekp_open(tmp, &e) == A112_ERR_FORMAT);
    buf[n / 2] ^= 0x5A;

    CHECK(write_file(tmp, buf, (size_t)(n - 8)));
    CHECK(a112_ekp_open(tmp, &e) == A112_ERR_FORMAT);

    CHECK(write_file(tmp, buf, 0));
    CHECK(a112_ekp_open(tmp, &e) == A112_ERR_FORMAT);

    remove_u8(tmp);
    free(buf);
}

int main(int argc, char **argv)
{
#if defined(_WIN32)
    argv = utf8_argv(&argc);
#endif
    printf("libarm112 %s\n", a112_version());
    test_without_table();

    if (argc > 1) {
        a112_ekp *e = NULL;
        const int rc = a112_ekp_open(argv[1], &e);
        CHECK(rc == A112_OK);
        if (rc == A112_OK) {
            printf("classifier %s: %u incidents, %u services, %u flags\n", a112_ekp_source_version(e),
                   (unsigned)a112_ekp_incident_count(e), (unsigned)a112_ekp_service_count(e),
                   (unsigned)a112_ekp_flag_count(e));
            test_stand_cases(e);
            a112_ekp_close(e);
        }
        test_corruption(argv[1]);
    } else {
        printf("classifier.ekp not given: routing tests skipped\n");
    }

    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
