FROM python:3.11-slim

WORKDIR /app
# Write print() output to the logs immediately instead of buffering it.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn web:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 120
