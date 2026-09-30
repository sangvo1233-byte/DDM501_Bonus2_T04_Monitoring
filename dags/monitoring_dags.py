"""Three bounded tutorial DAGs; ML dependencies stay in the API image."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from airflow import DAG
from airflow.exceptions import AirflowSkipException
from airflow.operators.python import PythonOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

API = os.getenv("API_URL", "http://api:8000")
MONITOR = os.getenv("EVIDENTLY_URL", "http://evidently:8001")
NOTIFIER = os.getenv("NOTIFIER_URL", "http://notifier:8002")


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "X-Ops-Token": os.getenv("OPS_TOKEN", "tutorial-local-only")})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def notify(text):
    # Notification failure must not hide the original training/health result.
    try:
        request(NOTIFIER + "/notify", {"text": text})
    except Exception as exc:
        print(f"Notification unavailable: {type(exc).__name__}")


def failed(context):
    notify(f"[AIRFLOW FAILED] {context['dag'].dag_id}.{context['task_instance'].task_id}")


def check_services():
    results = {}
    for name, url in {"api": API, "evidently": MONITOR,
                      "mlflow": os.getenv("MLFLOW_URL", "http://mlflow:5000")}.items():
        try:
            if name == "mlflow":
                with urllib.request.urlopen(url + "/health", timeout=10) as response:
                    results[name] = response.status == 200
            else:
                results[name] = request(url + "/health")["status"] == "ok"
        except Exception:
            results[name] = False
    if not all(results.values()):
        raise RuntimeError(f"Unhealthy services: {results}")
    notify(f"[HEALTH OK] {results}")
    return results


def analyze():
    try:
        result = request(MONITOR + "/analyze", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise AirflowSkipException("Not enough predictions for a drift analysis") from exc
        raise
    notify(f"[DRIFT] detected={result['dataset_drift']}, share={result['share_of_drifted_columns']}")
    return result


def needs_retrain(ti):
    result = ti.xcom_pull(task_ids="analyze")
    if not result or not result["dataset_drift"]:
        raise AirflowSkipException("No feature drift; keep the current champion")
    return True


def train(dag_run=None):
    conf = dag_run.conf if dag_run else {}
    result = request(API + "/ops/retrain", {"candidate": conf.get("candidate", "logistic")})
    notify(f"[RETRAIN] version={result['version']}, auc={result['test_roc_auc']}, promoted={result['promoted']}")
    return result


defaults = {"owner": "ddm501", "retries": 1, "retry_delay": timedelta(seconds=10),
            "on_failure_callback": failed}

with DAG("service_health_check", schedule="*/15 * * * *", start_date=datetime(2026, 1, 1),
         catchup=False, max_active_runs=1, default_args=defaults) as health_dag:
    PythonOperator(task_id="check_services", python_callable=check_services)

with DAG("drift_monitoring", schedule="@hourly", start_date=datetime(2026, 1, 1),
         catchup=False, max_active_runs=1, default_args=defaults) as drift_dag:
    analysis = PythonOperator(task_id="analyze", python_callable=analyze)
    gate = PythonOperator(task_id="needs_retrain", python_callable=needs_retrain)
    trigger = TriggerDagRunOperator(task_id="trigger_retrain", trigger_dag_id="model_retrain",
                                    wait_for_completion=False)
    analysis >> gate >> trigger

with DAG("model_retrain", schedule=None, start_date=datetime(2026, 1, 1), catchup=False,
         max_active_runs=1, default_args=defaults) as retrain_dag:
    PythonOperator(task_id="train_gate_promote_reload", python_callable=train)
