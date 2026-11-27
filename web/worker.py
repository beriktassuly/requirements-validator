"""Run the existing CLI with a private structured progress channel on stdout."""

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from threading import Lock

from reqvalidator.cli import main as cli_main
from reqvalidator.document import Document


def source_payload(document: Document) -> dict[str, object]:
    return {
        "sha256": document.sha256,
        "lines": [
            {"number": line.number, "text": line.text, "page": line.page}
            for line in document.lines
        ],
    }


def run(source_path: Path, output: Path, concurrency: str) -> int:
    channel = sys.stdout
    channel_lock = Lock()

    def observe(event: dict[str, object]) -> None:
        with channel_lock:
            print(json.dumps(event, ensure_ascii=False, allow_nan=False), file=channel, flush=True)

    def save_source(document: Document) -> None:
        (output / "source.json").write_text(
            json.dumps(source_payload(document), ensure_ascii=False), encoding="utf-8",
        )

    with redirect_stdout(sys.stderr):
        return cli_main(
            [str(source_path), str(output / "review.md"), "--json-output",
             str(output / "review.json"), "--progress", "--timings", "--concurrency", concurrency],
            progress_observer=observe,
            source_observer=save_source,
        )


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
