"""
Train the model this tutorial serves. 

Run:  python scripts/train_model.py
"""
import json
from pathlib import Path

import joblib
import pandas as pd
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "wdbc.csv"
MODELS = ROOT / "models"

DROP = ["sample_id", "diagnosis"]


def main() -> None:
    frame = pd.read_csv(RAW).dropna()
    frame = frame[frame["diagnosis"].isin({"M", "B"})].drop_duplicates("sample_id")
    features = [c for c in frame.columns if c not in DROP]
    X = frame[features]
    y = (frame["diagnosis"] == "M").astype(int)      # 1 = malignant

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42)
    model = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=5000, random_state=42))
    model.fit(X_tr, y_tr)
    auc = roc_auc_score(y_te, model.predict_proba(X_te)[:, 1])

    MODELS.mkdir(exist_ok=True)
    joblib.dump(model, MODELS / "model.joblib")
    (MODELS / "model_card.json").write_text(json.dumps({
        "version": "1.0.0",
        "sklearn_version": sklearn.__version__,
        "features": features,
        "train_rows": len(X_tr),
        "test_roc_auc": round(float(auc), 5),
        "positive_class": "M (malignant)",
    }, indent=2))
    print(f"trained on {len(X_tr)} rows, test ROC AUC {auc:.5f}")
    print(f"saved -> models/model.joblib  and  models/model_card.json")


if __name__ == "__main__":
    main()
