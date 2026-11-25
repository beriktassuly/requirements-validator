from dataclasses import replace
from pathlib import Path

from reqvalidator.document import load_document
from reqvalidator.ground import deduplicate, locate_quote
from reqvalidator.model import Anchor, Finding
from reqvalidator.segment import segment_document


def test_grounding_keeps_source_whitespace_and_rejects_inventions(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_text("## Функциональные требования\n* **Ф-1.** Порог хранится\n  в тенге.\n", encoding="utf-8")
    document = load_document(path)
    block = next(block for block in segment_document(document) if block.source_id == "Ф-1")

    assert locate_quote(document, block, "в тенге") == Anchor(block.id, "в тенге", 3, 3, 3, 9, None, None)
    assert locate_quote(document, block, "Порог   хранится в тенге.") == Anchor(
        block.id, "Порог хранится\n  в тенге.", 2, 3, 12, 10, None, None
    )
    assert locate_quote(document, block, "в долларах") is None
    assert locate_quote(document, block, "порог хранится") is None


def test_repeated_quote_without_a_unique_anchor_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_text("## Функциональные требования\n* **Ф-1.** Порог или порог, значение «порог».\n", encoding="utf-8")
    document = load_document(path)
    block = next(block for block in segment_document(document) if block.source_id == "Ф-1")

    assert locate_quote(document, block, "порог") is None


def _anchor(identifier: str, line: int, text: str) -> Anchor:
    return Anchor(identifier, text, line, line, 1, len(text), None, None)


def test_duplicate_from_multiple_passes_preserves_all_evidence_and_highest_severity() -> None:
    first = _anchor("B001", 1, "Сделать быстро.")
    second = _anchor("B002", 3, "Дедлайн не установлен.")
    local = Finding(
        criterion="verifiability", severity="minor", title="Срок не измерим",
        description="Время операции не задано.", recommendation="Задать длительность.",
        scenario=None, requirement_ids=("B001",), evidence=(first,), scopes=("local",),
    )
    global_finding = replace(
        local, severity="major", title="Нет проверяемого времени",
        requirement_ids=("B002",), evidence=(first, second), scopes=("set",),
    )
    (merged,) = deduplicate((local, global_finding), semantic_pairs=((1, 2),))

    assert merged.id == "D-001"
    assert merged.severity == "major"
    assert set(merged.scopes) == {"local", "set"}
    assert merged.requirement_ids == ("B001", "B002")
    assert merged.evidence == (first, second)


def test_pair_of_conflicts_with_only_one_common_side_stays_separate() -> None:
    common = _anchor("B001", 1, "Отправить push.")
    left = _anchor("B002", 2, "Не отправлять при смене порога.")
    right = _anchor("B003", 3, "Не отправлять ночью.")
    first = Finding(
        criterion="contradiction", severity="major", title="Смена порога конфликтует с отправкой",
        description="Условия отправки расходятся после изменения порога.",
        recommendation="Уточнить приоритет.", scenario="Порог меняется после покупки.",
        requirement_ids=("B001", "B002"), evidence=(common, left), scopes=("set",),
    )
    second = replace(
        first, title="Ночное ограничение конфликтует с отправкой",
        description="Правила расходятся при ночной покупке.", scenario="Покупка ночью.",
        requirement_ids=("B001", "B003"), evidence=(common, right),
    )

    assert len(deduplicate((first, second), semantic_pairs=((1, 2),))) == 2


def test_merge_keeps_distinct_context_and_removes_contained_citations() -> None:
    left = _anchor("B001", 1, "GET изменяет настройки.")
    right = _anchor("B002", 2, "Сохранить новый порог.")
    fragment = replace(left, quote="GET", end_col=3)
    extra = _anchor("B003", 3, "Сумма обязательна.")
    first = Finding(
        "contradiction", "critical", "Запись через GET", "GET изменяет настройки.",
        "Разделить чтение и запись.", "Клиент сохраняет настройки.",
        ("B001", "B002"), (fragment, right), ("set",),
    )
    second = replace(
        first, title="Описание метода противоречит записи", evidence=(left, right, extra),
        requirement_ids=("B001", "B002", "B003"), criterion="ambiguity",
        recommendation="Описать отдельный контракт сохранения.",
    )
    (merged,) = deduplicate((first, second), semantic_pairs=((1, 2),))
    assert merged.evidence == (left, right, extra)
    assert merged.requirement_ids == ("B001", "B002", "B003")
    assert merged.severity == "critical"
    assert merged.additional_recommendations == ("Описать отдельный контракт сохранения.",)
