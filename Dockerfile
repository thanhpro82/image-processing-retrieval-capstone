FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CORPUS_DIR=/data SEARCH_DB=/index/search.sqlite HOST=0.0.0.0 PORT=8080
WORKDIR /app
COPY app/ /app/app/
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["sh", "-c", "if [ ! -f /index/search.sqlite ] || [ /data/search.sqlite -nt /index/search.sqlite ]; then cp /data/search.sqlite /index/search.sqlite.tmp && mv /index/search.sqlite.tmp /index/search.sqlite; fi; exec python -m app.server"]
