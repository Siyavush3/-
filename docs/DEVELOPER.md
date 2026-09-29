# Руководство разработчика

Как собрать, запустить, проверить и выпустить тренажёр. Устройство системы — [SYSTEM.md](SYSTEM.md),
программный интерфейс — [API.md](API.md), формат сценариев — [SCENARIOS.md](SCENARIOS.md).

## 1. Окружение

Нужны Python 3.11 (подходят 3.10–3.12), git; для C-ядра — CMake ≥ 3.16 и компилятор C11 (GCC ≥ 9, Clang,
MSVC 2022). Без ядра маршрутизация идёт через эталонную модель на Python с тем же результатом.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements/dev.txt              # Linux: .venv/bin/pip
.venv/Scripts/pip install -r requirements/tts.txt -r requirements/asr.txt   # озвучка и голос — по желанию
python tools/fetch_voices.py && python tools/fetch_asr.py       # голоса piper и модель Vosk
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel
python server/run.py --demo                                     # http://127.0.0.1:8112, демо-учётки — data/demo/accounts.json
```

Windows без Visual Studio: компилятор `zig cc`, CMake и Ninja ставятся из PyPI (`requirements/toolchain.txt`) —
команды в корневом README. Ключи `server/run.py`: `--host`, `--port`, `--certfile`, `--keyfile`, `--demo`
(создать демо-учётки, если в базе нет пользователей); переменные окружения (`A112_DB`, `A112_VAR`, `A112_TTS`, `A112_ASR` и другие) — в
корневом README.

| Файл требований | Что ставит |
|---|---|
| `requirements/server.txt` | сервер: FastAPI, uvicorn, pymorphy3, psycopg, openpyxl, psutil |
| `requirements/tts.txt` | озвучка: piper-tts, onnxruntime, numpy (отдельный процесс) |
| `requirements/asr.txt` | распознавание речи: vosk и его зависимости |
| `requirements/dev.txt` | сервер + pytest, httpx2, openpyxl, python-docx |
| `requirements/toolchain.txt` | сборка C-ядра без Visual Studio: ziglang, cmake, ninja |

## 2. Тесты

```bash
python -m pytest                       # A112_REQUIRE_NATIVE=1 — C-ядро обязательно (так в CI)
ctest --test-dir build --output-on-failure
```

Тесты не трогают рабочие данные: каждый прогон — во временных каталогах, озвучка и предзагрузка распознавания
выключены (`tests/conftest.py`). Тесты настоящей озвучки и распознавания пропускаются, если нет голосов или
модели.

| Модуль | Что проверяет |
|---|---|
| `test_accounts.py` | учётные записи, сеансы, пароли, блокировки, журнал аудита, хранилище |
| `test_api.py` | занятия по настоящему HTTP и WebSocket, права ролей, досылка после обрыва |
| `test_engine.py`, `test_lesson.py` | движок и занятие на поддельных часах от начала до итога |
| `test_evaluation.py`, `test_fsm_reference.py` | оценка ДДС по категориям памятки, автомат статусов по треку стенда |
| `test_scenario.py`, `test_reference_stand.py`, `test_ekp_native.py` | маршрутизация карточек, сверка с кадрами стенда, C-ядро против эталона |
| `test_calls.py` | вызовы 112: заявитель-бот, распознавание вопроса, оценка карточки |
| `test_textcheck.py` | орфография, оформление, факты с учётом словоформ |
| `test_generator.py` | генерация сценариев с эталоном, правка по комментарию, образцы комментариев |
| `test_speech.py`, `test_asr.py` | озвучка (текст для синтеза, кэш, телефонная линия, очередь); распознавание речи, микрофон по WebSocket |
| `test_teaching.py`, `test_admin_ops.py` | преподаватель и обучающийся; администратор |
| `test_kiosk.py`, `test_setup.py`, `test_delivery.py` | киоск, установщики, дистрибутив, документация, список библиотек |

**CI** (`.github/workflows`): Linux — C-ядро в контейнере Ubuntu 20.04 (GCC 9, CMake 3.16, нижняя граница
поддержки), полный прогон на Ubuntu 22.04 и 24.04; Windows — сборка MSVC и полный прогон.

## 3. Правила кода

- **Движок занятия однопоточный и детерминированный.** Всё состояние занятия меняется в цикле событий:
  `Engine.handle(занятие, участник, сообщение)` и `Engine.tick()` возвращают список сообщений «кому — что».
  Часы подставляются, поэтому занятие целиком проверяется за миллисекунды.
- **Правильный ответ — за правилами заказчика.** Категории нарушений нумеруются как в памятке и инструкции,
  штрафы — в `data/evaluation`; языковая модель эталон не меняет.
- **База:** один SQL для SQLite и PostgreSQL (`server/store/db.py`). Схема меняется только новой миграцией в
  `server/store/schema.py` — старые не правятся.
- **Интерфейс:** без сборки и внешних библиотек; у каждой страницы свой скрипт в `web/js`, общий помощник —
  `api.js` (токен, запросы, WebSocket с досылкой).
- **Озвучка** — отдельный процесс (piper и espeak-ng распространяются по GPL-3.0, сервер их не импортирует);
  **распознавание речи** — в процессе сервера, в своём пуле потоков.
- **Документация — из кода, где можно.** Новый HTTP-запрос требует описания в `SUMMARY` в
  `tools/build_reference.py` (иначе упадёт тест); после изменений — `python tools/build_reference.py`,
  `python tools/licenses.py`, `python tools/build_docs.py`.

## 4. Как добавить

| Что | Где |
|---|---|
| Категорию нарушения | `data/evaluation/rubric*.json` (номер, название, штраф) и правило в `domain/evaluation.py` или `domain/calls.py`; тест с ошибкой, которая её вызывает |
| Вопрос оператора 112 | намерение в `INTENTS` (`domain/calls.py`), ответ в `dialogue` сценариев, кнопка в `web/op112.html`, слова — в `data/asr/vocabulary.txt` |
| Статус или переход ДДС | `data/fsm/response_status.json` с источником перехода; тесты автомата |
| Настройку администратора | `SCHEMA` в `server/ops/settings.py` и её применение в `apply_settings` (`server/app/main.py`) |
| Район для генератора | `data/gazetteer/moscow.json` |
| Сценарий | [SCENARIOS.md](SCENARIOS.md) |

## 5. Инструменты

| Команда | Что делает |
|---|---|
| `tools/build_classifier.py` | классификатор из xlsx заказчика → `data/classifier` |
| `tools/build_calls_pack.py` | сценарий вызовов 112 из билетов → `data/scenarios/calls_bilety.json` |
| `tools/compile_ekp.py` | таблица правил маршрутизации для C-ядра |
| `tools/routing_reference.py` | сверка маршрутизации с кадрами стенда (в CI) |
| `tools/fetch_voices.py`, `tools/fetch_asr.py` | голоса озвучки и модель распознавания с контрольными суммами; `--check` — только проверить |
| `tools/demo_call.py` | демо-звонок: озвучка реплик с телефонной линией |
| `tools/demo_dds.py` | автопилот ДДС: бот проходит занятие по эталону, человек смотрит |
| `tools/loadtest.py` | нагрузочный тест по требованиям ТЗ (`--cpus 4`, `--voice 20`) → `build/loadtest` |
| `tools/licenses.py` | список библиотек и лицензий → `docs/LIBRARIES.md` |
| `tools/build_reference.py` | справочные разделы документации из кода (`--check` — проверить) |
| `tools/build_docs.py` | сопроводительная документация → `docs/Сопроводительная документация.docx` |
| `tools/make_dist.py` | zip-дистрибутив для установки без интернета (`--platform windows` или `linux`) |
| `tools/make_setup.py` | установщик `ARM112-Setup-<версия>.exe` (Windows, Python 3.11, Inno Setup 6) |

## 6. Выпуск версии

1. Номер версии — `pycore/arm112/__init__.py` (`__version__`) и `CMakeLists.txt` (`project(... VERSION ...)`).
2. Проверки: `python -m pytest`, `ctest`, `python tools/loadtest.py --cpus 4`.
3. Документы: `python tools/build_reference.py`, `python tools/licenses.py`, `python tools/build_docs.py`.
4. Поставка: `python tools/make_setup.py` (Windows), `python tools/make_dist.py --platform linux` (Ubuntu); рядом
   с каждым файлом — `.sha256`.
5. Публикация: установщик и дистрибутивы — в GitHub Releases (в репозиторий файлы больше 100 МБ не помещаются).
