# Агент проверки требований BCC-HUB × MNU FinTech

Агент принимает PDF, Markdown, DOCX или TXT, выделяет требования и сохраняет отчёт на русском. Для каждого требования и всего набора есть отдельная проверка. Содержимое замечаний связано с фактическими цитатами, страницами и строками. Рубрика сопоставлена с ISO/IEC/IEEE 29148:2018 (5.2.5, 5.2.6) и IEEE 830-1998 (4.3). [ТЗ](https://github.com/BCC-HUB/ai-requirements-validator-task#readme)

## Установка

Нужен Python 3.15.2. Команды выполняются из корня распакованного проекта. На Windows можно работать из PowerShell без активации venv.

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

На Linux и macOS используйте `python3.15 -m venv .venv`, затем `.venv/bin/python -m pip install -r requirements.txt` и `cp .env.example .env`. Ниже `python` означает Python из этого окружения. На Windows замените его на `.\.venv\Scripts\python.exe` либо активируйте venv штатным способом.

## Ключ и модель

Получите свой ключ Gemini в Google AI Studio и заполните в `.env` поле `REQVALIDATOR_API_KEY`. В `.env.example` уже заданы проверенные параметры для Google API.

```dotenv
REQVALIDATOR_PROVIDER=compatible
REQVALIDATOR_MODEL=gemini-2.5-flash
REQVALIDATOR_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
REQVALIDATOR_COMPATIBLE_FORMAT=json_schema
REQVALIDATOR_MIN_REQUEST_INTERVAL=5
REQVALIDATOR_CONCURRENCY=1
```

Последовательный контрольный прогон Gemini занял около 16 минут. Прогон с тремя одновременными запросами завершился за 7:43 с повторами HTTP 429 и 503 и частичным семантическим слиянием. Попытки с двумя и одним запросом шли следом на том же проекте и прервались из-за 429. Контрольный запрос подтвердил исчерпание суточной квоты проекта. Для оценки влияния параллельности нужен отдельный замер после сброса квоты. В примере задан один запрос за раз.

На бесплатном тарифе даже один полный прогон такого PDF может исчерпать дневную квоту. В ответе на контрольный запрос Google указал лимит нашего проекта — 20 запросов в день для этой модели. Фактические лимиты своего проекта смотрите в Google AI Studio. При HTTP 429 и исчерпанной дневной квоте повторите после её сброса в полночь по тихоокеанскому времени. При лимите частоты увеличьте `REQVALIDATOR_MIN_REQUEST_INTERVAL`. При HTTP 503 попробуйте позже. [Получить Gemini-ключ](https://aistudio.google.com/apikey) · [Квоты Gemini](https://ai.google.dev/gemini-api/docs/rate-limits)

Для прямого OpenAI поменяйте профиль в `.env` следующим образом. Вставьте ключ, созданный в собственном проекте OpenAI, и при необходимости замените пример модели на доступную.

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

Для OpenAI Responses оставьте `REQVALIDATOR_SEED` пустым. Для reasoning-моделей оставьте `REQVALIDATOR_TEMPERATURE` пустым, если документация выбранной модели её не поддерживает. Для совместимого сервиса можно переключить `REQVALIDATOR_COMPATIBLE_FORMAT` на `json_object` или `prompt`, если endpoint не принимает строгую схему. Парсинг и валидация схемы на стороне агента остаются во всех режимах. Seed и temperature передаются только по явной настройке и при поддержке сервиса. Переменные среды имеют приоритет над `.env`. [Ключ OpenAI](https://platform.openai.com/api-keys) · [Рекомендации для актуальной модели](https://developers.openai.com/api/docs/guides/latest-model)

## Прогон

Положите PDF с текстовым слоем в `input/requirements.pdf` либо подставьте собственный путь. Выходной каталог создаётся автоматически. Markdown пишется на второй путь, JSON для оценки можно запросить отдельно.

```powershell
python -m reqvalidator input/requirements.pdf reports/review.md --json-output reports/review.json
```

Отчёт для сдачи получен версией `v1.0.0` (коммит `0fd8b02` из паспорта), через OpenAI Responses с моделью `gpt-6.2-sol`, без дополнительных ролей. [Отчёт Markdown](reports/submission.md) · [Данные JSON](reports/submission.json)

Пример прогона с `--reviewers qa,api --external-docs` на коммите `f56a59c`. [Отчёт с дополнительными ролями](reports/pr3-openai-f56a59c/review.md)

Проверочный прогон Gemini на том же PDF выявил все четыре контрольных дефекта и дал больше замечаний и повторов.

Даже при одинаковом входном файле и настройках ответы модели могут различаться. При повторном прогоне могут измениться число и формулировки замечаний. Агент в каждом прогоне сверяет цитаты с исходным документом.

Markdown организатора уже есть в проекте. Оценщику передаётся тот же файл, который проверил агент: он проверяет хеш и координаты цитат, в том числе страницы PDF.

```powershell
python -m reqvalidator tests/fixtures/requirements.md reports/review-md.md --json-output reports/review-md.json
```

В качестве версии отчёт использует `REQVALIDATOR_VERSION`, затем коммит локального Git, затем тег этого исходного архива `v0.1.0`. Возврат 0 означает, что файлы записаны. Код 2 обозначает ошибку входа или конфигурации, 3 — ошибку этапа модели, 4 — ошибку записи. При ошибке этапа возможен частичный отчёт, его покрытие указано в сводке. Сам вердикт о готовности не меняет код возврата.

PDF без текстового слоя требует предварительного OCR. Если документ целиком превышает настроенный лимит входа, агент сообщает об ограничении. При первом прогоне проверьте извлечённые строки таблицы, русские цитаты и совпадение страниц в исходном PDF. В stdout выводятся пути записанных файлов, замечания находятся в отчёте.

### Дополнительные режимы

Прогресс, замеры, дополнительные ревьюеры и проверка внешних источников выключены по умолчанию. Их можно включать по отдельности. Флаги имеют приоритет над одноимёнными настройками окружения и `.env`.

```powershell
python -m reqvalidator input/requirements.pdf reports/review.md --json-output reports/review.json --progress --timings
python -m reqvalidator input/requirements.pdf reports/roles.md --json-output reports/roles.json --reviewers qa,api --reviewer-max-findings 5
python -m reqvalidator input/requirements.pdf reports/sources.md --json-output reports/sources.json --external-docs
```

- `--progress` выводит в stderr начало и конец этапов, а также ход проверки пакетов. `--quiet` скрывает строки прогресса. `--timings` печатает итоговые замеры в stderr и добавляет поле `timings` в JSON при `--json-output`. Для эксперимента со слиянием задайте `--merge-reasoning-effort low`, если модель принимает это значение.
- `--concurrency` или `REQVALIDATOR_CONCURRENCY` задаёт число одновременных запросов (1–8, без настройки — 3, в примере Gemini — 1). Интервал между вызовами SDK общий. Повторы HTTP внутри SDK учитываются отдельно. При параллельном запуске длительности пересекающихся этапов не складываются в общее время.
- `--reviewers qa[,api]` добавляет проверку сценариев с неопределённым ожидаемым результатом (`qa`) и проверку явно описанного контракта (`api`). Каждая роль получает документ целиком, пока он помещается в её бюджет запроса, или обрабатывает пакеты существующих блоков. Если основной проход целиком невозможен, явно включённые роли всё равно проверяют документ, а отчёт отмечает неполное покрытие. `--reviewer-max-findings` ограничивает число находок за один запрос. Метки ревьюеров сохраняются при слиянии карточек. Если дополнительная роль завершится с ошибкой, отчёт отметит это в диагностике и в поле `failed_reviewers` в JSON. Покрытие основного анализа оценивается отдельно.
- `--external-docs` проверяет явно указанные версии известных внешних библиотек и API. Для Context7 укажите `CONTEXT7_API_KEY`. Поиск передаёт только идентификатор известной технологии, версию и фиксированный технический вопрос. Ссылки и статусы находятся в `external_references`. Проверка конкретного утверждения доступна для поддерживаемых правил, например для значения `redirect_slashes` по умолчанию в FastAPI. Если источник недоступен или обслужил другую версию, это отражается в статусе.

### Предложить формулировку

Команда `rewrite` использует готовый JSON, точный исходный файл и ID замечания. Результат записывается отдельным JSON с предложенным текстом, блоками для правки и критериями приёмки. Если нужно решение аналитика, команда просит у модели несколько вариантов и конкретный вопрос.

```powershell
python -m reqvalidator.rewrite reports/review.json --finding D-002 --document input/requirements.pdf --output reports/rewrite-D-002.json
```

## Docker

Образ содержит только CLI и его зависимости. Создайте `.env` на хосте. Первый вызов собирает образ один раз, второй выполняет полный прогон одной командой.

```powershell
docker build -t bcc-reqvalidator:0.1.0 .
docker run --rm --env-file .env -v "${PWD}:/io" bcc-reqvalidator:0.1.0 /io/input/requirements.pdf /io/reports/review.md --json-output /io/reports/review.json
```

На Linux для записи от своего пользователя можно добавить `--user "$(id -u):$(id -g)"` перед именем образа. `.env` передаётся при запуске и в образ не включается.

## Фикстуры, тесты и оценка

Установите dev-зависимости в то же окружение. Обычные тесты подменяют модель заглушкой и не требуют ключа. Для живых проверок задайте рабочий `.env` и явно передайте `--live`.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest -m live --live
```

Сгенерировать PDF из включённой Markdown-фикстуры можно следующей командой. При отсутствии подходящего кириллического моноширинного шрифта передайте `--font путь/к/шрифту.ttf`.

```powershell
python -m tests.generate_pdf tests/fixtures/requirements.md tests/fixtures/requirements.pdf
python -m reqvalidator tests/fixtures/requirements.pdf reports/review-pdf.md --json-output reports/review-pdf.json
```

После прогона оценщик проверяет SHA входа, координаты и дословность цитат, полноту по вручную размеченным дефектам и дубли. По умолчанию сопоставление лексическое. Флаг `--semantic` проверяет смысл через настроенную LLM и ограничивает кандидатов пересечением с блоками источника. Оба показателя сохраняются отдельно. Несопоставленные замечания считаются кандидатами в ложные срабатывания. Для подтверждённого числа передайте `--adjudications` с JSON-объектом вида `{"D-001": "valid", "D-002": "false_positive"}`.

```powershell
python -m evaluation.score --document tests/fixtures/requirements.md --report reports/review-md.json --output reports/eval.json
```

`reqvalidator/` содержит ядро, провайдеры, загрузку, нарезку, доказательства и CLI. `tests/` хранит фикстуры и регрессии. `evaluation/` содержит эталон и оценщик. [Архитектурные решения](docs/DECISIONS.md)
