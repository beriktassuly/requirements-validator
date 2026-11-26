"""Opt-in checks of versioned public documentation, without sending source text."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import ExternalReference, Finding
from .segment import Block


_SEARCH_URL = "https://context7.com/api/v3/search"
_MAX_LOOKUPS = 8
_MAX_BYTES = 1_000_000
_TIMEOUT_SECONDS = 4
_VERSION_TEXT = (
    r"[vV]?(?:20[0-9]{2}-[0-9]{2}-[0-9]{2}|[0-9]{1,3}(?:\.[0-9]{1,3}){0,3})"
    r"(?:(?:[.-]?(?:alpha|beta|rc|a|b))[0-9]{1,3})?"
)
_VERSION = re.compile(_VERSION_TEXT, re.IGNORECASE | re.ASCII)
_SEPARATOR = r"[ \t`(]*(?:(?:==|[=:\|@]|\b(?:верси[яию]|version)\b[ \t]*:?)\s*`?)?"
_VERSION_LABEL = _SEPARATOR + rf"(?P<version>{_VERSION_TEXT})(?![\w@+*-]|\.[\w.*])"
_EXTERNAL_NAME = r"(?P<name>[A-Za-z][A-Za-z0-9._-]{0,39})(?![\w.-])"
_EXPLICIT_API = (
    re.compile(r"\b(?:API|SDK|библиотек[аиу]|library)[ \t]+" + _EXTERNAL_NAME + _VERSION_LABEL, re.IGNORECASE),
    re.compile(r"\b" + _EXTERNAL_NAME + r"[ \t]+(?:API|SDK)\b" + _VERSION_LABEL, re.IGNORECASE),
)
_GENERIC_NAMES = {"api", "sdk", "rest", "http", "https", "internal", "external"}
_LINK_SUFFIX = re.compile(r"[A-Za-z0-9/_~.+\-]*(?:#[A-Za-z0-9_~.+\-]*)?")
_VERSIONED_LINK = re.compile(r"/(?:blob|tree)/(?P<version>" + _VERSION_TEXT + r")/")
_VERSION_MARKER = re.compile(r"(?:==|@|\bверси[яию]\b|\bversion\b)", re.IGNORECASE)
_DEFAULT = r"(?:по[ \t]+умолчанию|by[ \t]+default|default)"
_BOOLEAN = r"(?P<value>true|false)"
_GENERAL_QUESTION = "Where is the official API reference for this release?"


@dataclass(frozen=True, slots=True)
class _Rule:
    parameter: str
    question: str
    official_value: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class _Technology:
    name: str
    aliases: tuple[str, ...]
    library_id: str
    official_prefixes: tuple[str, ...]
    source: str | None = None
    rules: tuple[_Rule, ...] = ()


_CATALOG = (
    _Technology(
        "FastAPI", ("FastAPI",), "/fastapi/fastapi",
        ("https://fastapi.tiangolo.com/", "https://github.com/fastapi/fastapi/"),
        "https://raw.githubusercontent.com/fastapi/fastapi/{version}/fastapi/applications.py",
        (
            _Rule(
                "redirect_slashes", "What is the default of FastAPI redirect_slashes?",
                re.compile(
                    r"(?m)^        redirect_slashes: "
                    r"(?:Annotated\[\n(?:(?!        \])[^\n]*\n){0,50}        \]|bool)"
                    r" = (?P<value>True|False),$",
                ),
            ),
            _Rule(
                "debug", "What is the default of FastAPI debug?",
                re.compile(
                    r"(?m)^        debug: "
                    r"(?:Annotated\[\n(?:(?!        \])[^\n]*\n){0,50}        \]|bool)"
                    r" = (?P<value>True|False),$",
                ),
            ),
        ),
    ),
    _Technology(
        "Python Requests", ("Python Requests", "library requests", "библиотека requests", "requests"),
        "/psf/requests",
        ("https://requests.readthedocs.io/", "https://github.com/psf/requests/"),
        "https://raw.githubusercontent.com/psf/requests/v{version}/docs/user/advanced.rst",
        (
            _Rule(
                "verify", "What is the default of the Python Requests TLS verify option?",
                re.compile(
                    r"(?m)^By default, ``verify`` is set to (?P<value>True|False)\.",
                ),
            ),
        ),
    ),
    _Technology(
        "Pydantic", ("Pydantic",), "/pydantic/pydantic",
        ("https://docs.pydantic.dev/", "https://github.com/pydantic/pydantic/"),
    ),
    _Technology(
        "OpenAI Python SDK", ("OpenAI Python SDK", "OpenAI SDK", "SDK OpenAI", "openai-python"),
        "/openai/openai-python", ("https://github.com/openai/openai-python/",),
    ),
    _Technology(
        "Stripe API", ("Stripe API", "API Stripe"), "/stripe/openapi",
        ("https://docs.stripe.com/", "https://github.com/stripe/openapi/"),
    ),
    _Technology(
        "Next.js", ("Next.js",), "/vercel/next.js",
        ("https://nextjs.org/docs/", "https://github.com/vercel/next.js/"),
    ),
)
_KNOWN_NAMES = tuple(
    (
        technology,
        re.compile(
            rf"(?<![\w./-])(?:{'|'.join(re.escape(name) for name in technology.aliases)})(?![\w./-])"
            + _VERSION_LABEL,
            re.IGNORECASE,
        ),
    )
    for technology in _CATALOG
)


@dataclass(frozen=True, slots=True)
class _Target:
    technology: str
    version: str
    catalog: _Technology | None = None
    rule: _Rule | None = None
    expected: str | None = None

    @property
    def question(self) -> str:
        return self.rule.question if self.rule else _GENERAL_QUESTION


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str,
    ) -> None:
        return None


def _version(value: object) -> str | None:
    if isinstance(value, str) and _VERSION.fullmatch(value):
        return value.lower().removeprefix("v")
    return None


def _literal_default(line: str, parameter: str) -> str | None:
    # Require an explicit claim about a default. An application override is not a claim.
    label = rf"(?<![\w.]){re.escape(parameter)}\b"
    pattern = rf"(?:{label}\s*{_DEFAULT}|{_DEFAULT}\s+{label})\s*(?:(?:[-—:=]|is|равно)\s*)?{_BOOLEAN}\b"
    values = {match["value"].lower() for match in re.finditer(pattern, line.replace("`", ""), re.IGNORECASE)}
    return values.pop() if len(values) == 1 else None


def _targets(blocks: Sequence[Block]) -> dict[_Target, set[str]]:
    targets: dict[_Target, set[str]] = defaultdict(set)
    for block in blocks:
        if block.kind in {"example", "governance"}:
            continue
        for line in block.text.splitlines():
            known_spans = []
            for catalog, pattern in _KNOWN_NAMES:
                for match in pattern.finditer(line):
                    version = _version(match["version"])
                    if version is None:
                        continue
                    if (
                        catalog.name == "Python Requests" and match[0].lower().startswith("requests")
                        and not _VERSION_MARKER.search(match[0]) and not match["version"].lower().startswith("v")
                    ):
                        # The English word "requests" may describe operations rather than a package.
                        continue
                    known_spans.append(match.span())
                    assertions = [
                        _Target(catalog.name, version, catalog, rule, declared)
                        for rule in catalog.rules
                        if (declared := _literal_default(line, rule.parameter)) is not None
                    ]
                    for target in assertions or [_Target(catalog.name, version, catalog)]:
                        targets[target].add(block.id)
            for pattern in _EXPLICIT_API:
                for match in pattern.finditer(line):
                    name = match["name"]
                    if name.casefold() in _GENERIC_NAMES or any(
                        start < match.end() and match.start() < stop for start, stop in known_spans
                    ):
                        continue
                    if (version := _version(match["version"])) is not None:
                        # Unknown names are reported locally and never sent.
                        targets[_Target(name, version)].add(block.id)
    return targets


def _fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes, str | None]:
    # Callers use only compiled-in endpoints. Reject every redirect, including between HTTPS hosts.
    try:
        request = Request(url, headers=headers)
        with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_BYTES + 1)
            if len(body) <= _MAX_BYTES:
                return response.status, body, response.headers.get("X-Context7-Search-Version")
    except HTTPError as exc:
        exc.close()
        return exc.code, b"", None
    except (HTTPException, OSError, ValueError):
        pass
    return 0, b"", None


def _reference(
    target: _Target,
    source: Literal["context7", "official"],
    code: int,
    *,
    url: str | None = None,
    served: str | None = None,
) -> ExternalReference:
    return ExternalReference(
        url=url, requested_version=target.version, served_version=served,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        status="not_found" if code in {200, 404} else "unavailable",
        technology=target.technology, question=target.question, source=source,
    )


def _official_link(payload: dict[str, object], catalog: _Technology) -> str | None:
    for collection, key in (("infoSnippets", "pageId"), ("codeSnippets", "codeId")):
        snippets = payload.get(collection)
        if not isinstance(snippets, list):
            continue
        for snippet in snippets:
            url = snippet.get(key) if isinstance(snippet, dict) else None
            if not isinstance(url, str) or len(url) > 300:
                continue
            for prefix in catalog.official_prefixes:
                if not url.startswith(prefix):
                    continue
                suffix = url[len(prefix):]
                path = suffix.partition("#")[0]
                if "." in path.split("/") or ".." in path.split("/") or "//" in path:
                    continue
                if prefix.startswith("https://github.com/") and not path.startswith(("blob/", "tree/", "releases/")):
                    continue
                if _LINK_SUFFIX.fullmatch(suffix):
                    return url
    return None


def _context7(target: _Target, catalog: _Technology, api_key: str | None) -> ExternalReference:
    if not api_key:
        return _reference(target, "context7", 0)
    params = {
        "library": catalog.library_id, "version": target.version,
        "query": target.question, "type": "json",
    }
    code, data, version_header = _fetch(f"{_SEARCH_URL}?{urlencode(params)}", {
        "Accept": "application/json", "Authorization": f"Bearer {api_key}",
    })
    result = _reference(target, "context7", code)
    if code != 200:
        return result
    try:
        payload = json.loads(data)
    except (ValueError, RecursionError):
        return replace(result, status="unavailable")
    if not isinstance(payload, dict):
        return replace(result, status="unavailable")
    version = payload.get("version")
    version = version if isinstance(version, dict) else {}
    libraries = version.get("libraries")
    detail = version
    if isinstance(libraries, list) and len(libraries) == 1 and isinstance(libraries[0], dict):
        detail = libraries[0]
    url = _official_link(payload, catalog)
    served = _version(detail.get("served"))
    if "served" not in detail:
        library_id = payload.get("libraryId")
        if isinstance(library_id, str):
            project_id, _, tag = library_id.rpartition("/")
            if project_id == catalog.library_id:
                served = _version(tag)
        if served is None and url:
            tag = _VERSIONED_LINK.search(url)
            served = _version(tag["version"]) if tag else None
    result = replace(result, url=url, served_version=served)
    if "unverified" in (version_header, version.get("status"), detail.get("status")):
        return replace(result, status="version_mismatch")
    matches = served == target.version or (served is not None and served.startswith(target.version + "."))
    if served is not None and not matches:
        return replace(result, status="version_mismatch")
    # This only confirms availability of a versioned reference. A technical claim still requires
    # an independent check in the version-pinned official documentation.
    if target.rule is None and matches and url:
        return replace(result, status="confirmed")
    return result


def _official(target: _Target, catalog: _Technology) -> ExternalReference:
    if not target.rule or not catalog.source:
        return _reference(target, "official", 404)
    url = catalog.source.format(version=target.version)
    code, data, _ = _fetch(url, {"Accept": "text/plain"})
    result = _reference(target, "official", code)
    if code != 200:
        return result
    result = replace(result, url=url, served_version=target.version)
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    values = {match["value"].lower() for match in target.rule.official_value.finditer(text)}
    if len(values) == 1:
        return replace(result, status="confirmed" if values.pop() == target.expected else "contradicts")
    return result


def check_external_docs(
    blocks: Sequence[Block], findings: Sequence[Finding], api_key: str | None,
) -> tuple[tuple[Finding, ...], tuple[ExternalReference, ...]]:
    """Annotate grounded cards and report checks on named technologies without a card."""
    by_block: dict[str, list[ExternalReference]] = defaultdict(list)
    references = []
    lookups = 0
    for target, block_ids in _targets(blocks).items():
        if target.catalog is None:
            results = (_reference(target, "official", 404),)
        elif lookups >= _MAX_LOOKUPS:
            results = (_reference(target, "context7", 0), _reference(target, "official", 0))
        else:
            lookups += 1
            results = (_context7(target, target.catalog, api_key), _official(target, target.catalog))
        references.extend(results)
        for block_id in block_ids:
            by_block[block_id].extend(results)
    annotated = tuple(
        replace(finding, external_references=tuple(dict.fromkeys(
            reference for anchor in finding.evidence for reference in by_block.get(anchor.block_id, ())
        )))
        for finding in findings
    )
    return annotated, tuple(references)
