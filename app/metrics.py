"""
Every metric this service exposes, in the order the tutorial introduces them.

"""
import os

from prometheus_client import Counter, Gauge, Histogram

# Off by default; the tutorial turns it on so you can
# watch what happens, then turn it back off.
TRAP = os.getenv("T04_TRAP", "0") == "1"


# --------------------------------------------------------------- COUNTER
# Only ever goes up. You almost never read a counter directly -- you read
# rate() of it, which turns "3,412 requests since the process started" into
# "requests per second right now".
#
PREDICTIONS = Counter(
    "wdbc_predictions_total",
    "Predictions served, by outcome.",
    ["outcome"] + (["sample_id"] if TRAP else []),
)

ERRORS = Counter(
    "wdbc_errors_total",
    "Requests that failed, by reason.",
    ["reason"],
)


# ------------------------------------------------------------- HISTOGRAM
# Buckets, not an average. An average latency of 40 ms is consistent with
# everyone getting 40 ms and with 95% getting 10 ms while 5% get 600 ms --
# and only one of those is an incident. Buckets keep the shape.
#
LATENCY = Histogram(
    "wdbc_prediction_latency_seconds",
    "Time spent producing one prediction.",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)


# ----------------------------------------------------------------- GAUGE
# Goes up and down. Use it for a level: a queue depth, a temperature, a
# yes/no readiness flag.
MODEL_LOADED = Gauge(
    "wdbc_model_loaded",
    "1 if a model is loaded and able to serve, 0 otherwise.",
)

# The info pattern: a gauge fixed at 1 whose LABELS carry the payload. It
# looks strange until the first time you join on it to find out which model
# version was serving during an incident.
MODEL_INFO = Gauge(
    "wdbc_model_info",
    "Always 1. The labels are the point.",
    ["version", "sklearn_version"],
)

# A gauge that moves with the data rather than the traffic. If the share of
# malignant predictions doubles overnight, either the world changed or your
# inputs did -- and you want to know which before a human notices.
MALIGNANT_SHARE = Gauge(
    "wdbc_malignant_share",
    "Share of the last 200 predictions that came out malignant.",
)
