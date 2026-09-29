#if !defined(_WIN32)
#define _POSIX_C_SOURCE 200809L
#endif

#include "platform/mmap_file.h"

#include <stdint.h>
#include <string.h>

#include "arm112/arm112.h"

#if defined(_WIN32)

#define WIN32_LEAN_AND_MEAN
#include <stdlib.h>
#include <windows.h>

int a112_map_open(const char *path_utf8, a112_map *m)
{
    if (!path_utf8 || !m)
        return A112_ERR_ARG;
    memset(m, 0, sizeof *m);

    int wlen = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path_utf8, -1, NULL, 0);
    if (wlen <= 0)
        return A112_ERR_ARG;
    wchar_t *wpath = (wchar_t *)malloc((size_t)wlen * sizeof(wchar_t));
    if (!wpath)
        return A112_ERR_NOMEM;
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path_utf8, -1, wpath, wlen);

    HANDLE file = CreateFileW(wpath, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, NULL);
    free(wpath);
    if (file == INVALID_HANDLE_VALUE)
        return A112_ERR_IO;

    LARGE_INTEGER size;
    if (!GetFileSizeEx(file, &size)) {
        CloseHandle(file);
        return A112_ERR_IO;
    }
    if (size.QuadPart == 0) {
        CloseHandle(file);
        return A112_ERR_FORMAT;
    }
#if SIZE_MAX < UINT64_MAX
    if ((uint64_t)size.QuadPart > SIZE_MAX) {
        CloseHandle(file);
        return A112_ERR_IO;
    }
#endif

    HANDLE mapping = CreateFileMappingW(file, NULL, PAGE_READONLY, 0, 0, NULL);
    if (!mapping) {
        CloseHandle(file);
        return A112_ERR_IO;
    }
    const void *view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
    if (!view) {
        CloseHandle(mapping);
        CloseHandle(file);
        return A112_ERR_IO;
    }

    m->data = (const uint8_t *)view;
    m->size = (size_t)size.QuadPart;
    m->file = file;
    m->mapping = mapping;
    return A112_OK;
}

void a112_map_close(a112_map *m)
{
    if (!m)
        return;
    if (m->data)
        UnmapViewOfFile(m->data);
    if (m->mapping)
        CloseHandle((HANDLE)m->mapping);
    if (m->file)
        CloseHandle((HANDLE)m->file);
    memset(m, 0, sizeof *m);
}

#else /* POSIX */

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

int a112_map_open(const char *path_utf8, a112_map *m)
{
    if (!path_utf8 || !m)
        return A112_ERR_ARG;
    memset(m, 0, sizeof *m);

    int fd = open(path_utf8, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return A112_ERR_IO;
    struct stat st;
    if (fstat(fd, &st) != 0) {
        close(fd);
        return A112_ERR_IO;
    }
    if (st.st_size == 0) {
        close(fd);
        return A112_ERR_FORMAT;
    }
    void *p = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd); /* отображение живёт и без дескриптора */
    if (p == MAP_FAILED)
        return A112_ERR_IO;

    m->data = (const uint8_t *)p;
    m->size = (size_t)st.st_size;
    return A112_OK;
}

void a112_map_close(a112_map *m)
{
    if (!m)
        return;
    if (m->data)
        munmap((void *)m->data, m->size);
    memset(m, 0, sizeof *m);
}

#endif
