FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py http_app.py ./

# Vaults live on a mounted volume — without one, every redeploy wipes history.
ENV CONTEXT_VAULT_HOME=/data
VOLUME ["/data"]

# http_app reads PORT itself, so no shell expansion is needed in the command.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "http_app.py"]
