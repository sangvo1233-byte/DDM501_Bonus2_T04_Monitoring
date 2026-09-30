"""Track candidates and promote only models that pass the fixed holdout gate."""
import json
import os
from importlib.metadata import version as installed_version
from threading import Lock

import mlflow
import mlflow.sklearn
from mlflow.exceptions import MlflowException
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient

from scripts.train_model import dataset, train_candidate

MODEL_NAME = "wdbc-risk-model"
training_lock = Lock()


def client():
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    return MlflowClient()


def champion():
    api = client()
    try:
        registered = api.get_registered_model(MODEL_NAME)
    except MlflowException as exc:
        if exc.error_code == "RESOURCE_DOES_NOT_EXIST":
            return None
        raise
    if "champion" not in registered.aliases:
        return None
    version = api.get_model_version_by_alias(MODEL_NAME, "champion")
    card = json.loads(api.get_model_version(MODEL_NAME, version.version).tags["card"])
    card["version"] = version.version
    return mlflow.sklearn.load_model(f"models:/{MODEL_NAME}/{version.version}"), card


def retrain(candidate="logistic", minimum_auc=0.95, tolerance=0.005):
    # ponytail: one training job per process; use an external lock for multiple replicas.
    with training_lock:
        api = client()
        current = champion()
        model, card = train_candidate(candidate)
        auc = card["test_roc_auc"]
        previous_auc = current[1]["test_roc_auc"] if current else None
        accepted = auc >= minimum_auc and (
            previous_auc is None or auc >= previous_auc - tolerance)
        mlflow.set_experiment("DDM501 Bonus 2 WDBC")
        with mlflow.start_run(run_name=f"{candidate}-candidate") as run:
            mlflow.log_params({"candidate": candidate, "random_state": 42,
                               "minimum_auc": minimum_auc, "tolerance": tolerance})
            mlflow.log_metric("test_roc_auc", auc)
            X_tr, _, _, _ = dataset()
            mlflow.sklearn.log_model(
                model, "model", signature=infer_signature(X_tr, model.predict(X_tr)),
                input_example=X_tr.head(2), pip_requirements=[
                    f"{name}=={installed_version(name)}" for name in
                    ("scikit-learn", "numpy", "pandas", "scipy", "joblib", "cloudpickle")])
            mlflow.log_dict(card, "model_card.json")
            mlflow.set_tag("quality_gate", "passed" if accepted else "rejected")
            version = mlflow.register_model(f"runs:/{run.info.run_id}/model", MODEL_NAME)
            api.set_model_version_tag(MODEL_NAME, version.version, "card", json.dumps(card))
            api.set_model_version_tag(MODEL_NAME, version.version, "quality_gate",
                                      "passed" if accepted else "rejected")
            if accepted:
                api.set_registered_model_alias(MODEL_NAME, "champion", version.version)
            result = {"candidate": candidate, "version": version.version,
                      "run_id": run.info.run_id, "test_roc_auc": auc,
                      "previous_auc": previous_auc, "promoted": accepted}
        return result
