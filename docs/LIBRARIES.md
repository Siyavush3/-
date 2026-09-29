# Сторонние библиотеки и компоненты

Список Python-пакетов собран автоматически по метаданным установленных пакетов: `python tools/licenses.py`.
`server.txt` — сервер тренажёра, `tts.txt` — необязательная озвучка (отдельный процесс), `asr.txt` —
необязательное распознавание речи оператора,
`toolchain.txt` — сборка C-ядра, `dev.txt` — тесты и сборка документации; последние два в поставку
не входят.

## Python-пакеты

| Пакет | Версия | Лицензия | Файл зависимостей | Назначение |
|---|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | server.txt | зависимость |
| annotated-types | 0.8.0 | MIT | server.txt | зависимость pydantic |
| anyio | 4.15.1 | MIT | server.txt | асинхронный ввод-вывод Starlette |
| certifi | 2026.7.22 | MPL-2.0 | asr.txt | зависимость requests |
| cffi | 2.1.1 | MIT-0 | asr.txt | зависимость vosk |
| charset-normalizer | 3.5.1 | MIT | asr.txt | зависимость requests |
| click | 8.5.0 | BSD-3-Clause | server.txt | командная строка uvicorn |
| cmake | 4.4.3 | Apache 2.0 | toolchain.txt | зависимость |
| colorama | 0.4.6 | BSD License | asr.txt | цвет в консоли (Windows) |
| coloredlogs | 15.0.1 | MIT | tts.txt | зависимость onnxruntime |
| DAWG2-Python | 0.9.0 | MIT | server.txt | словарь pymorphy3 |
| et_xmlfile | 2.0.0 | MIT | server.txt | зависимость openpyxl |
| fastapi | 0.141.1 | MIT | server.txt | веб-сервер: REST API |
| flatbuffers | 25.12.19 | Apache 2.0 | tts.txt | зависимость onnxruntime |
| h11 | 0.16.0 | MIT | server.txt | HTTP для uvicorn |
| httpcore2 | 2.13.1 | BSD-3-Clause | dev.txt | зависимость |
| httpx2 | 2.13.1 | BSD-3-Clause | dev.txt | зависимость |
| humanfriendly | 10.0 | MIT | tts.txt | зависимость onnxruntime |
| idna | 3.20 | BSD-3-Clause | server.txt | зависимость anyio |
| iniconfig | 2.3.0 | MIT | dev.txt | зависимость |
| lxml | 6.1.3 | BSD-3-Clause | dev.txt | зависимость python-docx |
| mpmath | 1.3.0 | BSD | tts.txt | зависимость sympy |
| ninja | 1.13.2 | Apache-2.0 | toolchain.txt | зависимость |
| numpy | 1.26.4 | BSD License | tts.txt | обработка звука |
| onnxruntime | 1.17.3 | MIT License | tts.txt | выполнение нейросети голоса на процессоре |
| openpyxl | 3.1.5 | MIT | server.txt | выгрузка отчётов в Excel |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | tts.txt | зависимость onnxruntime |
| pathvalidate | 3.3.1 | MIT License | tts.txt | зависимость |
| piper-tts | 1.8.0 | GPL-3.0-or-later | tts.txt | синтез речи (отдельный процесс озвучки) |
| pluggy | 1.6.0 | MIT | dev.txt | зависимость |
| protobuf | 7.36.2 | 3-Clause BSD License | tts.txt | зависимость onnxruntime |
| psutil | 7.1.0 | BSD-3-Clause | server.txt | мониторинг нагрузки |
| psycopg | 3.3.6 | LGPL-3.0-only | server.txt | драйвер PostgreSQL |
| pycparser | 3.0 | BSD-3-Clause | asr.txt | зависимость cffi |
| pydantic | 2.13.5 | MIT | server.txt | проверка входных данных API |
| pydantic_core | 2.46.5 | MIT | server.txt | ядро pydantic |
| Pygments | 2.21.0 | BSD-2-Clause | dev.txt | зависимость |
| pymorphy3 | 2.0.6 | MIT license | server.txt | морфология русского языка: орфография и словоформы |
| pymorphy3-dicts-ru | 2.4.417150.4580142 | MIT license | server.txt | словарь OpenCorpora для pymorphy3 |
| pyreadline3 | 3.5.6 | BSD | tts.txt | зависимость humanfriendly (Windows) |
| pytest | 9.1.1 | MIT | dev.txt | зависимость |
| python-docx | 1.2.0 | MIT | dev.txt | сборка документации .docx (tools/build_docs.py) |
| requests | 2.34.2 | Apache-2.0 | asr.txt | зависимость vosk (загрузка моделей, не используется) |
| srt | 3.5.3 | MIT | asr.txt | зависимость vosk (субтитры, не используется) |
| starlette | 1.7.0 | BSD-3-Clause | server.txt | основа FastAPI: маршрутизация, статика, WebSocket |
| sympy | 1.14.0 | BSD | tts.txt | зависимость onnxruntime |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | asr.txt | зависимость vosk |
| truststore | 0.10.4 | MIT | dev.txt | зависимость |
| typing_extensions | 4.16.0 | PSF-2.0 | server.txt | совместимость типов |
| typing-inspection | 0.4.4 | MIT | server.txt | зависимость pydantic |
| tzdata | 2026.4 | Apache-2.0 | server.txt | часовые пояса (Windows) |
| urllib3 | 2.8.0 | MIT | asr.txt | зависимость requests |
| uvicorn | 0.54.0 | BSD-3-Clause | server.txt | HTTP/WebSocket-сервер |
| vosk | 0.3.45 | Apache Software License | asr.txt | распознавание речи оператора (Kaldi) |
| websockets | 17.1 | BSD-3-Clause | server.txt | протокол WebSocket для uvicorn |
| ziglang | 0.16.0 | MIT | toolchain.txt | зависимость |

## Прочие компоненты

| Компонент | Лицензия | Как используется |
|---|---|---|
| Python 3.11 | PSF License | среда выполнения сервера; в `ARM112-Setup.exe` — встроенный Python 3.11.9 (python.org) |
| Inno Setup 6 | Inno Setup License (бесплатно, в том числе для коммерческого использования) | сборка установщика `ARM112-Setup.exe`; в поставке — только его программа установки |
| Microsoft Visual C++ Redistributable 2015–2022 (x64) | лицензия Microsoft на распространяемые компоненты | внутри `ARM112-Setup.exe`; ставится, только если в системе нет `msvcp140.dll` (нужна onnxruntime) |
| SQLite | общественное достояние | база данных по умолчанию (входит в Python) |
| PostgreSQL ≥ 12 | PostgreSQL License | база данных учебного класса (необязательно) |
| C-ядро `libarm112` | собственная разработка | маршрутизация по классификатору и справочнику служб; без него — эталон на Python |
| Веб-интерфейс (HTML, CSS, JS) | собственная разработка | без внешних библиотек и шрифтов: контур изолирован |
| espeak-ng (внутри piper-tts) | GPL-3.0 | фонемизация для синтеза речи; работает только в отдельном процессе озвучки |
| Голос `ru_RU-dmitri-medium` (piper) | данные: CC0; базовая модель lessac — исследовательская лицензия Blizzard 2013 | заявители (мужской голос) |
| Голос `ru_RU-ruslan-medium` (piper) | данные: CC BY-NC-SA 4.0 (некоммерческая); базовая модель lessac — исследовательская | старший бригады, заявители |
| Голос `ru_RU-irina-medium` (piper) | данные RHVoice — лицензия не указана; базовая модель lessac — исследовательская | заявители (женский голос) |
| Модель распознавания `vosk-model-small-ru-0.22` (Vosk) | Apache 2.0 (каталог моделей Vosk) | распознавание вопросов оператора 112 |
| Браузеры Chrome, Firefox, Яндекс.Браузер | лицензии производителей | рабочие места; в поставку не входят |

**Голоса.** Все русские голоса piper дообучены с английской модели lessac, данные которой выданы по
исследовательской лицензии (Research Licence Agreement, Blizzard Challenge 2013); у голоса ruslan данные
ещё и некоммерческие (CC BY-NC-SA 4.0). Для демонстрации на хакатоне это допустимо; для эксплуатации у
заказчика голоса нужно заменить моделью с подходящей лицензией — например, обучить piper с нуля на данных
CC0 (датасеты dmitri и denis) или использовать лицензированный синтез. Голоса лежат отдельно (`data/voices`),
замена не требует изменений в коде; без голосов тренажёр работает, реплики показываются текстом.

**GPL.** piper-tts 1.8 и espeak-ng распространяются по GPL-3.0. Сервер тренажёра их не импортирует:
озвучка — отдельная программа (`python -m speech.render`), сервер читает только готовые WAV-файлы.
