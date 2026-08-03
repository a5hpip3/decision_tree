FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static

# No volume: this service holds no data. It reads the vault over HTTP.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "app.py"]
