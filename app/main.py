"""
Breast cancer diagnosis API — the service Tutorial 04 watches.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, List

import joblib
import pandas as pd
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, FiniteFloat
import httpx

from app.metrics import (CAPTURE_FAILURES, ERRORS, LATENCY, MALIGNANT_SHARE, MODEL_INFO,
                         MODEL_LOADED, PREDICTIONS, TRAP)
from app.ops import require_ops

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
THRESHOLD = 0.5
WINDOW = 200

app = FastAPI(title="WDBC diagnosis API", version="1.0.0")

state: Dict[str, object] = {"model": None, "card": None}
recent: deque = deque(maxlen=WINDOW)


class PredictRequest(BaseModel):
    sample_id: str = Field(..., min_length=1, max_length=80, examples=["WDBC-0001"])
    features: Dict[str, FiniteFloat]


def install_model(model, card):
    state["bundle"] = (model, card)
    state["model"] = model
    state["card"] = card
    MODEL_LOADED.set(1)
    MODEL_INFO.clear()
    MODEL_INFO.labels(version=card["version"], sklearn_version=card["sklearn_version"]).set(1)


@app.on_event("startup")
def load_model() -> None:
    if os.getenv("MLFLOW_TRACKING_URI"):
        from app.registry import champion, retrain
        loaded = champion()
        if loaded is None:
            result = retrain()
            if not result["promoted"]:
                raise RuntimeError("initial model failed the quality gate")
            loaded = champion()
        install_model(*loaded)
        return
    path = MODELS / "model.joblib"
    if not path.exists():
        MODEL_LOADED.set(0)
        return
    install_model(joblib.load(path), json.loads((MODELS / "model_card.json").read_text()))


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    ERRORS.labels(reason="invalid_request").inc()
    return await request_validation_exception_handler(request, exc)


def capture(features):
    url = os.getenv("EVIDENTLY_URL")
    if not url:
        return
    try:
        response = httpx.post(url + "/capture", json={"features": features}, timeout=2,
                              headers={"X-Ops-Token": os.getenv("OPS_TOKEN", "tutorial-local-only")})
        response.raise_for_status()
    except httpx.HTTPError:
        CAPTURE_FAILURES.inc()


@app.get("/health")
def health() -> dict:
    # The health check reports what it can actually do, not merely that the
    # process is alive. A container that answers 200 while serving 503s to
    # every real request is worse than one that is honestly down.
    return {"status": "ok" if state["model"] else "degraded",
            "model_loaded": state["model"] is not None,
            "version": (state["card"] or {}).get("version")}


@app.post("/predict")
def predict(req: PredictRequest, background: BackgroundTasks) -> dict:
    # Read one tuple so a concurrent reload cannot mix model and feature metadata.
    model, card = state.get("bundle", (state["model"], state["card"]))
    if model is None:
        ERRORS.labels(reason="model_not_loaded").inc()
        raise HTTPException(status_code=503, detail="model not loaded")

    features: List[str] = card["features"]
    missing = [f for f in features if f not in req.features]
    if missing:
        ERRORS.labels(reason="missing_features").inc()
        raise HTTPException(status_code=422,
                            detail=f"missing features: {missing[:3]}")

    # The timer wraps only the work being measured. Wrap the whole request and
    # you are also measuring FastAPI's parsing, which is not what the number
    # is supposed to mean.
    start = time.perf_counter()
    frame = pd.DataFrame([[req.features[f] for f in features]], columns=features)
    probability = float(model.predict_proba(frame)[0, 1])
    LATENCY.observe(time.perf_counter() - start)

    outcome = "malignant" if probability >= THRESHOLD else "benign"
    labels = {"outcome": outcome}
    if TRAP:
        # Section 6. One new time series per patient, forever.
        labels["sample_id"] = req.sample_id
    PREDICTIONS.labels(**labels).inc()

    recent.append(1 if outcome == "malignant" else 0)
    MALIGNANT_SHARE.set(sum(recent) / len(recent))
    background.add_task(capture, {f: req.features[f] for f in features})

    return {"sample_id": req.sample_id, "probability": round(probability, 6),
            "outcome": outcome, "threshold": THRESHOLD,
            "model_version": card["version"]}


class RetrainRequest(BaseModel):
    candidate: str = Field(default="logistic", pattern="^(logistic|dummy)$")


@app.post("/ops/retrain", dependencies=[Depends(require_ops)])
def retrain_model(request: RetrainRequest):
    if not os.getenv("MLFLOW_TRACKING_URI"):
        raise HTTPException(status_code=503, detail="MLflow registry is not configured")
    from app.registry import champion, retrain
    result = retrain(request.candidate)
    if result["promoted"]:
        install_model(*champion())
    return result


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    # Plain text in Prometheus exposition format. Open it in a browser once and
    # read it -- it is the entire contract between your service and Prometheus,
    # and it is simpler than people expect.
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
