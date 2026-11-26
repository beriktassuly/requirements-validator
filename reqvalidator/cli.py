"""Command-line adapter for the review engine."""

import argparse
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from functools import partial
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import VERSION_TAG
from .config import ConfigurationError, Settings
from .document import DocumentError, load_document
from .llm import OpenAIReviewer, Reviewer, ReviewError
from .report import render_json, render_markdown
from .review import ReviewEngine, ReviewLimitError
from .timing import RunTimings


def _revision() -> str:
    configured = os.environ.get("REQVALIDATOR_VERSION", "").strip()
    if configured:
        return configured
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True, check=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            commit = result.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                return commit
    return VERSION_TAG


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


def _check_paths(source: Path, output: Path, sidecar: Path | None) -> None:
    if output.suffix.casefold() != ".md":
        raise ValueError("Путь отчёта должен оканчиваться на .md.")
    if sidecar is not None and sidecar.suffix.casefold() != ".json":
        raise ValueError("Путь JSON должен оканчиваться на .json.")
    if _same_path(source, output) or (
        sidecar is not None and (_same_path(source, sidecar) or _same_path(output, sidecar))
    ):
        raise ValueError("Входной файл, отчёт и JSON должны иметь разные пути.")
    for target in (output, sidecar):
        if target is not None and target.is_dir():
            raise ValueError(f"Ожидался путь к файлу, а не каталог: {target}")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=".reqvalidator-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    reviewer_factory: Callable[[Settings], Reviewer] = OpenAIReviewer,
) -> int:
    parser = argparse.ArgumentParser(
        prog="reqvalidator", description="Проверка качества требований с русским отчётом.",
    )
    parser.add_argument("input", type=Path, help="Документ PDF, Markdown, DOCX или TXT")
    parser.add_argument("output", type=Path, help="Путь к отчёту .md")
    parser.add_argument("--json-output", type=Path, help="Дополнительный JSON для eval")
    parser.add_argument("--env-file", type=Path, help="Путь к отдельному .env")
    parser.add_argument("--progress", action="store_const", const="1", help="Показывать ход проверки в stderr")
    parser.add_argument("--quiet", action="store_const", const="1", help="Отключить сообщения о ходе проверки")
    parser.add_argument("--timings", action="store_const", const="1", help="Вывести замеры в stderr и JSON")
    parser.add_argument("--merge-reasoning-effort", help="Уровень reasoning для проходов слияния")
    parser.add_argument("--reviewers", metavar="qa[,api]", help="Роли дополнительных ревьюеров через запятую")
    parser.add_argument("--reviewer-max-findings", metavar="ЧИСЛО", help="Лимит находок за проход роли (1–30)")
    parser.add_argument("--external-docs", action="store_const", const="1", help="Сверять внешние контракты с документацией")
    parser.add_argument("--version", action="version", version=_revision())
    args = parser.parse_args(argv)
    overrides = {
        f"REQVALIDATOR_{option.upper()}": value
        for option in (
            "progress", "quiet", "timings", "merge_reasoning_effort",
            "reviewers", "reviewer_max_findings", "external_docs",
        )
        if (value := getattr(args, option)) is not None
    }
    timings = None
    show_timings = False
    stderr = partial(print, file=sys.stderr, flush=True)
    try:
        try:
            _check_paths(args.input, args.output, args.json_output)
            configuration: Settings | ConfigurationError
            try:
                configuration = Settings.from_env(env_file=args.env_file, overrides=overrides or None)
            except ConfigurationError as error:
                configuration = error
            else:
                progress = configuration.progress and not configuration.quiet
                show_timings = configuration.timings
                if progress or show_timings:
                    timings = RunTimings(progress=stderr if progress else None)
            with timings.stage("extract") if timings is not None else nullcontext():
                document = load_document(args.input)
            # Preserve precedence of document errors over configuration errors.
            if isinstance(configuration, ConfigurationError):
                raise configuration
            settings = configuration
        except (DocumentError, ConfigurationError, OSError, ValueError) as error:
            print(f"Ошибка входа или конфигурации. {error}", file=sys.stderr)
            return 2

        try:
            reviewer = (
                OpenAIReviewer(settings, timings=timings)
                if timings is not None and reviewer_factory is OpenAIReviewer
                else reviewer_factory(settings)
            )
            managed = reviewer if hasattr(reviewer, "__enter__") else nullcontext(reviewer)
            with managed as active:
                report = ReviewEngine(
                    active,
                    max_input_chars=settings.max_input_chars,
                    local_batch_size=settings.local_batch_size,
                    timings=timings,
                    merge_reasoning_effort=settings.merge_reasoning_effort,
                    reviewers=settings.reviewers,
                    reviewer_max_findings=settings.reviewer_max_findings,
                    external_docs=settings.external_docs,
                    context7_api_key=settings.context7_api_key,
                ).run(
                    document, model=settings.model, provider=settings.provider, version=_revision(),
                )
        except ReviewLimitError as error:
            print(f"Вход невозможно проверить целиком. {error}", file=sys.stderr)
            return 2
        except ReviewError as error:
            print(f"Ошибка модели. {error}", file=sys.stderr)
            return 3

        try:
            with timings.stage("write") if timings is not None else nullcontext():
                _write_text(args.output, render_markdown(report))
                if args.json_output is not None:
                    snapshot = timings.as_dict() if show_timings and timings is not None else None
                    _write_text(args.json_output, render_json(report, timings=snapshot))
        except OSError as error:
            print(f"Не удалось записать выходные файлы. {error}", file=sys.stderr)
            return 4
        print(f"Отчёт сохранён: {args.output}")
        if args.json_output is not None:
            print(f"JSON сохранён: {args.json_output}")
        if not report.coverage_complete:
            print("Покрытие неполное. Подробнее в разделе «Диагностика».", file=sys.stderr)
        return 3 if report.failed_stages else 0
    finally:
        if show_timings and timings is not None:
            for line in timings.summary_lines():
                stderr(line)
