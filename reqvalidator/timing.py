"""Opt-in progress and measurements for one run."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Literal

RetryKind = Literal["format", "coverage", "validation"]

_STAGE_LABELS = {
    "extract": "извлечение текста",
    "segment": "нарезка документа",
    "local": "проверка локальных пакетов",
    "set": "проверка документа целиком (ожидание результата при параллельном запуске)",
    "set_model": "запрос общей проверки (параллельно с локальными пакетами)",
    "evidence": "сверка цитат",
    "reviewers": "дополнительные ревьюеры",
    "external_docs": "проверка внешних источников",
    "merge": "первый проход слияния",
    "merge_consolidated": "второй проход слияния",
    "clarify": "уточнение оценок",
    "write": "запись отчёта",
}


@dataclass(slots=True)
class RunTimings:
    """Counters are updated from worker threads. Stages and progress run on the main thread.

    With concurrent calls, model_wait_seconds is the sum over calls and may exceed total_seconds.
    """

    progress: Callable[[str], None] | None = None
    started_at: float = field(default_factory=monotonic, init=False)
    stages: dict[str, float] = field(default_factory=dict, init=False)
    model_calls: int = field(default=0, init=False)
    sdk_requests: int = field(default=0, init=False)
    model_wait_seconds: float = field(default=0.0, init=False)
    limiter_wait_seconds: float = field(default=0.0, init=False)
    retries: dict[str, int] = field(default_factory=lambda: {
        "format": 0, "coverage": 0, "validation": 0, "network": 0,
    }, init=False)
    retry_seconds: dict[str, float] = field(default_factory=lambda: {
        "format": 0.0, "coverage": 0.0, "validation": 0.0, "network": 0.0,
    }, init=False)
    sdk_retry_wait_seconds: float = field(default=0.0, init=False)
    sdk_retry_reasons: dict[str, int] = field(default_factory=dict, init=False)
    merge_pairs: dict[str, int] = field(default_factory=lambda: {
        "merge": 0, "merge_consolidated": 0,
    }, init=False)
    usage: dict[str, int] = field(default_factory=dict, init=False)
    _active_stages: list[tuple[str, float]] = field(default_factory=list, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(f"[{monotonic() - self.started_at:6.1f} с] {message}")

    @contextmanager
    def stage(self, stage: str) -> Iterator[float]:
        label = _STAGE_LABELS[stage]
        self._emit(f"Начато: {label}.")
        started = monotonic()
        with self._lock:
            self._active_stages.append((stage, started))
        outcome = "Завершено"
        try:
            yield started
        except BaseException:
            outcome = "Прервано"
            raise
        finally:
            duration = monotonic() - started
            with self._lock:
                self._active_stages.pop()
                self.stages[stage] = self.stages.get(stage, 0.0) + duration
            self._emit(f"{outcome}: {label} ({duration:.1f} с).")

    def record_stage(self, stage: str, duration: float) -> None:
        with self._lock:
            self.stages[stage] = self.stages.get(stage, 0.0) + duration

    def local_batch(self, completed: int, total: int, started: float) -> None:
        remaining_minutes = (monotonic() - started) * (total - completed) / completed / 60
        self._emit(f"Обработано {completed} из {total} пакетов, осталось ~{remaining_minutes:.1f} мин.")

    @contextmanager
    def model_call(self, *, measured_transport: bool) -> Iterator[None]:
        with self._lock:
            self.model_calls += 1
        started = monotonic()
        try:
            yield
        finally:
            if not measured_transport:
                elapsed = monotonic() - started
                with self._lock:
                    self.model_wait_seconds += elapsed

    @contextmanager
    def retry(self, kind: RetryKind) -> Iterator[None]:
        with self._lock:
            self.retries[kind] += 1
        started = monotonic()
        try:
            yield
        finally:
            elapsed = monotonic() - started
            with self._lock:
                self.retry_seconds[kind] += elapsed

    def add_limiter_wait(self, seconds: float) -> None:
        with self._lock:
            self.limiter_wait_seconds += seconds

    def sdk_attempt(
        self, duration: float, *, retry_gap: float | None, previous_status: int | None,
    ) -> None:
        with self._lock:
            self.sdk_requests += 1
            self.model_wait_seconds += duration
            if retry_gap is not None:
                self.retries["network"] += 1
                self.retry_seconds["network"] += retry_gap + duration
                self.sdk_retry_wait_seconds += retry_gap
                reason = str(previous_status) if previous_status is not None else "transport"
                self.sdk_retry_reasons[reason] = self.sdk_retry_reasons.get(reason, 0) + 1

    def add_usage(self, usage: dict[str, object]) -> None:
        with self._lock:
            for name, compatible_name in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = usage.get(name, usage.get(compatible_name))
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.usage[name] = self.usage.get(name, 0) + value

    def as_dict(self) -> dict[str, object]:
        with self._lock:
            now = monotonic()
            stages = dict(self.stages)
            for stage, started in self._active_stages:
                stages[stage] = stages.get(stage, 0.0) + now - started
            return {
                "total_seconds": now - self.started_at,
                "stages_seconds": stages,
                "model_calls": self.model_calls,
                "sdk_requests": self.sdk_requests,
                "model_wait_seconds": self.model_wait_seconds,
                "limiter_wait_seconds": self.limiter_wait_seconds,
                "retries": dict(self.retries),
                "retry_seconds": dict(self.retry_seconds),
                "sdk_retry_wait_seconds": self.sdk_retry_wait_seconds,
                "sdk_retry_reasons": dict(self.sdk_retry_reasons),
                "merge_pairs": dict(self.merge_pairs),
                "usage": dict(self.usage),
            }

    def summary_lines(self) -> Iterator[str]:
        with self._lock:
            total = monotonic() - self.started_at
            stages = dict(self.stages)
            calls, requests = self.model_calls, self.sdk_requests
            model_wait, limiter_wait = self.model_wait_seconds, self.limiter_wait_seconds
            retries, retry_seconds = dict(self.retries), dict(self.retry_seconds)
            reasons, sdk_wait = dict(self.sdk_retry_reasons), self.sdk_retry_wait_seconds
            pairs, usage = dict(self.merge_pairs), dict(self.usage)
        yield f"Замеры (всего {total:.1f} с)"
        for stage, seconds in stages.items():
            yield f"  {_STAGE_LABELS[stage]} — {seconds:.1f} с"
        yield (
            f"  Вызовы модели — {calls}, попытки HTTP — {requests}, "
            f"ожидание ответа — {model_wait:.1f} с, "
            f"сон ограничителя — {limiter_wait:.1f} с"
        )
        for kind, label in (
            ("format", "формат или схема"), ("coverage", "покрытие"),
            ("validation", "дополнительные условия"), ("network", "сеть и HTTP"),
        ):
            yield f"  Повторы ({label}) — {retries[kind]}, {retry_seconds[kind]:.1f} с"
        if reasons:
            text = ", ".join(
                f"{'транспорт' if reason == 'transport' else 'HTTP ' + reason} — {count}"
                for reason, count in reasons.items()
            )
            yield f"  Причины повторов SDK — {text}. Паузы SDK — {sdk_wait:.1f} с"
        yield (
            f"  Пары слияния — первый проход {pairs['merge']}, "
            f"второй проход {pairs['merge_consolidated']}"
        )
        if usage:
            totals = ", ".join(
                f"{label} — {usage[key]}"
                for key, label in (
                    ("input_tokens", "вход"), ("output_tokens", "выход"), ("total_tokens", "всего"),
                )
                if key in usage
            )
            yield f"  Токены из usage — {totals}"
