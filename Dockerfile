FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 merlin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY merlin ./merlin
COPY sql ./sql
COPY bin ./bin
COPY LICENSE NOTICE merlin.example.toml ./

USER merlin
ENV PYTHONUNBUFFERED=1
CMD ["bin/ingest"]
