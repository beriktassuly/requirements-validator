"""Open the saved submission and replay only timed lines present in a real log."""

from datetime import datetime
from functools import cached_property
from hashlib import file_digest
import json
from pathlib import Path
import re

from reqvalidator.document import DocumentError, load_document
from reqvalidator.timing import _STAGE_LABELS

from .worker import source_payload


_TIMED_LINE = re.compile(r"^\[\s*(\d+(?:\.\d+)?)\s+с\]\s+(.+)$")
_LABELS = "|".join(re.escape(label) for label in _STAGE_LABELS.values())
_SAFE_LOG_LINE = re.compile(
    rf"(?:Начато: (?:{_LABELS})\."
    rf"|(?:Завершено|Прервано): (?:{_LABELS}) \(\d+(?:\.\d+)? с\)\."
    r"|Обработано \d+ из \d+ пакетов, осталось ~\d+(?:\.\d+)? мин\.)"
)


class Demo:
    def __init__(self, root: Path) -> None:
        self.directory = root / "web-demo"
        self.submission = root / "reports" / "submission"

    def download(self, extension: str) -> Path:
        return self.submission.with_suffix(f".{extension}")

    @cached_property
    def source(self) -> dict[str, object] | None:
        path = self.directory / "requirements.pdf"
        if not path.is_file():
            return None
        try:
            with path.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
            index = json.loads((self.directory / "source-lines.json").read_text(encoding="utf-8-sig"))
            if isinstance(index, dict) and index.get("sha256") == digest and isinstance(index.get("lines"), list):
                return {"sha256": digest, "lines": index["lines"]}
        except (OSError, ValueError):
            pass
        try:
            return source_payload(load_document(path))
        except (DocumentError, OSError, ValueError):
            return None

    @cached_property
    def submission_report(self) -> dict[str, object]:
        return json.loads(self.download("json").read_text(encoding="utf-8-sig"))

    def report(self, source: dict[str, object] | None) -> dict[str, object]:
        report = self.submission_report
        return {"report": report, "source": source, "partial": report.get("coverage_complete") is False}

    @cached_property
    def replay(self) -> dict[str, object] | None:
        log_file = self.directory / "progress.log"
        try:
            reviewed_at = self.submission_report["reviewed_at"]
            if not isinstance(reviewed_at, str) or datetime.fromisoformat(reviewed_at).utcoffset() is None:
                return None
            log = log_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, TypeError, KeyError, ValueError):
            return None
        events = []
        for line in log.splitlines():
            match = _TIMED_LINE.fullmatch(line)
            if match and _SAFE_LOG_LINE.fullmatch(match[2]):
                events.append({"elapsed": float(match[1]), "text": match[2]})
        return {"reviewedAt": reviewed_at, "events": events} if events else None
