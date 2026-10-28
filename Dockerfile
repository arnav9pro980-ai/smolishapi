
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "180", "--bind", "0.0.0.0:10000", "app:app"]
