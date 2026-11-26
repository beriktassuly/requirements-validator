from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import re
from threading import Lock
from time import sleep

import pytest

from reqvalidator import llm
from reqvalidator.config import ConfigurationError, Settings
from reqvalidator.document import load_document
from reqvalidator.ground import deduplicate
from reqvalidator.llm import ReviewError
from reqvalidator.model import Anchor, Finding, LocalResponse, MergeResponse
from reqvalidator.report import render_json, render_markdown
from reqvalidator.review import ReviewEngine
from reqvalidator.timing import RunTimings
from tests.fakes import TWO_RULES, ScriptedReviewer, T, write_rule_fixture
from tests.test_review_cli import _configure_offline

PASSPORT = {"model": "offline", "provider": "offline", "version": "test"}


def _many_rules(path: Path) -> Path:
    extra = "".join(f"* **Ф-{n}.** Сервис хранит значение номер {n}.\n" for n in range(3, 7))
    path.write_text(TWO_RULES + extra, encoding="utf-8")
    return path


class SlowReviewer(ScriptedReviewer):
    """Thread-safe fake. The first batch answers last, so completion order differs from batch order."""

    def __init__(self, *, fail_source_id: str | None = None) -> None:
        super().__init__()
        self.fail_source_id = fail_source_id
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def ask(self, system: str, prompt: str, schema: type[T]) -> T:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if schema is LocalResponse:
                match = re.search(r"Целевые ID:\s*(\[[^\n]*\])", prompt)
                assert match is not None
                (target,) = json.loads(match[1])
                inventory, _ = json.JSONDecoder().raw_decode(prompt.partition("Документ JSON:\n")[2].lstrip())
                source_id = next(b["source_id"] for b in inventory["blocks"] if b["id"] == target)
                sleep(0.3 if source_id == "Ф-1" else 0.02)
                if source_id == self.fail_source_id:
                    raise ReviewError("Провайдер исчерпал квоту.", fatal=True)
            with self._lock:
                return super().ask(system, prompt, schema)
        finally:
            with self._lock:
                self.active -= 1


def _same(left, right) -> bool:
    return render_markdown(replace(left, reviewed_at="")) == render_markdown(replace(right, reviewed_at=""))


def test_parallel_local_batches_produce_the_same_report(tmp_path: Path) -> None:
    document = load_document(_many_rules(tmp_path / "input.md"))
    sequential = ReviewEngine(SlowReviewer(), local_batch_size=1).run(document, **PASSPORT)
    reviewer = SlowReviewer()
    parallel = ReviewEngine(reviewer, local_batch_size=1, concurrency=3).run(document, **PASSPORT)
    assert reviewer.peak >= 2
    assert _same(parallel, sequential)


def test_fatal_local_error_blocks_later_batches_like_sequential_run(tmp_path: Path) -> None:
    document = load_document(_many_rules(tmp_path / "input.md"))
    sequential = ReviewEngine(SlowReviewer(fail_source_id="Ф-3"), local_batch_size=1).run(document, **PASSPORT)
    parallel = ReviewEngine(
        SlowReviewer(fail_source_id="Ф-3"), local_batch_size=1, concurrency=3,
    ).run(document, **PASSPORT)
    for report in (sequential, parallel):
        assert report.failed_stages == 1
        assert "Проверка набора пропущена после ошибки обращения к модели." in report.warnings
        later = [row for row in report.requirements if row.block.source_id in {"Ф-4", "Ф-5", "Ф-6"}]
        assert later and all(check.status == "uncertain" for row in later for check in row.checks)
    assert _same(parallel, sequential)


def test_limiter_spaces_starts_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    waits = []

    def advance(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    monkeypatch.setattr(llm, "monotonic", lambda: now)
    monkeypatch.setattr(llm, "sleep", advance)
    settings = Settings(
        provider="compatible", model="offline", api_key="local",
        base_url="http://localhost/v1", min_request_interval=10,
    )
    with llm.OpenAIReviewer(settings) as reviewer:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: reviewer._wait_for_slot(), range(4)))
    assert waits == [10, 10, 10]


def test_timings_counters_survive_threads() -> None:
    timings = RunTimings()

    def work(_: int) -> None:
        for _ in range(200):
            with timings.model_call(measured_transport=False):
                pass
            with timings.retry("format"):
                pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    assert timings.model_calls == 1600
    assert timings.retries["format"] == 1600


def _finding(block: str, title: str, recommendation: str, line: int) -> Finding:
    anchor = Anchor(block, title, line, line, 1, len(title), None, None)
    return Finding("completeness", "minor", title, "Описание.", recommendation, None, (), (anchor,), ("set",))


def test_both_failed_merge_passes_emit_one_warning() -> None:
    class UnavailableMergeReviewer(ScriptedReviewer):
        def ask(self, system, prompt, schema):
            assert schema is MergeResponse
            self.calls[schema.__name__] += 1
            raise ReviewError("HTTP 429", fatal=True)

    first = replace(_finding("B001", "Нет порога", "Указать порог.", 1), severity="critical")
    second = replace(first, title="Нет режима отправки", description="Нужно определить момент отправки.")
    reviewer = UnavailableMergeReviewer()
    warnings: list[str] = []
    findings = ReviewEngine(reviewer).consolidate([first, second], warnings)
    assert reviewer.calls["MergeResponse"] == 2
    assert len(findings) == 2
    assert warnings == ["Семантическое слияние выполнено частично. Точные дубли удалены кодом."]


def test_forced_pairs_merge_without_shared_blocks() -> None:
    first = _finding("B038", "Раздел тестирования не заполнен", "Согласовать объём тестирования.", 1)
    second = _finding("B045", "Чек-лист не заполнен", "Зафиксировать результаты проверок.", 2)
    assert len(deduplicate((first, second))) == 2
    (merged,) = deduplicate((first, second), forced_pairs=((1, 2),))
    assert len(merged.evidence) == 2
    assert first.title in merged.description and second.title in merged.description
    assert first.recommendation in merged.recommendation and second.recommendation in merged.recommendation


def test_governance_card_lists_each_source_gap_once(tmp_path: Path) -> None:
    source = write_rule_fixture(tmp_path / "input.md")
    report = ReviewEngine(ScriptedReviewer()).run(load_document(source), **PASSPORT)
    tests = _finding("B038", "Раздел тестирования оставлен на заполнение", "Согласовать объём тестирования.", 1)
    version = _finding("B062", "Нет версии и даты", "Зафиксировать версию и дату.", 2)
    checklist = _finding("B062", "Нет отметок чек-листа", "Заполнить чек-лист.", 2)
    checklist = replace(checklist, evidence=(replace(checklist.evidence[0], start_col=55, end_col=73),))
    header = _finding("B063", "Шапка таблицы", "—", 3)
    summary = replace(
        version, title="Служебные разделы неполны", description="Версия и дата не указаны.",
        recommendation="Снова записать версию и дату.",
        evidence=tests.evidence + version.evidence + checklist.evidence + header.evidence,
    )
    pairs = ((1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4))
    (card,) = deduplicate((summary, tests, version, checklist), forced_pairs=pairs)
    result = replace(report, findings=(card,))
    markdown = render_markdown(result)
    assert (
        "- Раздел тестирования оставлен на заполнение — Описание.\n"
        "- Нет версии и даты — Описание.\n"
        "- Нет отметок чек-листа — Описание.\n\n"
        "**Исправление.**\n\n"
        "- Согласовать объём тестирования.\n"
        "- Зафиксировать версию и дату.\n"
        "- Заполнить чек-лист."
    ) in markdown
    assert "Снова записать" not in markdown
    (exported,) = json.loads(render_json(result))["findings"]
    assert exported["description"].splitlines() == card.description.splitlines()
    assert exported["recommendation"].splitlines() == card.recommendation.splitlines()
    assert len(exported["evidence"]) == 4


def test_merge_keeps_only_distinct_additional_recommendations() -> None:
    base = _finding("B001", "Порог", "Разделить чтение и запись настройки.", 1)
    variants = [
        "Разделить чтение и запись настройки и указать маршрут.",
        "Описать отдельный контракт чтения без изменения состояния.",
        "Указать статус ответа для первой записи.",
        "Добавить пример ответа для ошибки сервера.",
    ]
    group = [base, *(replace(base, recommendation=text) for text in variants)]
    (merged,) = deduplicate(group)
    assert merged.recommendation == base.recommendation
    assert merged.additional_recommendations == (variants[1], variants[2])


def test_markdown_keeps_inline_code_and_escapes_the_rest(tmp_path: Path) -> None:
    source = write_rule_fixture(tmp_path / "input.md")
    report = ReviewEngine(ScriptedReviewer()).run(load_document(source), **PASSPORT)
    finding = replace(report.findings[0], description="Передать `amount=50000` и *не* менять `a|b`.")
    rendered = render_markdown(replace(report, findings=(finding,)))
    assert "Передать `amount=50000` и \\*не\\* менять `a|b`." in rendered


def test_concurrency_setting_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_offline(monkeypatch, tmp_path)
    assert Settings.from_env().concurrency == 3
    monkeypatch.setenv("REQVALIDATOR_CONCURRENCY", "1")
    assert Settings.from_env().concurrency == 1
    monkeypatch.setenv("REQVALIDATOR_CONCURRENCY", "9")
    with pytest.raises(ConfigurationError):
        Settings.from_env()
