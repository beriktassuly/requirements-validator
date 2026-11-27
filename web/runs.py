"""In-process run registry and an isolated CLI subprocess for each upload."""

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Condition, Event, Thread
from time import monotonic
from uuid import uuid4


AVAILABLE_SECONDS = 3600
ACTIVE_STATUSES = {"queued", "running"}
_PROBLEMS = {
    "document": "Не удалось извлечь текст. Проверьте формат, кодировку или текстовый слой файла.",
    "empty": "В документе нет доступного текста для проверки.",
    "configuration": "Проверьте профиль, модель и ключ в настройках сервера.",
    "input": "Не удалось прочитать документ или подготовить файлы прогона.",
    "limit": "Документ превышает предел входа выбранного профиля.",
}
_EXIT_ERRORS = {
    2: "Анализ не начался из-за файла или настроек сервера.",
    3: "Не удалось завершить все этапы анализа. Проверьте модель, ключ, квоту и соединение.",
    4: "Не удалось записать отчёт.",
}


class RunConflict(Exception):
    """A run cannot be started in the current server state."""


def _load_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True)
class Run:
    id: str
    folder: Path
    filename: str
    status: str = "queued"
    enqueued_at: float = field(default_factory=monotonic)
    started_at: float | None = None
    ended_at: float | None = None
    stage: str | None = None
    stage_label: str | None = None
    stage_labels: dict[str, str] = field(default_factory=dict)
    active_stages: list[str] = field(default_factory=list)
    stage_history: dict[str, str] = field(default_factory=dict)
    background_set: str | None = None
    processed: int | None = None
    total: int | None = None
    remaining: float | None = None
    log: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=180))
    timings: dict[str, object] | None = None
    report_available: bool = False
    partial: bool = False
    error: str | None = None
    cancelling: bool = False
    problem: str | None = None
    revision: int = 0
    process: subprocess.Popen[str] | None = None
    worker: Thread | None = None

    def snapshot(self) -> dict[str, object]:
        started = self.started_at if self.started_at is not None else self.enqueued_at
        ended = self.ended_at if self.ended_at is not None else monotonic()
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "elapsed": round(ended - started, 1),
            "stage": self.stage,
            "stageLabel": self.stage_label,
            "activeStages": list(self.active_stages),
            "stageHistory": dict(self.stage_history),
            "backgroundSet": self.background_set,
            "processed": self.processed,
            "total": self.total,
            "remaining": self.remaining,
            "log": list(self.log),
            "timings": self.timings,
            "reportAvailable": self.report_available,
            "partial": self.partial,
            "error": self.error,
            "cancelling": self.cancelling,
        }

    def accept(self, event: dict[str, object]) -> None:
        kind = event.get("kind")
        elapsed = event.get("elapsed")
        message = event.get("text")
        stage = event.get("stage")
        state = event.get("state")
        label = event.get("label")
        if isinstance(elapsed, (int, float)) and isinstance(message, str):
            self.log.append({"elapsed": round(elapsed, 1), "text": message})
        if kind == "stage" and isinstance(stage, str) and isinstance(label, str) and isinstance(state, str):
            self.stage_labels[stage] = label
            self.stage_history[stage] = state
            if state == "running":
                self.active_stages.append(stage)
            elif self.active_stages and self.active_stages[-1] == stage:
                self.active_stages.pop()
            self.stage = self.active_stages[-1] if self.active_stages else stage
            self.stage_label = self.stage_labels[self.stage]
        elif kind == "background" and stage == "set_model" and isinstance(state, str):
            self.background_set = state
        elif kind == "batch":
            processed = event.get("processed")
            total = event.get("total")
            remaining = event.get("remaining")
            if isinstance(processed, int) and isinstance(total, int) and isinstance(remaining, (float, int)):
                self.processed = processed
                self.total = total
                self.remaining = round(remaining, 1)
        elif kind == "timings" and isinstance(value := event.get("timings"), dict):
            self.timings = value
        elif kind == "problem" and isinstance(value := event.get("problem"), str):
            self.problem = value


class Runs:
    def __init__(self, root: Path, storage: Path) -> None:
        self.root = root
        self.storage = storage
        self.condition = Condition()
        self.jobs: dict[str, Run] = {}
        self.closing = Event()
        self.janitor = Thread(target=self._cleanup_loop, daemon=True)
        self.janitor.start()

    def new_folder(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="upload-", dir=self.storage))

    def start(self, folder: Path, source: Path, concurrency: int) -> dict[str, object]:
        with self.condition:
            if self.closing.is_set() or any(job.status in ACTIVE_STATUSES for job in self.jobs.values()):
                raise RunConflict("Один анализ уже выполняется. Дождитесь его завершения или остановите его.")
            if len(self.jobs) >= 24:
                raise RunConflict("Временно заняты все места для отчётов. Повторите запуск позже.")
            job = Run(uuid4().hex, folder, source.name)
            worker = Thread(target=self._run, args=(job, source, concurrency), daemon=True)
            job.worker = worker
            self.jobs[job.id] = job
            worker.start()
            return job.snapshot()

    def _changed(self, job: Run) -> None:
        job.revision += 1
        self.condition.notify_all()

    def _run(self, job: Run, source: Path, concurrency: int) -> None:
        process = None
        exit_code = None
        try:
            with self.condition:
                if job.cancelling:
                    return
                process = subprocess.Popen(
                    [sys.executable, "-X", "utf8", "-m", "web.worker", str(source), str(job.folder), str(concurrency)],
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    encoding="utf-8", errors="replace", shell=False,
                )
                job.process = process
                job.started_at = monotonic()
                job.status = "running"
                self._changed(job)
            assert process.stdout is not None
            with process.stdout:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        with self.condition:
                            job.accept(event)
                            self._changed(job)
            exit_code = process.wait()
        except OSError:
            pass
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            shutil.rmtree(source.parent, ignore_errors=True)
            report = _load_object(job.folder / "review.json")
            if report is None:
                shutil.rmtree(job.folder, ignore_errors=True)
            with self.condition:
                job.process = None
                job.ended_at = monotonic()
                for stage in job.active_stages:
                    job.stage_history[stage] = "failed"
                job.active_stages.clear()
                if job.background_set == "running":
                    job.background_set = "failed"
                job.report_available = report is not None
                job.partial = report is not None and (report.get("coverage_complete") is False or exit_code != 0)
                if job.cancelling:
                    job.status = "cancelled"
                elif exit_code == 0 and job.report_available:
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = _PROBLEMS.get(job.problem or "") or _EXIT_ERRORS.get(exit_code)
                    if job.error is None:
                        job.error = "Не удалось выполнить анализ или получить корректный отчёт."
                job.cancelling = False
                self._changed(job)

    def cancel(self, job: Run) -> dict[str, object]:
        with self.condition:
            process = job.process
            if job.status in ACTIVE_STATUSES and not job.cancelling and (process is None or process.poll() is None):
                if process is not None:
                    process.kill()
                job.cancelling = True
                self._changed(job)
            return job.snapshot()

    def _cleanup_loop(self) -> None:
        while not self.closing.wait(30):
            with self.condition:
                expired = [
                    key for key, job in self.jobs.items()
                    if job.ended_at is not None and monotonic() - job.ended_at >= AVAILABLE_SECONDS
                ]
                for key in expired:
                    shutil.rmtree(self.jobs.pop(key).folder, ignore_errors=True)

    def lookup(self, run_id: str) -> Run | None:
        with self.condition:
            job = self.jobs.get(run_id)
            if job is not None and job.ended_at is not None and monotonic() - job.ended_at >= AVAILABLE_SECONDS:
                return None
            return job

    def close(self) -> None:
        self.closing.set()
        with self.condition:
            jobs = list(self.jobs.values())
            for job in jobs:
                self.cancel(job)
        for job in jobs:
            if job.worker is not None:
                job.worker.join(timeout=5)
        self.janitor.join(timeout=2)

    def report(self, job: Run) -> dict[str, object] | None:
        with self.condition:
            if not job.report_available:
                return None
            report = _load_object(job.folder / "review.json")
            if report is None:
                return None
            return {"report": report, "source": _load_object(job.folder / "source.json"), "partial": job.partial}

    def download(self, job: Run, extension: str) -> bytes | None:
        with self.condition:
            if not job.report_available:
                return None
            try:
                return (job.folder / f"review.{extension}").read_bytes()
            except OSError:
                return None
