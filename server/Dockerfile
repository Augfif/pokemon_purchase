FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY account_store.py .
COPY github_pool.py .
COPY templates ./templates

RUN mkdir -p /app/shared/data /app/shared/logs

EXPOSE 8000

CMD ["waitress-serve", "--listen=0.0.0.0:8000", "app:app"]
