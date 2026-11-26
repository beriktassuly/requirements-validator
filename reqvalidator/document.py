"""Extract a single, addressable text representation of each input document."""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile


class DocumentError(Exception):
    """The input could not be read as a supported requirements document."""


@dataclass(frozen=True, slots=True)
class SourceLine:
    number: int
    text: str
    page: int | None = None
    heading_level: int | None = None
    table_row: bool = False


@dataclass(frozen=True, slots=True)
class Document:
    name: str
    format: str
    sha256: str
    lines: tuple[SourceLine, ...]
    warnings: tuple[str, ...] = ()
    page_count: int | None = None


def load_document(path: Path) -> Document:
    path = Path(path)
    file_format = path.suffix.lower().removeprefix(".")
    if file_format not in {"pdf", "docx", "md", "txt"}:
        raise DocumentError("Поддерживаемые форматы: PDF, DOCX, MD и TXT.")

    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise DocumentError(f"Входной файл не найден: {path}") from exc
    except OSError as exc:
        raise DocumentError(f"Не удалось прочитать входной файл: {path}") from exc
    if not data:
        raise DocumentError("Входной файл пуст.")

    warnings: tuple[str, ...] = ()
    page_count: int | None = None
    if file_format == "pdf":
        lines, warnings, page_count = _read_pdf(data)
    elif file_format == "docx":
        lines = _read_docx(data)
    else:
        try:
            plain_text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("Для MD и TXT требуется кодировка UTF-8.") from exc
        if "\x00" in plain_text:
            raise DocumentError("Текстовый файл содержит нулевые байты. Проверьте формат и кодировку.")
        lines = [SourceLine(number, line) for number, line in enumerate(plain_text.splitlines(), 1)]

    if not any(line.text.strip() for line in lines):
        raise DocumentError("В документе нет доступного текста для проверки.")

    return Document(
        name=path.name,
        format=file_format,
        sha256=hashlib.sha256(data).hexdigest(),
        lines=tuple(lines),
        warnings=warnings,
        page_count=page_count,
    )


def require_report_document(document: Document, report: Mapping[str, object]) -> None:
    source = report.get("document")
    if not isinstance(source, dict) or source.get("sha256") != document.sha256:
        raise ValueError("SHA-256 в отчёте отличается от входного файла. Передайте точный файл прогона.")


def _read_pdf(data: bytes) -> tuple[list[SourceLine], tuple[str, ...], int]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("PDF защищён паролем. Сохраните копию, доступную для извлечения текста.")
        page_count = len(reader.pages)
    except (PyPdfError, OSError, ValueError, TypeError, KeyError, NotImplementedError) as exc:
        raise DocumentError("Не удалось открыть PDF. Проверьте целостность и защиту файла.") from exc

    if page_count == 0:
        raise DocumentError("В PDF нет страниц.")

    lines: list[SourceLine] = []
    missing_pages: list[int] = []
    for page_number in range(1, page_count + 1):
        try:
            page = reader.pages[page_number - 1]
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except (PyPdfError, ValueError, TypeError, KeyError, IndexError, NotImplementedError):
                page_text = ""
            if not page_text.strip():
                page_text = page.extract_text() or ""
        except (PyPdfError, OSError, ValueError, TypeError, KeyError, IndexError, NotImplementedError):
            page_text = ""

        if not page_text.strip():
            missing_pages.append(page_number)
            continue
        for line in page_text.splitlines():
            lines.append(SourceLine(len(lines) + 1, line, page_number))

    if not lines:
        raise DocumentError("В PDF не найден текстовый слой. Передайте PDF после OCR или исходный текст.")
    warnings: tuple[str, ...] = ()
    if missing_pages:
        pages = ", ".join(map(str, missing_pages))
        warnings = (f"Текст не извлечён со страниц PDF: {pages}. Покрытие документа может быть неполным.",)
    return lines, warnings, page_count


def _read_docx(data: bytes) -> list[SourceLine]:
    from docx import Document as WordDocument
    from docx.opc.exceptions import PackageNotFoundError
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        word = WordDocument(io.BytesIO(data))
        lines: list[SourceLine] = []
        for item in word.iter_inner_content():
            if isinstance(item, Paragraph):
                style_name = item.style.name if item.style is not None else ""
                heading = re.fullmatch(r"(?:Heading|Заголовок) ([1-9])", style_name or "", re.IGNORECASE)
                level = min(6, int(heading[1])) if heading else None
                for position, line in enumerate(item.text.splitlines() or [""]):
                    lines.append(SourceLine(len(lines) + 1, line, heading_level=level if position == 0 else None))
                if item.text.strip():
                    lines.append(SourceLine(len(lines) + 1, ""))
            elif isinstance(item, Table):
                for row in item.rows:
                    cells = [" ".join(cell.text.splitlines()) for cell in row.cells]
                    lines.append(SourceLine(len(lines) + 1, " | ".join(cells), table_row=True))
                lines.append(SourceLine(len(lines) + 1, ""))
        return lines
    except (PackageNotFoundError, BadZipFile, OSError, SyntaxError, ValueError, KeyError) as exc:
        raise DocumentError("Не удалось прочитать DOCX. Проверьте, что файл открывается как документ Word.") from exc
