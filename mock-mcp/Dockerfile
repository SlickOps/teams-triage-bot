FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# MCP_SERVER_KIND (jenkins|datadog|argocd) and PORT are supplied at deploy time --
# same codebase, 3 instances, picked at runtime. No default kind: fail loudly if
# unset rather than silently serving the wrong personality.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
