"""Feature drift with Evidently; bounded snapshots and reproducible HTML reports."""
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import pandas as pd
from evidently.metric_preset import DataDriftPreset
from evidently.report import Report
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from pydantic import BaseModel, FiniteFloat
from app.ops import require_ops
from scripts.train_model import dataset

app = FastAPI(title="WDBC Evidently drift monitor")
REFERENCE = dataset()[0]
FEATURES = list(REFERENCE.columns)
REPORTS = Path(os.getenv("REPORTS_DIR", "runtime/reports"))
REPORTS.mkdir(parents=True, exist_ok=True)
samples = deque(maxlen=1000)
lock = Lock()
analysis_lock = Lock()
latest = {}
DRIFT = Gauge("evidently_data_drift_detected", "1 if at least 30% of features drift")
SHARE = Gauge("evidently_drifted_feature_share", "Share of drifting input features")
SIZE = Gauge("evidently_current_window_size", "Samples in the bounded current window")


class CaptureRequest(BaseModel):
    features: dict[str, FiniteFloat]


@app.get("/health")
def health():
    return {"status": "ok", "reference_rows": len(REFERENCE), "current_rows": len(samples)}


@app.post("/capture", dependencies=[Depends(require_ops)])
def capture(request: CaptureRequest):
    if set(request.features) != set(FEATURES):
        raise HTTPException(status_code=422, detail="expected exactly the 30 WDBC features")
    with lock:
        samples.append({f: request.features[f] for f in FEATURES})
        SIZE.set(len(samples))
    return {"captured": True}


@app.post("/analyze", dependencies=[Depends(require_ops)])
def analyze():
    # ponytail: serialize reports for one monitor replica; use a job queue if scaled out.
    with analysis_lock:
        with lock:
            snapshot = list(samples)[-200:]
        if len(snapshot) < 100:
            raise HTTPException(status_code=409, detail="need at least 100 predictions")
        report = Report(metrics=[DataDriftPreset(drift_share=0.3)])
        report.run(reference_data=REFERENCE, current_data=pd.DataFrame(snapshot, columns=FEATURES))
        metrics = report.as_dict()["metrics"][0]["result"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"drift-{stamp}"
        result = {"dataset_drift": metrics["dataset_drift"],
                  "share_of_drifted_columns": metrics["share_of_drifted_columns"],
                  "number_of_drifted_columns": metrics["number_of_drifted_columns"],
                  "reference_rows": len(REFERENCE), "current_rows": len(snapshot),
                  "report": f"/reports/{name}.html", "timestamp": stamp}
        report.save_html(str(REPORTS / f"{name}.html"))
        (REPORTS / f"{name}.json").write_text(json.dumps(result, indent=2))
        latest.clear()
        latest.update(result)
        DRIFT.set(int(result["dataset_drift"]))
        SHARE.set(result["share_of_drifted_columns"])
        return result


@app.get("/reports")
def reports():
    return {"latest": latest, "reports": [p.name for p in sorted(REPORTS.glob("*.html"))]}


@app.get("/reports/{name}")
def report_file(name: str):
    if not name.startswith("drift-") or not name.endswith(".html") or Path(name).name != name:
        raise HTTPException(status_code=404, detail="unknown report")
    path = REPORTS / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown report")
    return FileResponse(path, media_type="text/html")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
