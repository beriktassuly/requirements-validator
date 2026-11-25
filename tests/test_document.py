import json
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas

from reqvalidator.document import DocumentError, load_document
from reqvalidator.segment import segment_document
from tests.generate_pdf import write_fixture_pdf


FIXTURES = Path(__file__).with_name("fixtures")


def test_markdown_preserves_source_lines_tables_and_json() -> None:
    source = FIXTURES / "requirements.md"
    document = load_document(source)
    raw = source.read_text(encoding="utf-8-sig").splitlines()

    assert document.format == "md"
    assert [line.text for line in document.lines] == raw
    assert [line.number for line in document.lines] == list(range(1, len(raw) + 1))
    assert all(line.page is None for line in document.lines)
    assert document.lines[63].text == (
        "| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 по Алматы "
        "| Февральский счётчик вырос на 4 000 ₸ |"
    )
    assert json.loads(document.lines[170].text) == {"result": "ERROR", "message": "Проверьте сумму"}


def test_pdf_preserves_pages_table_row_and_json(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text(
        "# Acceptance\n"
        "| State | Result |\n"
        "| ready | allowed |\n"
        "## API example\n"
        "~~~json\n"
        '{"result":"ERROR"}\n'
        "~~~\n",
        encoding="utf-8",
    )
    pdf = tmp_path / "source.pdf"
    write_fixture_pdf(source, pdf, lines_per_page=4)
    document = load_document(pdf)

    assert document.format == "pdf"
    assert not document.warnings
    assert [line.number for line in document.lines] == list(range(1, len(document.lines) + 1))
    assert {line.page for line in document.lines} == {1, 2}
    table_row = next(line for line in document.lines if "ready" in line.text)
    assert table_row.page == 1
    assert [cell.strip() for cell in table_row.text.strip().strip("|").split("|")] == ["ready", "allowed"]
    example = next(line for line in document.lines if '{"result"' in line.text)
    assert example.page == 2
    assert json.loads(example.text) == {"result": "ERROR"}


def test_pdf_without_text_fails_with_clear_diagnostic(tmp_path: Path) -> None:
    input_path = tmp_path / "blank.pdf"
    canvas = Canvas(str(input_path))
    canvas.rect(20, 20, 100, 100)
    canvas.showPage()
    canvas.save()

    with pytest.raises(DocumentError, match="текстов"):
        load_document(input_path)


def test_organiser_requirement_snapshot() -> None:
    document = load_document(FIXTURES / "requirements.md")
    expected = json.loads((FIXTURES / "segmentation.json").read_text(encoding="utf-8"))
    blocks = segment_document(document)
    actual = [
        {
            "id": block.id,
            "source_id": block.source_id,
            "kind": block.kind,
            "start_line": block.start_line,
            "end_line": block.end_line,
            "quote": block.text,
        }
        for block in blocks
        if block.is_requirement
    ]

    assert actual == expected
    assert any(block.kind == "governance" for block in blocks)
    assert all(not block.is_requirement for block in blocks if block.kind == "governance")
    assert all(
        block.text == "\n".join(line.text for line in document.lines[block.start_line - 1 : block.end_line])
        for block in blocks
    )
