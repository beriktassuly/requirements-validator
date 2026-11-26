# Конфигурация запуска

## Установка

Нужен Python 3.15.2. Команды выполняются из корня проекта. В PowerShell можно запускать `.\.venv\Scripts\python.exe` напрямую или активировать виртуальное окружение.

На Linux и macOS выполните следующие команды.

```sh
python3.15 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Активируйте окружение командой `source .venv/bin/activate` или используйте `.venv/bin/python` вместо `python` в примерах.

## OpenAI

Основной профиль в `.env.example` использует OpenAI Responses. Заполните `OPENAI_API_KEY` и при необходимости укажите доступную вам модель. [Ключ OpenAI](https://platform.openai.com/api-keys) · [Рекомендации для актуальной модели](https://developers.openai.com/api/docs/guides/latest-model)

```dotenv
REQVALIDATOR_PROVIDER=openai
REQVALIDATOR_MODEL=gpt-6.2-sol
REQVALIDATOR_BASE_URL=
REQVALIDATOR_API_KEY=
OPENAI_API_KEY=
REQVALIDATOR_REASONING_EFFORT=medium
REQVALIDATOR_MIN_REQUEST_INTERVAL=0
REQVALIDATOR_CONCURRENCY=3
```

Для OpenAI Responses оставьте `REQVALIDATOR_SEED` пустым. Для reasoning-модели оставьте `REQVALIDATOR_TEMPERATURE` пустым, если модель её не поддерживает. Адаптер передаёт seed и temperature только при явной настройке и поддержке сервиса.

## Gemini

Для Gemini заполните `REQVALIDATOR_API_KEY` ключом Google AI Studio. На проверенном бесплатном проекте лимит составил 20 запросов в день для модели. Банковский PDF потребовал около 26 вызовов модели без учёта HTTP-повторов. Для такого PDF используйте платный проект с достаточной квотой. Для бесплатного проекта возьмите более короткий документ. [Ключ Gemini](https://aistudio.google.com/apikey) · [Квоты Gemini](https://ai.google.dev/gemini-api/docs/rate-limits)

```dotenv
REQVALIDATOR_PROVIDER=compatible
REQVALIDATOR_MODEL=gemini-2.5-flash
REQVALIDATOR_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
REQVALIDATOR_COMPATIBLE_FORMAT=json_schema
REQVALIDATOR_REASONING_EFFORT=
REQVALIDATOR_MIN_REQUEST_INTERVAL=5
REQVALIDATOR_CONCURRENCY=1
```

При HTTP 429 проверьте лимит проекта в Google AI Studio. Дневная квота сбрасывается в полночь по тихоокеанскому времени. При превышении лимита частоты увеличьте `REQVALIDATOR_MIN_REQUEST_INTERVAL`.

Если совместимый API не принимает строгую схему, переключите `REQVALIDATOR_COMPATIBLE_FORMAT` на `json_object` или `prompt`. Во всех форматах CLI разбирает JSON и проверяет его по схеме.

## Приоритет настроек и параллельность

Флаги CLI имеют приоритет над одноимёнными переменными окружения. Переменные окружения имеют приоритет над `.env`. Другой файл настроек можно передать через `--env-file`.

`--concurrency` или `REQVALIDATOR_CONCURRENCY` задаёт 1–8 одновременных запросов. По умолчанию используется 3, в примере Gemini — 1. Интервал между вызовами SDK общий. HTTP-повторы внутри SDK учитываются отдельно. Длительности пересекающихся этапов могут в сумме превышать общее время прогона.

## Вход и результат

PDF должен содержать текстовый слой. Для скана сначала выполните OCR. При первом прогоне проверьте строки таблиц, русские цитаты и номера страниц. Если весь документ превышает лимит входа, CLI сообщает об ограничении. Поддерживаются также Markdown, DOCX и TXT.

Второй позиционный аргумент задаёт путь к Markdown, `--json-output` сохраняет JSON для оценки. CLI сам создаёт выходной каталог и печатает пути записанных отчётов в stdout. Даже с одинаковым входом формулировки и число замечаний могут различаться. Цитаты сверяются с исходным документом в каждом прогоне.

В паспорт попадает `REQVALIDATOR_VERSION`, если он задан, иначе коммит локального Git, а в архиве исходников или образе без Git — запасная версия `v2.0.0`.

| Код возврата | Значение |
|---|---|
| `0` | Отчёт записан |
| `2` | Ошибка входа или конфигурации |
| `3` | Ошибка основного этапа модели |
| `4` | Ошибка записи |

При ошибке основного этапа возможен частичный отчёт с отметкой о покрытии. Вердикт о готовности сам по себе не меняет код возврата.

## Docker

Образ содержит CLI и его зависимости. Соберите образ один раз по [команде в README](../README.md#docker). Docker получает `.env` с хоста при запуске. Файл ключей не попадает в образ. На Linux для записи от своего пользователя добавьте `--user "$(id -u):$(id -g)"` перед именем образа.
