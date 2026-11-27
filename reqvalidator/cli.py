"""Command-line adapter for the review engine."""

import argparse
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict
from functools import partial
from itertools import combinations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import VERSION_TAG
from .config import ConfigurationError, Settings
from .document import Document, DocumentError, load_document
from .llm import OpenAIReviewer, Reviewer, ReviewError
from .report import render_json, render_markdown
from .review import ReviewEngine, ReviewLimitError
from .timing import ProgressObserver, RunTimings


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


def _check_paths(source: Path, output: Path, sidecar: Path | None, debug_proposals: Path | None) -> None:
    if output.suffix.casefold() != ".md":
        raise ValueError("Путь отчёта должен оканчиваться на .md.")
    json_paths = tuple(path for path in (sidecar, debug_proposals) if path is not None)
    if any(path.suffix.casefold() != ".json" for path in json_paths):
        raise ValueError("Путь JSON должен оканчиваться на .json.")
    if any(_same_path(left, right) for left, right in combinations((source, output, *json_paths), 2)):
        raise ValueError("Входной файл, отчёт и выходные JSON должны иметь разные пути.")
    for target in (output, *json_paths):
        if target.is_dir():
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
    progress_observer: ProgressObserver | None = None,
    source_observer: Callable[[Document], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="reqvalidator", description="Проверка качества требований с русским отчётом.",
    )
    parser.add_argument("input", type=Path, help="Документ PDF, Markdown, DOCX или TXT")
    parser.add_argument("output", type=Path, help="Путь к отчёту .md")
    parser.add_argument("--json-output", type=Path, help="Дополнительный JSON для eval")
    parser.add_argument("--debug-proposals", type=Path, help="JSON с подтверждёнными находками до слияния")
    parser.add_argument("--env-file", type=Path, help="Путь к отдельному .env")
    parser.add_argument("--progress", action="store_const", const="1", help="Показывать ход проверки в stderr")
    parser.add_argument("--quiet", action="store_const", const="1", help="Отключить сообщения о ходе проверки")
    parser.add_argument("--timings", action="store_const", const="1", help="Вывести замеры в stderr и JSON")
    parser.add_argument("--merge-reasoning-effort", help="Уровень reasoning для проходов слияния")
    parser.add_argument("--concurrency", metavar="ЧИСЛО", help="Одновременных запросов к модели (1–8)")
    parser.add_argument("--reviewers", metavar="qa[,api]", help="Роли дополнительных ревьюеров через запятую")
    parser.add_argument("--reviewer-max-findings", metavar="ЧИСЛО", help="Лимит находок за проход роли (1–30)")
    parser.add_argument("--external-docs", action="store_const", const="1", help="Сверять внешние контракты с документацией")
    parser.add_argument("--version", action="version", version=_revision())
    args = parser.parse_args(argv)
    overrides = {
        f"REQVALIDATOR_{option.upper()}": value
        for option in (
            "progress", "quiet", "timings", "merge_reasoning_effort", "concurrency",
            "reviewers", "reviewer_max_findings", "external_docs",
        )
        if (value := getattr(args, option)) is not None
    }
    timings = None
    show_timings = False
    stderr = partial(print, file=sys.stderr, flush=True)
    try:
        try:
            _check_paths(args.input, args.output, args.json_output, args.debug_proposals)
            configuration: Settings | ConfigurationError
            try:
                configuration = Settings.from_env(env_file=args.env_file, overrides=overrides or None)
            except ConfigurationError as error:
                configuration = error
            else:
                progress = configuration.progress and not configuration.quiet
                show_timings = configuration.timings
                if progress or show_timings or progress_observer is not None:
                    timings = RunTimings(progress=stderr if progress else None, observer=progress_observer)
            with timings.stage("extract") if timings is not None else nullcontext():
                document = load_document(args.input)
            # Preserve precedence of document errors over configuration errors.
            if isinstance(configuration, ConfigurationError):
                raise configuration
            settings = configuration
            if source_observer is not None:
                source_observer(document)
        except (DocumentError, ConfigurationError, OSError, ValueError) as error:
            if progress_observer is not None:
                if isinstance(error, DocumentError):
                    problem = "document"
                    if str(error) in {"Входной файл пуст.", "В документе нет доступного текста для проверки."}:
                        problem = "empty"
                else:
                    problem = "configuration" if isinstance(error, ConfigurationError) else "input"
                progress_observer({"kind": "problem", "problem": problem})
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
                engine = ReviewEngine(
                    active,
                    max_input_chars=settings.max_input_chars,
                    local_batch_size=settings.local_batch_size,
                    timings=timings,
                    merge_reasoning_effort=settings.merge_reasoning_effort,
                    concurrency=settings.concurrency,
                    reviewers=settings.reviewers,
                    reviewer_max_findings=settings.reviewer_max_findings,
                    external_docs=settings.external_docs,
                    context7_api_key=settings.context7_api_key,
                )
                report = engine.run(
                    document, model=settings.model, provider=settings.provider, version=_revision(),
                )
        except ReviewLimitError as error:
            if progress_observer is not None:
                progress_observer({"kind": "problem", "problem": "limit"})
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
                if args.debug_proposals is not None:
                    _write_text(args.debug_proposals, json.dumps(
                        [asdict(finding) for finding in engine.last_pre_merge], ensure_ascii=False, indent=2,
                    ) + "\n")
        except OSError as error:
            print(f"Не удалось записать выходные файлы. {error}", file=sys.stderr)
            return 4
        print(f"Отчёт сохранён: {args.output}")
        if args.json_output is not None:
            print(f"JSON сохранён: {args.json_output}")
        if not report.coverage_complete:
            print("Покрытие неполное. Подробнее в разделе «Диагностика».", file=sys.stderr)
        if report.failed_reviewers:
            print(
                f"Не завершили проверку дополнительные ревьюеры: {', '.join(report.failed_reviewers)}.",
                file=sys.stderr,
            )
        return 3 if report.failed_stages else 0
    finally:
        if progress_observer is not None and timings is not None:
            progress_observer({"kind": "timings", "timings": timings.as_dict()})
        if show_timings and timings is not None:
            for line in timings.summary_lines():
                stderr(line)
