FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py http_app.py oauth.py onboarding.py api.py ./

# Vaults live on a mounted volume — without one, every redeploy wipes history.
# No VOLUME instruction: Railway rejects it ("use Railway Volumes") and manages
# the mount itself, so attach the volume at /data on the service instead.
ENV CONTEXT_VAULT_HOME=/data

# http_app reads PORT itself, so no shell expansion is needed in the command.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "http_app.py"]
