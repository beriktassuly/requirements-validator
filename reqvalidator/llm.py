"""Structured LLM calls. A reviewer can be substituted without network access."""

from __future__ import annotations

from contextlib import nullcontext
import json
from time import monotonic, sleep
from types import TracebackType
from typing import Any, Protocol, TypeVar

from httpx2 import Request, Response
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    ContentFilterFinishReasonError,
    DefaultHttpxClient,
    LengthFinishReasonError,
    OpenAI,
    OpenAIError,
)
from pydantic import BaseModel, ValidationError

from .config import ReasoningEffort, Settings
from .timing import RunTimings

T = TypeVar("T", bound=BaseModel)


class Reviewer(Protocol):
    def ask(self, system: str, prompt: str, schema: type[T]) -> T:
        """Return a locally validated instance of the requested schema."""
        ...


class ReviewError(RuntimeError):
    """A review could not be completed by the configured provider."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class _InvalidResult(ValueError):
    """The model may be able to regenerate this output."""


class _MeasuredHttpClient(DefaultHttpxClient):
    def __init__(self, timings: RunTimings, **options: Any) -> None:
        super().__init__(**options)
        self.timings = timings
        self._last_attempt: tuple[float, int | None] | None = None

    def send(self, request: Request, **kwargs: Any) -> Response:
        previous = self._last_attempt if request.headers.get("x-stainless-retry-count", "0") != "0" else None
        started = monotonic()
        status = None
        try:
            response = super().send(request, **kwargs)
            status = response.status_code
        finally:
            ended = monotonic()
            self._last_attempt = (ended, status)
            self.timings.sdk_attempt(
                ended - started,
                retry_gap=started - previous[0] if previous else None,
                previous_status=previous[1] if previous else None,
            )
        if response.is_success and response.is_stream_consumed:
            # The buffered JSON may contain usage even when SDK/schema parsing fails.
            try:
                body = response.json()
            except (ValueError, RuntimeError):
                pass
            else:
                if isinstance(body, dict) and isinstance(usage := body.get("usage"), dict):
                    self.timings.add_usage(usage)
        return response


class OpenAIReviewer:
    def __init__(self, settings: Settings, *, timings: RunTimings | None = None) -> None:
        self.settings = settings
        self.timings = timings
        self._next_request_at = 0.0
        base_url = "https://api.openai.com/v1" if settings.provider == "openai" else settings.base_url
        try:
            options: dict[str, Any] = {}
            if timings is not None:
                options["http_client"] = _MeasuredHttpClient(timings, base_url=base_url, timeout=settings.timeout)
            self._client = OpenAI(
                api_key=settings.api_key,
                base_url=base_url,
                timeout=settings.timeout,
                max_retries=2,
                **options,
            )
        except (OpenAIError, ValueError):
            raise ReviewError("Не удалось настроить клиент LLM. Проверьте ключ и адрес API.", fatal=True) from None

    def __enter__(self) -> OpenAIReviewer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def ask(
        self, system: str, prompt: str, schema: type[T], *,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> T:
        request = self._openai_responses if self.settings.provider == "openai" else self._compatible_chat
        effort = reasoning_effort if reasoning_effort is not None else self.settings.reasoning_effort
        attempts = self.settings.validation_retries + 1
        reason = ""
        timings = self.timings
        with timings.model_call(measured_transport=True) if timings is not None else nullcontext():
            for attempt in range(attempts):
                with timings.retry("format") if timings is not None and attempt else nullcontext():
                    instructions = system
                    if attempt:
                        instructions += (
                            "\n\nПовтори ответ с нуля. Предыдущий ответ не прошёл проверку формата. "
                            "Верни ровно один полный JSON-объект по схеме, без вводного текста."
                        )
                    delay = self._next_request_at - monotonic()
                    if delay > 0:
                        if timings is None:
                            sleep(delay)
                        else:
                            sleep_started = monotonic()
                            sleep(delay)
                            timings.limiter_wait_seconds += monotonic() - sleep_started
                    self._next_request_at = monotonic() + self.settings.min_request_interval
                    try:
                        return request(instructions, prompt, schema, effort)
                    except _InvalidResult as error:
                        reason = str(error)
                    except LengthFinishReasonError:
                        reason = "ответ оборван по лимиту токенов"
                    except (ValidationError, APIResponseValidationError):
                        reason = "структура ответа не соответствует схеме"
                    except ContentFilterFinishReasonError:
                        raise ReviewError("Провайдер остановил ответ фильтром содержимого.") from None
                    except APITimeoutError:
                        raise ReviewError("Истекло время ожидания LLM. Проверьте REQVALIDATOR_TIMEOUT.", fatal=True) from None
                    except APIConnectionError:
                        raise ReviewError("Не удалось подключиться к LLM. Проверьте сеть и адрес API.", fatal=True) from None
                    except APIStatusError as error:
                        raise ReviewError(_status_message(error.status_code), fatal=True) from None
                    except APIError:
                        raise ReviewError("Провайдер LLM вернул необрабатываемую ошибку.", fatal=True) from None
        raise ReviewError(f"Ответ LLM не принят после {attempts} попыток: {reason}.")

    def _openai_responses(
        self, system: str, prompt: str, schema: type[T], effort: ReasoningEffort | None,
    ) -> T:
        options: dict[str, Any] = {}
        if effort is not None:
            options["reasoning"] = {"effort": effort}
        if self.settings.max_output_tokens is not None:
            options["max_output_tokens"] = self.settings.max_output_tokens
        if self.settings.temperature is not None:
            options["temperature"] = self.settings.temperature

        response = self._client.responses.parse(
            model=self.settings.model,
            instructions=system,
            input=prompt,
            text_format=schema,
            store=False,
            **options,
        )
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", None)
            if reason == "content_filter":
                raise ReviewError("OpenAI остановил ответ фильтром содержимого.")
            if reason == "max_output_tokens":
                raise _InvalidResult("ответ неполный, проверьте лимит REQVALIDATOR_MAX_OUTPUT_TOKENS")
            raise _InvalidResult("OpenAI вернул неполный ответ")
        if response.status not in (None, "completed"):
            raise ReviewError("OpenAI не завершил запрос.")
        if any(
            item.type == "refusal"
            for message in response.output if message.type == "message"
            for item in message.content
        ):
            raise ReviewError("OpenAI отказался формировать ответ.")
        if response.output_parsed is None:
            raise _InvalidResult("в ответе отсутствует структурированный результат")
        return response.output_parsed

    def _compatible_chat(
        self, system: str, prompt: str, schema: type[T], effort: ReasoningEffort | None,
    ) -> T:
        wire_format = self.settings.compatible_format
        if wire_format != "json_schema":
            schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
            system = f"{system}\n\nВерни только JSON-объект по схеме:\n{schema_json}"
        options: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        for parameter, option in (
            ("reasoning_effort", effort),
            ("max_completion_tokens", self.settings.max_output_tokens),
            ("temperature", self.settings.temperature),
            ("seed", self.settings.seed),
        ):
            if option is not None:
                options[parameter] = option
        if wire_format == "json_schema":
            response = self._client.chat.completions.parse(response_format=schema, **options)
        else:
            if wire_format == "json_object":
                options["response_format"] = {"type": "json_object"}
            response = self._client.chat.completions.create(**options)

        if not response.choices:
            raise _InvalidResult("провайдер не вернул ни одного варианта ответа")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise _InvalidResult("ответ оборван по лимиту токенов")
        if choice.finish_reason == "content_filter" or getattr(choice.message, "refusal", None):
            raise ReviewError("Провайдер остановил ответ или отказался его формировать.")
        if not choice.message.content:
            raise _InvalidResult("в ответе отсутствует текст JSON")
        return schema.model_validate_json(choice.message.content, strict=True)


def _status_message(status: int) -> str:
    if status in (401, 403):
        return "Провайдер отклонил доступ. Проверьте ключ и права на модель."
    if status == 404:
        return "Провайдер не нашёл модель или маршрут API. Проверьте модель и базовый адрес."
    if status == 413:
        return "Провайдер отклонил размер запроса. Проверьте окно контекста выбранной модели."
    if status == 429:
        return (
            "Провайдер исчерпал квоту или лимит запросов. Проверьте тариф. "
            "Для лимита частоты можно задать REQVALIDATOR_MIN_REQUEST_INTERVAL."
        )
    if status in (400, 422):
        return (
            f"Провайдер отклонил запрос (HTTP {status}). Проверьте модель, схему и дополнительные параметры. "
            "Для совместимого API попробуйте REQVALIDATOR_COMPATIBLE_FORMAT=prompt."
        )
    return f"Запрос к LLM завершился ошибкой HTTP {status}."
