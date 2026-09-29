/*
 * ekp — маршрутизация по единому классификатору происшествий (ARCHITECTURE.md §6.8, §10.8).
 *
 * Таблица (FORMAT.md) отображается в память только для чтения. При открытии файл
 * проверяется целиком: границы разделов, контрольная сумма, порядок кодов,
 * диапазоны индексов. После этого маршрутизация не может выйти за пределы
 * отображения даже на испорченном файле — испорченный просто не откроется.
 *
 * Семантика — pycore/arm112/reference/routing.py; расхождение с эталоном — ошибка.
 */
#include <stdlib.h>
#include <string.h>

#include "arm112/arm112.h"
#include "platform/mmap_file.h"

#define EKP_FORMAT_VERSION 1u
#define EKP_HEADER_SIZE 64u
#define EKP_INCIDENT_SIZE 12u
#define EKP_ENTRY_SIZE 8u
#define EKP_MAX_TERRITORIAL 32u

enum { KIND_SERVICE = 0, KIND_TERRITORIAL = 1 };
enum { COND_ALWAYS = 0, COND_ALL_OF = 1, COND_NONE_OF = 2 };

struct a112_ekp {
    a112_map map;
    uint32_t n_flags, n_services, n_territorial, n_incidents, n_entries;
    const uint8_t *names;     /* смещения строк: флаги, службы, терр. классы, версия источника */
    const uint8_t *incidents; /* {code, first, count} по возрастанию code */
    const uint8_t *entries;   /* {target:u16, kind:u8, cond:u8, mask:u32} */
    const char *strings;
};

/* Чтение little-endian независимо от выравнивания и порядка байт платформы. */
static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t fnv1a32(const uint8_t *p, size_t n)
{
    uint32_t h = 0x811C9DC5u;
    for (size_t i = 0; i < n; i++) {
        h ^= p[i];
        h *= 0x01000193u;
    }
    return h;
}

static int section_fits(uint64_t off, uint64_t count, uint64_t item, uint64_t size)
{
    return off >= EKP_HEADER_SIZE && off % 8 == 0 && off <= size && count * item <= size - off;
}

static int validate(a112_ekp *e)
{
    const uint8_t *d = e->map.data;
    const uint64_t size = e->map.size;

    if (size < EKP_HEADER_SIZE || memcmp(d, "A112EKP", 8) != 0) /* 7 символов + '\0' */
        return A112_ERR_FORMAT;
    if (rd32(d + 8) != EKP_FORMAT_VERSION)
        return A112_ERR_FORMAT;

    e->n_flags = rd32(d + 12);
    e->n_services = rd32(d + 16);
    e->n_territorial = rd32(d + 20);
    e->n_incidents = rd32(d + 24);
    e->n_entries = rd32(d + 28);
    const uint32_t off_names = rd32(d + 32);
    const uint32_t off_incidents = rd32(d + 36);
    const uint32_t off_entries = rd32(d + 40);
    const uint32_t off_strings = rd32(d + 44);
    const uint32_t size_strings = rd32(d + 48);
    const uint32_t file_size = rd32(d + 52);
    const uint32_t checksum = rd32(d + 56);

    if (file_size != size)
        return A112_ERR_FORMAT;
    if (e->n_flags > A112_EKP_MAX_FLAGS || e->n_services > A112_EKP_MAX_SERVICES ||
        e->n_territorial > EKP_MAX_TERRITORIAL)
        return A112_ERR_FORMAT;
    if (fnv1a32(d + EKP_HEADER_SIZE, (size_t)(size - EKP_HEADER_SIZE)) != checksum)
        return A112_ERR_FORMAT;

    const uint64_t n_names = (uint64_t)e->n_flags + e->n_services + e->n_territorial + 1;
    if (!section_fits(off_names, n_names, 4, size) ||
        !section_fits(off_incidents, e->n_incidents, EKP_INCIDENT_SIZE, size) ||
        !section_fits(off_entries, e->n_entries, EKP_ENTRY_SIZE, size) ||
        !section_fits(off_strings, size_strings, 1, size))
        return A112_ERR_FORMAT;
    /* Последний байт строк — '\0', значит любое смещение внутри раздела даёт завершённую строку. */
    if (size_strings == 0 || d[off_strings + size_strings - 1] != '\0')
        return A112_ERR_FORMAT;

    e->names = d + off_names;
    e->incidents = d + off_incidents;
    e->entries = d + off_entries;
    e->strings = (const char *)(d + off_strings);

    for (uint64_t i = 0; i < n_names; i++)
        if (rd32(e->names + 4 * i) >= size_strings)
            return A112_ERR_FORMAT;

    for (uint32_t i = 0; i < e->n_incidents; i++) {
        const uint8_t *p = e->incidents + (size_t)i * EKP_INCIDENT_SIZE;
        if (i > 0 && rd32(p) <= rd32(p - EKP_INCIDENT_SIZE))
            return A112_ERR_FORMAT; /* коды строго по возрастанию — иначе не работает двоичный поиск */
        if ((uint64_t)rd32(p + 4) + rd32(p + 8) > e->n_entries)
            return A112_ERR_FORMAT;
    }

    const uint32_t flag_mask = e->n_flags >= 32 ? 0xFFFFFFFFu : ((1u << e->n_flags) - 1u);
    for (uint32_t i = 0; i < e->n_entries; i++) {
        const uint8_t *p = e->entries + (size_t)i * EKP_ENTRY_SIZE;
        const uint16_t target = rd16(p);
        const uint8_t kind = p[2], cond = p[3];
        const uint32_t mask = rd32(p + 4);
        if (kind == KIND_SERVICE) {
            if (target >= e->n_services)
                return A112_ERR_FORMAT;
        } else if (kind == KIND_TERRITORIAL) {
            if (target >= e->n_territorial)
                return A112_ERR_FORMAT;
        } else {
            return A112_ERR_FORMAT;
        }
        if (cond > COND_NONE_OF || (mask & ~flag_mask) != 0 || (cond == COND_ALWAYS && mask != 0))
            return A112_ERR_FORMAT;
    }
    return A112_OK;
}

int a112_ekp_open(const char *path_utf8, a112_ekp **out)
{
    if (!path_utf8 || !out)
        return A112_ERR_ARG;
    *out = NULL;
    a112_ekp *e = (a112_ekp *)calloc(1, sizeof *e);
    if (!e)
        return A112_ERR_NOMEM;
    int rc = a112_map_open(path_utf8, &e->map);
    if (rc == A112_OK)
        rc = validate(e);
    if (rc != A112_OK) {
        a112_map_close(&e->map);
        free(e);
        return rc;
    }
    *out = e;
    return A112_OK;
}

void a112_ekp_close(a112_ekp *ekp)
{
    if (!ekp)
        return;
    a112_map_close(&ekp->map);
    free(ekp);
}

static const char *name_at(const a112_ekp *e, uint64_t i)
{
    return e->strings + rd32(e->names + 4 * i);
}

const char *a112_ekp_source_version(const a112_ekp *e)
{
    return e ? name_at(e, (uint64_t)e->n_flags + e->n_services + e->n_territorial) : NULL;
}

uint32_t a112_ekp_flag_count(const a112_ekp *e) { return e ? e->n_flags : 0; }
uint32_t a112_ekp_service_count(const a112_ekp *e) { return e ? e->n_services : 0; }
uint32_t a112_ekp_territorial_count(const a112_ekp *e) { return e ? e->n_territorial : 0; }
uint32_t a112_ekp_incident_count(const a112_ekp *e) { return e ? e->n_incidents : 0; }

const char *a112_ekp_flag_name(const a112_ekp *e, uint32_t i)
{
    return (e && i < e->n_flags) ? name_at(e, i) : NULL;
}

const char *a112_ekp_service_name(const a112_ekp *e, uint32_t i)
{
    return (e && i < e->n_services) ? name_at(e, (uint64_t)e->n_flags + i) : NULL;
}

const char *a112_ekp_territorial_name(const a112_ekp *e, uint32_t i)
{
    return (e && i < e->n_territorial) ? name_at(e, (uint64_t)e->n_flags + e->n_services + i) : NULL;
}

uint32_t a112_ekp_incident_code(const a112_ekp *e, uint32_t i)
{
    return (e && i < e->n_incidents) ? rd32(e->incidents + (size_t)i * EKP_INCIDENT_SIZE) : 0;
}

int a112_ekp_route(const a112_ekp *e, uint32_t code, uint32_t flags, a112_route *out)
{
    if (!e || !out)
        return A112_ERR_ARG;
    memset(out, 0, sizeof *out);
    if (e->n_flags < 32 && (flags >> e->n_flags) != 0)
        return A112_ERR_ARG;

    /* Двоичный поиск первого кода >= code. */
    uint32_t lo = 0, hi = e->n_incidents;
    while (lo < hi) {
        const uint32_t mid = lo + (hi - lo) / 2;
        if (rd32(e->incidents + (size_t)mid * EKP_INCIDENT_SIZE) < code)
            lo = mid + 1;
        else
            hi = mid;
    }
    const uint8_t *inc = e->incidents + (size_t)lo * EKP_INCIDENT_SIZE;
    if (lo == e->n_incidents || rd32(inc) != code)
        return A112_ERR_NOT_FOUND;

    const uint32_t first = rd32(inc + 4), count = rd32(inc + 8);
    for (uint32_t i = first; i < first + count; i++) {
        const uint8_t *p = e->entries + (size_t)i * EKP_ENTRY_SIZE;
        const uint16_t target = rd16(p);
        const uint8_t kind = p[2], cond = p[3];
        const uint32_t mask = rd32(p + 4);

        const int ok = cond == COND_ALWAYS || (cond == COND_ALL_OF && (flags & mask) == mask) ||
                       (cond == COND_NONE_OF && (flags & mask) == 0);
        if (!ok)
            continue;
        if (kind == KIND_SERVICE) {
            const uint64_t bit = 1ull << (target & 63);
            if (!(out->services[target >> 6] & bit)) {
                out->services[target >> 6] |= bit;
                out->n_services++;
            }
        } else {
            out->territorial |= 1u << target;
        }
    }
    return A112_OK;
}
