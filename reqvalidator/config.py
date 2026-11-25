"""Environment configuration shared by the CLI and future adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
import os
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from dotenv import dotenv_values

Provider = Literal["openai", "compatible"]
CompatibleFormat = Literal["json_object", "json_schema", "prompt"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_PROVIDERS = {"openai", "compatible"}
_FORMATS = {"json_object", "json_schema", "prompt"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


class ConfigurationError(ValueError):
    """The agent cannot use the provided configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    provider: Provider
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    timeout: float = 90.0
    validation_retries: int = 2
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    compatible_format: CompatibleFormat = "json_object"
    temperature: float | None = None
    seed: int | None = None
    max_input_chars: int = 100_000
    local_batch_size: int = 6
    min_request_interval: float = 0.0

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        path = Path(env_file) if env_file is not None else Path(".env")
        try:
            with path.open(encoding="utf-8-sig") as stream:
                env = dotenv_values(stream=stream, interpolate=False)
        except FileNotFoundError:
            if env_file is not None:
                raise ConfigurationError("Указанный файл конфигурации не найден.") from None
            env = {}
        except (OSError, UnicodeError):
            raise ConfigurationError("Не удалось прочитать файл конфигурации UTF-8.") from None
        env.update(os.environ)

        def value(name: str, default: str = "") -> str:
            raw = env.get(name)
            return default if raw is None else raw.strip()

        provider = value("REQVALIDATOR_PROVIDER", "openai").lower()
        if provider not in _PROVIDERS:
            raise ConfigurationError("REQVALIDATOR_PROVIDER должен быть openai или compatible.")
        model = value("REQVALIDATOR_MODEL")
        if not model:
            raise ConfigurationError("Укажите модель в REQVALIDATOR_MODEL.")

        base_url = value("REQVALIDATOR_BASE_URL") or None
        api_key = value("REQVALIDATOR_API_KEY")
        if provider == "openai":
            if base_url:
                raise ConfigurationError("Для REQVALIDATOR_BASE_URL выберите провайдера compatible.")
            api_key = api_key or value("OPENAI_API_KEY")
            if not api_key:
                raise ConfigurationError("Укажите OPENAI_API_KEY для прямого OpenAI.")
        else:
            if not base_url:
                raise ConfigurationError("Для compatible укажите REQVALIDATOR_BASE_URL.")
            host = _endpoint_host(base_url)
            if not api_key and host not in {"localhost", "127.0.0.1", "::1"}:
                raise ConfigurationError("Для compatible укажите ключ в REQVALIDATOR_API_KEY.")
            api_key = api_key or "local"

        response_format = value("REQVALIDATOR_COMPATIBLE_FORMAT", "json_object").lower()
        if response_format not in _FORMATS:
            raise ConfigurationError(
                "REQVALIDATOR_COMPATIBLE_FORMAT должен быть json_object, json_schema или prompt."
            )
        effort = value("REQVALIDATOR_REASONING_EFFORT").lower() or None
        if effort is not None and effort not in _EFFORTS:
            raise ConfigurationError(
                "REQVALIDATOR_REASONING_EFFORT должен быть none, minimal, low, medium, high, xhigh или max."
            )

        timeout = _number(value("REQVALIDATOR_TIMEOUT", "90"), "REQVALIDATOR_TIMEOUT")
        if timeout is None or timeout <= 0:
            raise ConfigurationError("REQVALIDATOR_TIMEOUT должен быть положительным числом секунд.")
        retries = _integer(value("REQVALIDATOR_VALIDATION_RETRIES", "2"), "REQVALIDATOR_VALIDATION_RETRIES", 0, 5)
        max_input = _integer(value("REQVALIDATOR_MAX_INPUT_CHARS", "100000"), "REQVALIDATOR_MAX_INPUT_CHARS", 1000)
        batch_size = _integer(value("REQVALIDATOR_LOCAL_BATCH_SIZE", "6"), "REQVALIDATOR_LOCAL_BATCH_SIZE", 1, 24)
        if retries is None or max_input is None or batch_size is None:
            raise ConfigurationError("Размер пакета, лимит входа и число повторов не могут быть пустыми.")
        min_interval = _number(value("REQVALIDATOR_MIN_REQUEST_INTERVAL", "0"), "REQVALIDATOR_MIN_REQUEST_INTERVAL")
        if min_interval is None or min_interval < 0:
            raise ConfigurationError("REQVALIDATOR_MIN_REQUEST_INTERVAL должен быть неотрицательным числом секунд.")
        max_output = _integer(value("REQVALIDATOR_MAX_OUTPUT_TOKENS"), "REQVALIDATOR_MAX_OUTPUT_TOKENS", 1)
        temperature = _number(value("REQVALIDATOR_TEMPERATURE"), "REQVALIDATOR_TEMPERATURE")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ConfigurationError("REQVALIDATOR_TEMPERATURE должен быть между 0 и 2.")
        if temperature is not None and provider == "openai" and effort not in (None, "none"):
            raise ConfigurationError("Для OpenAI с включённым reasoning оставьте REQVALIDATOR_TEMPERATURE пустым.")
        seed = _integer(value("REQVALIDATOR_SEED"), "REQVALIDATOR_SEED", -(2**63), 2**63 - 1)
        if seed is not None and provider == "openai":
            raise ConfigurationError("Responses API не принимает seed. Уберите REQVALIDATOR_SEED.")

        return cls(
            provider=cast(Provider, provider),
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            validation_retries=retries,
            max_output_tokens=max_output,
            reasoning_effort=cast(ReasoningEffort | None, effort),
            compatible_format=cast(CompatibleFormat, response_format),
            temperature=temperature,
            seed=seed,
            max_input_chars=max_input,
            local_batch_size=batch_size,
            min_request_interval=min_interval,
        )


def _endpoint_host(url: str) -> str:
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
        host = endpoint.hostname
    except ValueError:
        raise ConfigurationError("Некорректный адрес REQVALIDATOR_BASE_URL.") from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not host
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or port == 0
        or any(character.isspace() for character in url)
    ):
        raise ConfigurationError("REQVALIDATOR_BASE_URL должен быть адресом HTTP(S) API без ключа и параметров.")
    return host


def _integer(raw: str, name: str, minimum: int, maximum: int | None = None) -> int | None:
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} должен быть целым числом.") from None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ConfigurationError(f"Значение {name} вне допустимого диапазона.")
    return parsed


def _number(raw: str, name: str) -> float | None:
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        raise ConfigurationError(f"{name} должен быть числом.") from None
    if not isfinite(parsed):
        raise ConfigurationError(f"{name} должен быть конечным числом.")
    return parsed
