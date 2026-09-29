/* Отображение файла в память только для чтения: mmap / CreateFileMapping. */
#ifndef A112_MMAP_FILE_H
#define A112_MMAP_FILE_H

#include <stddef.h>
#include <stdint.h>

typedef struct a112_map {
    const uint8_t *data;
    size_t size;
    void *file;    /* Windows: HANDLE файла */
    void *mapping; /* Windows: HANDLE отображения */
} a112_map;

/* Путь в UTF-8 (на Windows переводится в UTF-16 — путь может быть кириллическим).
 * Пустой файл — A112_ERR_FORMAT: отображать нечего. */
int a112_map_open(const char *path_utf8, a112_map *m);
void a112_map_close(a112_map *m);

#endif
