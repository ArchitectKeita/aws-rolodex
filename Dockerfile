# Small, non-root, pinned. Pulled from ECR Public so builds never hit Docker Hub rate limits.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

RUN useradd --system --uid 10001 rolodex
USER rolodex

EXPOSE 8080
# One process, eight threads: the heartbeat and request counter stay in one place per container.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "30", "--access-logfile", "-", "app:app"]
