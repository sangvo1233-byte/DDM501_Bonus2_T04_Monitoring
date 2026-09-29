FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/

#   1. `docker compose up --build` works from a clean checkout
#   2. The pickle is written by exactly the scikit-learn version installed one
#      layer above it. A model trained on your machine and copied in is a
#      pickle from whatever version you happened to have -- which is how you
#      get `AttributeError: 'DecisionTreeClassifier' object has no attribute
#      'monotonic_cst'` at load time and no idea why.
RUN python scripts/train_model.py && test -f models/model.joblib

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
r=urllib.request.urlopen('http://localhost:8000/health',timeout=4); \
sys.exit(0 if json.load(r)['model_loaded'] else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
