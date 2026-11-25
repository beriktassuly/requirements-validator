"""Build a text-layer PDF from an existing UTF-8 requirements document."""

import argparse
import os
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.pdfgen.canvas import Canvas


def _font_candidates() -> tuple[Path, ...]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"),
        windows / "consola.ttf",
        windows / "cour.ttf",
        Path("/System/Library/Fonts/Supplemental/Courier New.ttf"),
    )


def _choose_font(text: str, font_path: Path | None) -> str:
    required = {ord(character) for character in text if not character.isspace()}
    if font_path is None and text.isascii():
        return "Courier"

    candidates = (font_path,) if font_path is not None else _font_candidates()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            font = TTFont("FixtureMono", str(path))
        except (OSError, TTFError) as exc:
            if font_path is not None:
                raise ValueError(f"Не удалось открыть TTF-шрифт {path}.") from exc
            continue
        if required.issubset(font.face.charToGlyph):
            pdfmetrics.registerFont(font)
            return font.fontName

    raise ValueError("Укажите --font с путём к TTF-шрифту, содержащему все символы документа.")


def write_fixture_pdf(
    source: Path, destination: Path, *, font_path: Path | None = None, lines_per_page: int = 54
) -> None:
    if lines_per_page < 1:
        raise ValueError("Число строк на странице должно быть положительным.")

    text = source.read_text(encoding="utf-8-sig")
    lines = [line.expandtabs(4) for line in text.splitlines()]
    if not any(line.strip() for line in lines):
        raise ValueError("Источник PDF не содержит текста.")

    font = _choose_font(text, font_path)
    size, leading, margin = 10, 13, 36
    max_line_width = max(pdfmetrics.stringWidth(line, font, size) for line in lines)
    width = max(595, max_line_width + 2 * margin + size)
    height = max(842, lines_per_page * leading + 2 * margin)

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(destination), pagesize=(width, height), invariant=1)
    canvas.setTitle(source.stem)
    for offset in range(0, len(lines), lines_per_page):
        page = canvas.beginText(margin, height - margin)
        page.setFont(font, size, leading=leading)
        for line in lines[offset : offset + lines_per_page]:
            page.textLine(line)
        canvas.drawText(page)
        canvas.showPage()
    canvas.save()


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать тестовый PDF с текстовым слоем из UTF-8 MD/TXT.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--font", type=Path, help="путь к моноширинному TTF со всеми символами источника")
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve():
        parser.error("пути источника и PDF должны различаться")
    try:
        write_fixture_pdf(args.source, args.destination, font_path=args.font)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Фикстура PDF сохранена: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
