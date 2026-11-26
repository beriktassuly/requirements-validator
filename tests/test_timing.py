import json
from types import SimpleNamespace

import pytest
from httpx2 import ConnectError, Request, Response
from openai import DefaultHttpxClient, _base_client

from reqvalidator import llm, timing
from reqvalidator.config import Settings
from reqvalidator.model import MergeResponse
from reqvalidator.review import call_with_validation


def test_model_retries_keep_separate_counters_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    starts: list[float] = []
    scripted = iter((429, 503, {}, None, {"pairs": []}, {"pairs": [{"left": 1, "right": 2}]}))

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def send(_client: object, request: Request, **_options: object) -> Response:
        starts.append(monotonic())
        sleep(1)
        result = next(scripted)
        assert json.loads(request.content)["reasoning_effort"] == "medium"
        if result is None:
            raise ConnectError("offline fixture", request=request)
        if isinstance(result, int):
            return Response(result, request=request, headers={"Retry-After": "2"}, json={"error": "offline fixture"})
        return Response(200, request=request, json={
            "id": "offline-fixture",
            "object": "chat.completion",
            "model": "offline-fixture",
            "created": 0,
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(result)},
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    monkeypatch.setattr(llm, "monotonic", monotonic)
    monkeypatch.setattr(llm, "sleep", sleep)
    monkeypatch.setattr(timing, "monotonic", monotonic)
    monkeypatch.setattr(_base_client, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(_base_client, "random", lambda: 0)
    monkeypatch.setattr(DefaultHttpxClient, "send", send)
    timings = timing.RunTimings()
    settings = Settings(
        provider="compatible", model="offline-fixture", api_key="local",
        base_url="http://localhost/v1", compatible_format="json_object",
        reasoning_effort="low", validation_retries=1, min_request_interval=10,
    )

    with llm.OpenAIReviewer(settings, timings=timings) as reviewer:
        answer = call_with_validation(
            reviewer, "Инструкция", "Запрос", MergeResponse, lambda result: bool(result.pairs),
            max_input_chars=1000, retry_note="\nПовтор", retry_error="Неполное покрытие",
            retry_kind="coverage", timings=timings, reasoning_effort="medium",
        )

    assert [(pair.left, pair.right) for pair in answer.pairs] == [(1, 2)]
    assert starts == [0, 3, 6, 10, 11.5, 20]
    assert timings.model_calls == 2
    assert timings.sdk_requests == 6
    assert timings.model_wait_seconds == 6
    assert timings.limiter_wait_seconds == 10.5
    assert timings.retries == {"format": 1, "coverage": 1, "validation": 0, "network": 3}
    assert timings.retry_seconds == {"format": 5.5, "coverage": 8.5, "validation": 0, "network": 7.5}
    assert timings.sdk_retry_wait_seconds == 4.5
    assert timings.sdk_retry_reasons == {"429": 1, "503": 1, "transport": 1}
    assert timings.usage == {"input_tokens": 9, "output_tokens": 6, "total_tokens": 15}


def test_progress_marks_failed_stage_as_interrupted() -> None:
    events: list[str] = []
    timings = timing.RunTimings(progress=events.append)
    with pytest.raises(ValueError, match="broken"):
        with timings.stage("extract"):
            raise ValueError("broken")
    assert "Начато: извлечение текста." in events[0]
    assert "Прервано: извлечение текста" in events[1]
    assert "Завершено" not in events[1]
    assert "extract" in timings.as_dict()["stages_seconds"]
