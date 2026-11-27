"""Loopback UI gateway. Routes only known assets, uploads, reports and run snapshots."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
from urllib.parse import parse_qs, urlsplit

from reqvalidator.config import ConfigurationError, Settings
from reqvalidator.document import DocumentError, load_document

from .demo import Demo
from .runs import ACTIVE_STATUSES, AVAILABLE_SECONDS, Run, RunConflict, Runs
from .worker import source_payload


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
FORMATS = (".pdf", ".docx", ".md", ".txt")
_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "web" / "static"
_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?", re.IGNORECASE)
_RUN_PATH = re.compile(r"/api/runs/([a-f0-9]{32})(?:/(events|cancel|report|download/(?:md|json)))?")
_DEMO_DOWNLOAD = re.compile(r"/api/demo/download/(md|json)")
_MODEL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,100}")
_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "report-viewer.js": "text/javascript; charset=utf-8",
    "report.css": "text/css; charset=utf-8",
}


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def configuration() -> tuple[bool, dict[str, object]]:
    try:
        settings = Settings.from_env()
    except ConfigurationError:
        return False, {"provider": None, "model": None, "reviewers": [], "externalDocs": False, "concurrency": 3}
    model = settings.model if _MODEL_NAME.fullmatch(settings.model) else None
    secrets = [settings.api_key, settings.context7_api_key]
    if model is not None and any(secret and secret in model for secret in secrets):
        model = None
    return settings.provider != "compatible" or settings.api_key != "local", {
        "provider": settings.provider,
        "model": model,
        "reviewers": settings.reviewers,
        "externalDocs": settings.external_docs,
        "concurrency": settings.concurrency,
    }


class Server(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, runs: Runs) -> None:
        self.demo = Demo(_ROOT)
        self.runs = runs
        self.configured, self.profile = configuration()
        super().__init__((host, port), Handler)

    def handle_error(self, request: object, client_address: object) -> None:
        # A closing browser or a broken request must not print a traceback with local paths.
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RequirementsValidator"
    sys_version = ""
    server: Server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(25)

    def log_message(self, format: str, *args: object) -> None:
        # Request paths may include filenames. Keep them out of service diagnostics.
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if not _HOST.fullmatch(host) or (origin is not None and origin.lower() != f"http://{host.lower()}"):
                raise HttpError(HTTPStatus.FORBIDDEN, "Приложение принимает запросы с локального адреса.")
            target = urlsplit(self.path)
            if target.scheme or target.netloc:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Некорректный адрес запроса.")
            if method == "GET":
                self._get(target.path)
            else:
                self._post(target.path, parse_qs(target.query))
        except HttpError as error:
            self._json({"error": str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (OSError, ValueError):
            self._json({"error": "Не удалось обработать запрос. Повторите попытку."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _get(self, path: str) -> None:
        if path == "/api/config":
            self._json({
                "configured": self.server.configured,
                "profile": self.server.profile,
                "maxUploadBytes": MAX_UPLOAD_BYTES,
                "formats": FORMATS,
                "demoReplay": self.server.demo.replay is not None,
                "availabilitySeconds": AVAILABLE_SECONDS,
            })
            return
        if path == "/api/demo/report":
            self._json(self.server.demo.report(self.server.demo.source))
            return
        if path == "/api/demo/replay":
            replay = self.server.demo.replay
            if replay is None:
                raise HttpError(HTTPStatus.NOT_FOUND, "Запись прогона недоступна.")
            self._json(replay)
            return
        if match := _DEMO_DOWNLOAD.fullmatch(path):
            extension = match[1]
            self._download(self.server.demo.download(extension).read_bytes(), extension)
            return
        if match := _RUN_PATH.fullmatch(path):
            job = self._job(match[1])
            action = match[2]
            if action is None:
                with self.server.runs.condition:
                    payload = job.snapshot()
                self._json(payload)
                return
            if action == "events":
                self._event_stream(job)
                return
            if action == "report":
                report = self.server.runs.report(job)
                if report is not None:
                    self._json(report)
                    return
                raise HttpError(HTTPStatus.NOT_FOUND, "Отчёт ещё не записан или больше недоступен.")
            if action.startswith("download/"):
                extension = action.removeprefix("download/")
                payload = self.server.runs.download(job, extension)
                if payload is not None:
                    self._download(payload, extension)
                    return
                raise HttpError(HTTPStatus.NOT_FOUND, "Файл отчёта недоступен.")
        name = "index.html" if path == "/" else path.removeprefix("/static/").removeprefix("/")
        if name in _ASSETS:
            self._send((_STATIC / name).read_bytes(), _ASSETS[name])
            return
        raise HttpError(HTTPStatus.NOT_FOUND, "Страница не найдена.")

    def _post(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/runs":
            if not self.server.configured:
                raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "Для запуска настройте ключ, модель и профиль на сервере.")
            if self._option(query, "consent") != "yes":
                raise HttpError(HTTPStatus.BAD_REQUEST, "Подтвердите отправку текста провайдеру.")
            raw_concurrency = self._option(query, "concurrency") or str(self.server.profile["concurrency"])
            if raw_concurrency not in {str(number) for number in range(1, 9)}:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Число одновременных запросов должно быть от 1 до 8.")
            folder = self.server.runs.new_folder()
            keep_folder = False
            try:
                source = self._upload(folder, query)
                try:
                    payload = self.server.runs.start(folder, source, int(raw_concurrency))
                except RunConflict as error:
                    raise HttpError(HTTPStatus.CONFLICT, str(error)) from None
                keep_folder = True
            finally:
                if not keep_folder:
                    shutil.rmtree(folder, ignore_errors=True)
            self._json(payload, HTTPStatus.ACCEPTED)
            return
        if path == "/api/demo/source":
            with tempfile.TemporaryDirectory(dir=self.server.runs.storage) as folder:
                document = self._upload(Path(folder), query)
                try:
                    source = source_payload(load_document(document))
                except DocumentError as error:
                    message = "Не удалось извлечь текст. Проверьте формат, кодировку и текстовый слой файла."
                    if str(error) == "В документе нет доступного текста для проверки.":
                        message = str(error)
                    raise HttpError(HTTPStatus.UNPROCESSABLE_CONTENT, message) from None
            self._json(self.server.demo.report(source))
            return
        if (match := _RUN_PATH.fullmatch(path)) and match[2] == "cancel":
            self._json(self.server.runs.cancel(self._job(match[1])))
            return
        raise HttpError(HTTPStatus.NOT_FOUND, "Действие недоступно.")

    @staticmethod
    def _option(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key, [])
        if len(values) > 1:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Параметр запроса передан несколько раз.")
        return values[0] if values else ""

    def _upload(self, folder: Path, query: dict[str, list[str]]) -> Path:
        filename = self._option(query, "filename")
        if (
            not filename or not filename.strip() or len(filename.encode("utf-8")) > 180
            or any(c in '\\/:<>"|?*' or ord(c) < 32 or ord(c) == 127 for c in filename)
        ):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Недопустимое имя файла.")
        if Path(filename).suffix.casefold() not in FORMATS:
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Выберите документ PDF, DOCX, MD или TXT.")
        if self.headers.get_content_type() != "application/octet-stream":
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Загрузите файл в двоичном формате.")
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Потоковая передача файла не поддерживается.")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise HttpError(HTTPStatus.LENGTH_REQUIRED, "Размер файла не указан.")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,18}", lengths[0]):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Некорректный размер файла.")
        remaining = int(lengths[0])
        if remaining == 0:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Файл пуст. Выберите документ с текстом.")
        if remaining > MAX_UPLOAD_BYTES:
            raise HttpError(HTTPStatus.CONTENT_TOO_LARGE, "Максимальный размер документа — 20 МиБ.")
        directory = folder / "input"
        directory.mkdir()
        target = directory / filename
        with target.open("xb") as stream:
            while remaining:
                data = self.rfile.read(min(remaining, 1024 * 1024))
                if not data:
                    raise HttpError(HTTPStatus.BAD_REQUEST, "Загрузка прервана. Выберите файл снова.")
                stream.write(data)
                remaining -= len(data)
        return target

    def _job(self, run_id: str) -> Run:
        job = self.server.runs.lookup(run_id)
        if job is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Прогон не найден или срок доступа истёк.")
        return job

    def _event_stream(self, job: Run) -> None:
        self._headers(HTTPStatus.OK, "text/event-stream; charset=utf-8")
        self.end_headers()
        condition = self.server.runs.condition
        revision = -1
        while True:
            with condition:
                condition.wait_for(lambda: job.revision != revision, timeout=2)
                revision = job.revision
                payload = job.snapshot()
            event = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            self.wfile.write(f"event: snapshot\ndata: {event}\n\n".encode("utf-8"))
            self.wfile.flush()
            if payload["status"] not in ACTIVE_STATUSES:
                return

    def _headers(self, status: HTTPStatus, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.close_connection = True

    def _send(self, payload: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK,
              attachment: str | None = None) -> None:
        self._headers(status, content_type)
        self.send_header("Content-Length", str(len(payload)))
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(data, "application/json; charset=utf-8", status)

    def _download(self, payload: bytes, extension: str) -> None:
        mime = "application/json" if extension == "json" else "text/markdown"
        self._send(payload, f"{mime}; charset=utf-8", attachment=f"requirements-review.{extension}")


def main() -> None:
    def stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    host = os.environ.get("WEB_BIND_HOST", "127.0.0.1")
    with tempfile.TemporaryDirectory(prefix="requirements-validator-") as directory:
        runs = Runs(_ROOT, Path(directory))
        try:
            with Server(host, 8000, runs) as server:
                server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            runs.close()


if __name__ == "__main__":
    main()
