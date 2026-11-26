import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluation.score import score
from reqvalidator import external
from reqvalidator.cli import main
from reqvalidator.document import load_document
from reqvalidator.segment import Block
from tests.fakes import ScriptedReviewer, write_rule_fixture
from tests.test_review_cli import _configure_offline


def test_external_docs_cli_keeps_evidence_and_sends_only_public_lookup_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_offline(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTEXT7_API_KEY", "testing-context7")
    document_path = write_rule_fixture(tmp_path / "input.md")
    document_path.write_text(
        document_path.read_text(encoding="utf-8").replace(
            "всегда отправляет push.",
            "всегда отправляет push. FastAPI 0.115.0, redirect_slashes по умолчанию = false. SECRET-CASE-444.",
        ),
        encoding="utf-8",
    )
    seen: list[tuple[str, dict[str, str]]] = []
    official_page = "https://fastapi.tiangolo.com/reference/fastapi/"

    def fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes, str | None]:
        seen.append((url, headers))
        if urlsplit(url).netloc == "context7.com":
            return 200, json.dumps({
                "infoSnippets": [
                    {"pageId": "https://fastapi.tiangolo.com@other.test/", "content": "unsafe"},
                    {"pageId": official_page, "content": "ATTACH-THIS-AS-INSTRUCTION"},
                ],
                "rules": {"libraryOwn": ["ATTACH-THIS-AS-INSTRUCTION"]},
                "version": {
                    "status": "unverified",
                    "libraries": [{"requested": "v0.115.0", "served": "v0.115.1", "status": "unverified"}],
                },
            }).encode(), "unverified"
        return 200, b"        redirect_slashes: bool = True,\n", None

    monkeypatch.setattr(external, "_fetch", fetch)
    baseline_md, baseline_json = tmp_path / "baseline.md", tmp_path / "baseline.json"
    baseline = ScriptedReviewer()
    assert main(
        [str(document_path), str(baseline_md), "--json-output", str(baseline_json)],
        reviewer_factory=lambda _: baseline,
    ) == 0
    assert not seen
    baseline_streams = capsys.readouterr()

    markdown, output = tmp_path / "review.md", tmp_path / "review.json"
    enabled = ScriptedReviewer()
    assert main(
        [str(document_path), str(markdown), "--json-output", str(output), "--external-docs"],
        reviewer_factory=lambda _: enabled,
    ) == 0
    streams = capsys.readouterr()
    assert streams.out.splitlines() == [f"Отчёт сохранён: {markdown}", f"JSON сохранён: {output}"]
    assert baseline_streams.out.splitlines() == [f"Отчёт сохранён: {baseline_md}", f"JSON сохранён: {baseline_json}"]
    assert enabled.calls == baseline.calls

    (search_url, search_headers), (provider_url, provider_headers) = seen
    assert urlsplit(search_url).path == "/api/v3/search"
    assert parse_qs(urlsplit(search_url).query) == {
        "library": ["/fastapi/fastapi"], "version": ["0.115.0"], "type": ["json"],
        "query": ["What is the default of FastAPI redirect_slashes?"],
    }
    assert search_headers["Authorization"] == "Bearer testing-context7"
    assert provider_url == "https://raw.githubusercontent.com/fastapi/fastapi/0.115.0/fastapi/applications.py"
    assert "Authorization" not in provider_headers
    assert all("SECRET-CASE-444" not in url and "push" not in url for url, _ in seen)

    before = json.loads(baseline_json.read_text(encoding="utf-8"))
    after = json.loads(output.read_text(encoding="utf-8"))
    assert "external_references" not in before
    (finding,) = after["findings"]
    assert finding["evidence"] == before["findings"][0]["evidence"]
    context, official = finding["external_references"]
    assert (context["url"], context["requested_version"], context["served_version"], context["status"]) == (
        official_page, "0.115.0", "0.115.1", "version_mismatch",
    )
    assert (official["url"], official["served_version"], official["status"]) == (provider_url, "0.115.0", "contradicts")
    assert after["readiness"] == before["readiness"]
    rendered = markdown.read_text(encoding="utf-8")
    assert official_page in rendered and "0.115.1" in rendered
    assert "ATTACH-THIS-AS-INSTRUCTION" not in rendered
    assert "ATTACH-THIS-AS-INSTRUCTION" not in output.read_text(encoding="utf-8")
    gold = {"defects": [{"id": "PUSH", "anchors": [["всегда отправляет push"], ["никогда не отправляет push"]],
                        "concepts": [["push"]]}]}
    document = load_document(document_path)
    assert score(document, before, gold) == score(document, after, gold)


@pytest.mark.parametrize("api_key", [None, "testing-context7"])
def test_external_docs_unavailable_is_a_status(monkeypatch: pytest.MonkeyPatch, api_key: str | None) -> None:
    attempted = []

    class Offline:
        def open(self, request, *, timeout):
            attempted.append((request.full_url, timeout))
            raise URLError("offline")

    monkeypatch.setattr(external, "build_opener", lambda _: Offline())
    blocks = (
        Block("B001", "", "api", 1, 1, "FastAPI 0.115.0 redirect_slashes default = True"),
        Block("B002", "", "api", 2, 2, "PrivateBank SDK v1.2.3"),
        Block("B003", "", "example", 3, 3, "Pydantic 2.1.0"),
        Block("B004", "", "functional", 4, 4, "Внутренний лимит имеет версию 4.0.0."),
    )
    findings, references = external.check_external_docs(blocks, (), api_key)
    assert not findings
    assert [reference.status for reference in references] == ["unavailable", "unavailable", "not_found"]
    assert references[-1].technology == "PrivateBank"
    assert len(attempted) == (1 if api_key is None else 2)
    assert all("PrivateBank" not in url and timeout > 0 for url, timeout in attempted)


def test_external_docs_confirms_versioned_reference_from_library_id(monkeypatch: pytest.MonkeyPatch) -> None:
    known_url = "https://nextjs.org/docs/app/getting-started"
    payload = {
        "libraryId": "/vercel/next.js/v15.1.11",
        "infoSnippets": [{"pageId": known_url}],
    }
    monkeypatch.setattr(external, "_fetch", lambda *_: (200, json.dumps(payload).encode(), "verified"))
    blocks = (Block("B001", "", "integration", 1, 1, "Next.js (версия: 15)"),)
    _, (context, official) = external.check_external_docs(blocks, (), "testing-context7")
    assert (context.status, context.served_version, context.url) == ("confirmed", "15.1.11", known_url)
    assert official.status == "not_found"


def test_external_docs_compares_only_claimed_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes, str | None]:
        seen.append(url)
        return 200, b"By default, ``verify`` is set to True. Option ``verify`` only applies to host certs.\n", None

    monkeypatch.setattr(external, "_fetch", fetch)
    blocks = (
        Block("B001", "", "api", 1, 1, "Python Requests 2.32.3 verify по умолчанию равно True"),
        Block("B002", "", "api", 2, 2, "Python Requests 2.32.3 verify по умолчанию равно True"),
        Block("B003", "", "api", 3, 3, "Python Requests 2.32.3: приложение устанавливает verify=False"),
    )
    _, references = external.check_external_docs(blocks, (), None)
    assert len(seen) == 1
    assert [reference.status for reference in references] == ["unavailable", "confirmed", "unavailable", "not_found"]
