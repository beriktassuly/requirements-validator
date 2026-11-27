FROM python:3.15.2-slim-trixie AS core

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY reqvalidator/ ./reqvalidator/

FROM core AS web
COPY web/ ./web/
COPY reports/submission.md reports/submission.json ./reports/
COPY web-demo/ ./web-demo/
ENV WEB_BIND_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "web.server"]

# Keep the regular build and CLI entry point as the default target.
FROM core AS cli
ENTRYPOINT ["python", "-m", "reqvalidator"]
