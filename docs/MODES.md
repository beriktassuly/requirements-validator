# Режимы, тесты и оценка

## Дополнительные режимы

Прогресс, замеры, дополнительные роли и проверка внешних источников выключены по умолчанию. Каждую функцию можно включить отдельно. Флаги имеют приоритет над окружением и `.env`. [Конфигурация запуска](CONFIGURATION.md)

```powershell
python -m reqvalidator input/requirements.pdf reports/review.md --json-output reports/review.json --progress --timings
python -m reqvalidator input/requirements.pdf reports/roles.md --json-output reports/roles.json --reviewers qa,api --reviewer-max-findings 5
python -m reqvalidator input/requirements.pdf reports/sources.md --json-output reports/sources.json --external-docs
```

### Прогресс и отладка

`--progress` показывает начало и конец этапов и ход проверки пакетов в stderr. `--quiet` скрывает строки прогресса. `--timings` печатает сводку в stderr и добавляет поле `timings` в отчёт при `--json-output`. Для отдельного уровня reasoning на слиянии укажите, например, `--merge-reasoning-effort low`, если модель его поддерживает.

`--debug-proposals PATH.json` сохраняет подтверждённые цитатами находки до слияния. Используйте отдельный путь, отличный от входа, Markdown и JSON отчёта.

### Дополнительные роли

`--reviewers qa[,api]` проверяет сценарии с неопределённым ожидаемым результатом (`qa`) и явно описанный контракт (`api`). Каждая роль получает документ целиком, пока он помещается в бюджет запроса. Для более длинного документа роль обрабатывает пакеты блоков. Роли могут продолжить проверку при превышении лимита основного прохода, а отчёт покажет неполное покрытие.

`--reviewer-max-findings` задаёт предел находок за один запрос. Метки ролей сохраняются при слиянии. Сбой роли отражается в диагностике и `failed_reviewers` в JSON. Покрытие основного анализа оценивается отдельно.

### Внешние источники

`--external-docs` проверяет явно названные версии известных библиотек и API. Для Context7 укажите `CONTEXT7_API_KEY`. В поиск передаются идентификатор известной технологии, версия и фиксированный технический вопрос. В `external_references` записываются ссылки и статусы, в том числе недоступность источника и несовпадение версии. Сверка конкретного утверждения доступна для поддерживаемых правил, например для значения `redirect_slashes` по умолчанию в FastAPI.

### Новая формулировка требования

`rewrite` принимает готовый JSON, точный исходный файл и ID замечания. Отдельный JSON содержит предлагаемый текст, блоки для правки и критерии приёмки. Если требуется решение аналитика, команда просит несколько вариантов и конкретный вопрос.

```powershell
python -m reqvalidator.rewrite reports/review.json --finding D-002 --document input/requirements.pdf --output reports/rewrite-D-002.json
```

## Фикстуры, тесты и оценка

Установите dev-зависимости в то же окружение. Обычные тесты подменяют модель заглушкой и работают без ключа. Для живых тестов настройте рабочий `.env` и явно передайте `--live`.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest -m live --live
```

Markdown организатора входит в проект. Его можно проверить напрямую или сгенерировать PDF. Если подходящего кириллического моноширинного шрифта нет, передайте `--font путь/к/шрифту.ttf`.

```powershell
python -m reqvalidator tests/fixtures/requirements.md reports/review-md.md --json-output reports/review-md.json
python -m tests.generate_pdf tests/fixtures/requirements.md tests/fixtures/requirements.pdf
python -m reqvalidator tests/fixtures/requirements.pdf reports/review-pdf.md --json-output reports/review-pdf.json
```

Оценщик принимает тот же документ, по которому сделан отчёт. Он сверяет хеш, дословность цитат и координаты, включая страницы PDF, считает покрытие вручную размеченных дефектов и дубли. По умолчанию используется лексическое сопоставление. `--semantic` проверяет кандидатов через настроенную модель и ограничивает их пересечением с блоками источника. Лексический и семантический результаты сохраняются отдельно.

Несопоставленные замечания считаются кандидатами в ложные срабатывания. Для подтверждённого подсчёта передайте `--adjudications` с JSON-объектом вида `{"D-001": "valid", "D-002": "false_positive"}`.

```powershell
python -m evaluation.score --document tests/fixtures/requirements.md --report reports/review-md.json --output reports/eval.json
```

Код валидатора находится в `reqvalidator/`, фикстуры и регрессии — в `tests/`, эталон и оценщик — в `evaluation/`. [Архитектурные решения](DECISIONS.md)
