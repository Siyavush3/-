/*
 * libarm112 — вычислительное ядро тренажёра АРМ-112 (C11).
 *
 * Публичный API. Объекты ядра после открытия только читаются, поэтому
 * все функции потокобезопасны. Строки — UTF-8. Python-сторона вызывает
 * ядро через ctypes (pycore/arm112/_native.py); ctypes.CDLL отпускает GIL
 * на время вызова, так что параллельные вызовы из потоков не сериализуются.
 */
#ifndef ARM112_H
#define ARM112_H

#include <stdint.h>

#if defined(_WIN32)
#  if defined(A112_BUILDING)
#    define A112_API __declspec(dllexport)
#  else
#    define A112_API __declspec(dllimport)
#  endif
#elif defined(__GNUC__)
#  define A112_API __attribute__((visibility("default")))
#else
#  define A112_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Коды возврата. */
enum {
    A112_OK = 0,
    A112_ERR_ARG = -1,       /* неверный аргумент */
    A112_ERR_IO = -2,        /* файл не открылся или не отобразился в память */
    A112_ERR_FORMAT = -3,    /* файл повреждён или несовместимой версии */
    A112_ERR_NOT_FOUND = -4, /* нет такого кода / ключа */
    A112_ERR_NOMEM = -5
};

A112_API const char *a112_version(void);
A112_API const char *a112_strerror(int code);

/* Возможности процессора — для диспетчеризации SIMD-ядер во время выполнения.
 * Целевая база — AVX2 (Intel Core 8–10 поколения); AVX-512 есть не везде. */
enum {
    A112_CPU_SSE42 = 1u << 0,
    A112_CPU_AVX2 = 1u << 1,
    A112_CPU_FMA = 1u << 2,
    A112_CPU_AVX512F = 1u << 3
};
A112_API uint32_t a112_cpu_features(void);

/* ------------------------------------------------------------------------
 * ekp — маршрутизация по единому классификатору происшествий.
 *
 * Таблица строится из JSON-файлов data/classifier скриптом tools/compile_ekp.py
 * и отображается в память только для чтения. Семантику задаёт эталонная
 * модель pycore/arm112/reference/routing.py; ядро обязано с ней совпадать.
 * ------------------------------------------------------------------------ */
#define A112_EKP_MAX_FLAGS 32
#define A112_EKP_MAX_SERVICES 256

typedef struct a112_ekp a112_ekp;

typedef struct a112_route {
    uint64_t services[A112_EKP_MAX_SERVICES / 64]; /* бит i — служба i (a112_ekp_service_name) */
    uint32_t territorial;                          /* бит k — территориальный класс k */
    uint32_t n_services;                           /* число выставленных бит в services */
} a112_route;

A112_API int a112_ekp_open(const char *path_utf8, a112_ekp **out);
A112_API void a112_ekp_close(a112_ekp *ekp);

A112_API const char *a112_ekp_source_version(const a112_ekp *ekp);
A112_API uint32_t a112_ekp_flag_count(const a112_ekp *ekp);
A112_API const char *a112_ekp_flag_name(const a112_ekp *ekp, uint32_t index);
A112_API uint32_t a112_ekp_service_count(const a112_ekp *ekp);
A112_API const char *a112_ekp_service_name(const a112_ekp *ekp, uint32_t index);
A112_API uint32_t a112_ekp_territorial_count(const a112_ekp *ekp);
A112_API const char *a112_ekp_territorial_name(const a112_ekp *ekp, uint32_t index);
A112_API uint32_t a112_ekp_incident_count(const a112_ekp *ekp);
A112_API uint32_t a112_ekp_incident_code(const a112_ekp *ekp, uint32_t index);

/* Список служб для кода происшествия при выбранных флагах опросной карты.
 * flags — битовая маска по индексам a112_ekp_flag_name.
 * A112_ERR_NOT_FOUND — неизвестный код; A112_ERR_ARG — флаг вне таблицы. */
A112_API int a112_ekp_route(const a112_ekp *ekp, uint32_t code, uint32_t flags, a112_route *out);

#ifdef __cplusplus
}
#endif

#endif /* ARM112_H */
