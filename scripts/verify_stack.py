"""Run inside the API container; export measured checks to /app/evidence/extended."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.train_model import dataset

API = os.getenv("API_URL", "http://api:8000")
MONITOR = os.getenv("EVIDENTLY_URL", "http://evidently:8001")
NOTIFIER = "http://notifier:8002"
PROMETHEUS = "http://prometheus:9090"
HEADERS = {"X-Ops-Token": os.getenv("OPS_TOKEN", "tutorial-local-only")}
OUT = Path(os.getenv("EVIDENCE_DIR", "/app/evidence/extended"))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "telegram": "not executed"}
    with httpx.Client(timeout=120) as http:
        def get(url):
            response = http.get(url, headers=HEADERS)
            response.raise_for_status()
            return response.json()

        def post(url, payload):
            response = http.post(url, json=payload, headers=HEADERS)
            response.raise_for_status()
            return response.json()

        def wait_until(check, seconds=120):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                result = check()
                if result:
                    return result
                time.sleep(2)
            raise AssertionError("condition did not become true before timeout")

        results["services"] = {name: get(url + "/health") for name, url in
                               {"api": API, "evidently": MONITOR, "notifier": NOTIFIER}.items()}
        before = get(API + "/health")["version"]
        rejected = post(API + "/ops/retrain", {"candidate": "dummy"})
        assert rejected["test_roc_auc"] == 0.5 and not rejected["promoted"]
        assert get(API + "/health")["version"] == before
        promoted = post(API + "/ops/retrain", {"candidate": "logistic"})
        assert promoted["promoted"] and promoted["test_roc_auc"] >= 0.95
        assert get(API + "/health")["version"] == promoted["version"]
        results["quality_gate"] = {"rejected": rejected, "accepted": promoted,
                                   "rejection_preserved_champion": True,
                                   "promotion_reloaded_api": True}
        print("Quality gate: dummy rejected; logistic promoted and reloaded", flush=True)

        baseline = dataset()[0].sample(200, random_state=501)
        for name, frame in (("normal", baseline), ("drift_3sigma", baseline + dataset()[0].std() * 3)):
            for index, features in enumerate(frame.to_dict("records")):
                post(API + "/predict", {"sample_id": f"verify-{name}-{index}", "features": features})
            result = post(MONITOR + "/analyze", {})
            assert result["dataset_drift"] == (name == "drift_3sigma")
            results[name] = result
            report = http.get(MONITOR + result["report"])
            report.raise_for_status()
            (OUT / f"{name}-report.html").write_text(report.text, encoding="utf-8")
            print(f"{name}: drift={result['dataset_drift']}, share={result['share_of_drifted_columns']}", flush=True)

        def drift_firing():
            alerts = get(PROMETHEUS + "/api/v1/alerts")["data"]["alerts"]
            return [a for a in alerts if a["labels"]["alertname"] == "FeatureDriftDetected" and a["state"] == "firing"]

        results["prometheus_drift_alert"] = wait_until(drift_firing)

        def received(status):
            return [e for e in get(NOTIFIER + "/events")["events"]
                    if e["source"] == "alertmanager" and e["status"] == status
                    and e["timestamp"] >= results["timestamp_utc"]
                    and "FeatureDriftDetected" in e["text"]]

        results["webhook_firing"] = wait_until(lambda: received("firing"))
        assert all(e["delivery"] == "local" for e in results["webhook_firing"])
        print("Prometheus -> Alertmanager -> local webhook: FIRING received", flush=True)
        # A fresh normal window clears the drift metric without editing alert rules.
        for index, features in enumerate(baseline.to_dict("records")):
            post(API + "/predict", {"sample_id": f"recovery-{index}", "features": features})
        results["recovery"] = post(MONITOR + "/analyze", {})
        assert not results["recovery"]["dataset_drift"]
        results["webhook_resolved"] = wait_until(lambda: received("resolved"))
        targets = get(PROMETHEUS + "/api/v1/targets")["data"]["activeTargets"]
        results["scrapes"] = [{"job": t["labels"]["job"], "health": t["health"]} for t in targets]
        assert all(t["health"] == "up" for t in results["scrapes"])
        results["success"] = True
        (OUT / "runtime-checks.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("All checks passed; FIRING and RESOLVED evidence exported", flush=True)


if __name__ == "__main__":
    main()
