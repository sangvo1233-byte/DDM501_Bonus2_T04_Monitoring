"""Verify actual scheduler runs from the host; uses only Python's standard library."""
import base64
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AIRFLOW = "http://127.0.0.1:18081/api/v1"
API = "http://127.0.0.1:18000"
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    password = subprocess.check_output([
        "docker", "compose", "exec", "-T", "airflow", "cat",
        "/opt/airflow/standalone_admin_password.txt"], cwd=ROOT, text=True).strip()
    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    evidence = {"timestamp_utc": datetime.now(timezone.utc).isoformat()}

    def request(url, payload=None):
        headers = {"Content-Type": "application/json"}
        if url.startswith(AIRFLOW):
            headers["Authorization"] = auth
        data = json.dumps(payload).encode() if payload is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=120) as response:
            return json.load(response)

    def wait(check):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(3)
        raise AssertionError("Airflow did not finish within 240 seconds")

    def trigger(dag, suffix, conf=None):
        run = request(f"{AIRFLOW}/dags/{dag}/dagRuns", {
            "dag_run_id": f"verify-{STAMP}-{suffix}", "conf": conf or {}})
        run_id = run["dag_run_id"]

        def completed():
            current = request(f"{AIRFLOW}/dags/{dag}/dagRuns/{run_id}")
            if current["state"] == "failed":
                raise AssertionError(f"{dag} failed: {run_id}")
            return current if current["state"] == "success" else None

        done = wait(completed)
        tasks = request(f"{AIRFLOW}/dags/{dag}/dagRuns/{run_id}/taskInstances")["task_instances"]
        summary = {"dag_id": dag, "run_id": run_id, "state": done["state"],
                   "tasks": {t["task_id"]: t["state"] for t in tasks}}
        print(json.dumps(summary), flush=True)
        return summary

    evidence["health"] = trigger("service_health_check", "health")
    version = request(API + "/health")["version"]
    evidence["rejected_candidate"] = trigger("model_retrain", "dummy", {"candidate": "dummy"})
    assert request(API + "/health")["version"] == version
    evidence["normal"] = trigger("drift_monitoring", "normal")
    assert evidence["normal"]["tasks"]["needs_retrain"] == "skipped"
    # Generate deterministic 3-sigma traffic inside the API image; no host ML dependencies.
    code = "import httpx; from scripts.train_model import dataset; x=dataset()[0]; d=x.sample(200,random_state=501)+x.std()*3; c=httpx.Client(); [c.post('http://api:8000/predict',json={'sample_id':str(i),'features':r}).raise_for_status() for i,r in enumerate(d.to_dict('records'))]"
    subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", code], cwd=ROOT, check=True)
    previous_runs = {r["dag_run_id"] for r in request(f"{AIRFLOW}/dags/model_retrain/dagRuns?limit=100")["dag_runs"]}
    evidence["drift"] = trigger("drift_monitoring", "drift")
    assert evidence["drift"]["tasks"]["trigger_retrain"] == "success"

    def retrained():
        runs = request(f"{AIRFLOW}/dags/model_retrain/dagRuns?limit=100")["dag_runs"]
        fresh = [r for r in runs if r["dag_run_id"] not in previous_runs]
        if any(r["state"] == "failed" for r in fresh):
            raise AssertionError("Triggered retraining failed")
        return [{"run_id": r["dag_run_id"], "state": r["state"]} for r in fresh if r["state"] == "success"]

    evidence["triggered_retrain"] = wait(retrained)
    evidence["serving_version"] = request(API + "/health")["version"]
    assert evidence["serving_version"] != version
    evidence["success"] = True
    output = ROOT / "evidence" / "extended" / "airflow-checks.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("Airflow scheduler checks passed; password was not exported", flush=True)


if __name__ == "__main__":
    main()
