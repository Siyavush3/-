#include "arm112/arm112.h"

#ifndef A112_VERSION_STRING
#define A112_VERSION_STRING "0.0.0"
#endif

const char *a112_version(void)
{
    return A112_VERSION_STRING;
}

const char *a112_strerror(int code)
{
    switch (code) {
    case A112_OK:
        return "успешно";
    case A112_ERR_ARG:
        return "неверный аргумент";
    case A112_ERR_IO:
        return "ошибка ввода-вывода";
    case A112_ERR_FORMAT:
        return "файл повреждён или несовместимой версии";
    case A112_ERR_NOT_FOUND:
        return "не найдено";
    case A112_ERR_NOMEM:
        return "недостаточно памяти";
    default:
        return "неизвестная ошибка";
    }
}
