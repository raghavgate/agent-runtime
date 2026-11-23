# Public demo image: synthetic data only, served with `serve --public`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p .runs .conversations .evaluations \
    && chown -R appuser:appuser .runs .conversations .evaluations
USER appuser

# Render provides PORT and RENDER_EXTERNAL_HOSTNAME. OPENAI_API_KEY comes from the
# dashboard and is never baked into the image.
EXPOSE 10000
CMD ["python", "main.py", "--config", "config.synthetic.json", "serve", "--public", "--provider", "openai"]
