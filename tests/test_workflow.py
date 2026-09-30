"""Regression checks for validation, drift and observable local notifications."""
from collections import deque
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import drift, main, notifications
from scripts.train_model import dataset, train_candidate

HEADERS = {"X-Ops-Token": "tutorial-local-only"}


def test_training_separates_a_good_candidate_from_a_rejected_dummy():
    _, good = train_candidate("logistic")
    _, dummy = train_candidate("dummy")
    assert good["test_roc_auc"] >= 0.95
    assert dummy["test_roc_auc"] == 0.5


def test_registry_without_a_champion_can_bootstrap(monkeypatch):
    from app import registry
    api = SimpleNamespace(get_registered_model=lambda name: SimpleNamespace(aliases={}))
    monkeypatch.setattr(registry, "client", lambda: api)
    assert registry.champion() is None


def test_api_requires_complete_finite_features_and_protects_retraining(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("EVIDENTLY_URL", raising=False)
    monkeypatch.setenv("OPS_TOKEN", "tutorial-local-only")
    row = dataset()[0].iloc[0].to_dict()
    with TestClient(main.app) as api:
        assert api.get("/health").json()["model_loaded"]
        assert api.post("/predict", json={"sample_id": "test", "features": row}).status_code == 200
        missing = dict(row)
        missing.pop(next(iter(missing)))
        assert api.post("/predict", json={"sample_id": "test", "features": missing}).status_code == 422
        assert api.post("/predict", json={"sample_id": "test", "features": {"radius_mean": "NaN"}}).status_code == 422
        assert api.post("/ops/retrain", json={}).status_code == 403


def test_evidently_detects_feature_drift_and_writes_a_real_report(monkeypatch, tmp_path):
    monkeypatch.setattr(drift, "samples", deque(maxlen=1000))
    monkeypatch.setattr(drift, "REPORTS", tmp_path)
    monkeypatch.setenv("OPS_TOKEN", "tutorial-local-only")
    with TestClient(drift.app) as monitor:
        assert monitor.post("/analyze", json={}, headers=HEADERS).status_code == 409
        baseline = drift.REFERENCE.sample(200, random_state=501)
        for row in baseline.to_dict("records"):
            assert monitor.post("/capture", json={"features": row}, headers=HEADERS).status_code == 200
        normal = monitor.post("/analyze", json={}, headers=HEADERS).json()
        assert normal["dataset_drift"] is False
        shifted = baseline + drift.REFERENCE.std() * 3
        for row in shifted.to_dict("records"):
            monitor.post("/capture", json={"features": row}, headers=HEADERS)
        result = monitor.post("/analyze", json={}, headers=HEADERS).json()
        assert result["dataset_drift"] is True
        assert result["share_of_drifted_columns"] >= 0.3
        assert monitor.get(result["report"]).status_code == 200
        assert (tmp_path / result["report"].split("/")[-1]).stat().st_size > 1000
        assert monitor.get("/reports/not-a-report.html").status_code == 404


def test_local_receiver_persists_firing_and_resolved_without_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "STORE", tmp_path / "events.jsonl")
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    monkeypatch.setenv("OPS_TOKEN", "tutorial-local-only")
    with TestClient(notifications.app) as receiver:
        for status in ("firing", "resolved"):
            reply = receiver.post("/alerts", json={"alerts": [
                {"status": status, "labels": {"alertname": "ApiDown"}}]})
            assert reply.json()["received"] == 1
        events = receiver.get("/events", headers=HEADERS).json()["events"]
        assert [event["status"] for event in events] == ["firing", "resolved"]
        assert all(event["delivery"] == "local" for event in events)
        assert receiver.get("/events").status_code == 403
