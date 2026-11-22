FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY listeners.py oauth_config.py oauth_server.py oauth_store.py oauth_admin.py ./
COPY healthcheck.py ./
COPY x_public.py ./
COPY exa_provider.py ./

RUN groupadd --gid 10001 mcp && useradd --uid 10001 --gid mcp --no-create-home mcp && \
    mkdir -p /data && chown mcp:mcp /data

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/tmp
ENV MCP_OAUTH_DATABASE=/data/oauth.sqlite3
ENV MCP_X_CACHE_PATH=/data/x-public.sqlite3
USER mcp

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD ["python", "healthcheck.py"]

CMD ["python", "server.py"]
