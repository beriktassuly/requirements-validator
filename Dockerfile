FROM python:3.15.2-slim-trixie

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY reqvalidator/ ./reqvalidator/

ENTRYPOINT ["python", "-m", "reqvalidator"]
