from typing import Tuple
import os
import time
import threading
import datetime as dt
import requests
import pandas as pd
import logging
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi import FastAPI
from starlette.responses import Response
from sklearn.linear_model import Lasso
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
# -----------------------------------------------------------------------------
# Config (env-driven)
# -----------------------------------------------------------------------------
# PROM:        in-cluster Prometheus base URL
# NAMESPACE:   namespace of the workload we forecast
# DEPLOY:      deployment name we forecast
# LAGS:        how many past minutes to use as features
# INTERVAL:    loop sleep; also Prom range step is 60s, so ~1 sample per minute
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("predictor")

# -----------------------------------------------------------------------------
# Config (env-driven)
# -----------------------------------------------------------------------------
# PROM:        in-cluster Prometheus base URL
# NAMESPACE:   namespace of the workload we forecast
# DEPLOY:      deployment name we forecast
# LAGS:        how many past minutes to use as features
# INTERVAL:    loop sleep; also Prom range step is 60s, so ~1 sample per minute
# -----------------------------------------------------------------------------
PROM = os.getenv(
    "PROM_URL", "http://monitoring-kube-prometheus-stack-prometheus.monitoring.svc:9090")
NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")
DEPLOY = os.getenv("TARGET_DEPLOYMENT", "cpu-demo")
LAGS = int(os.getenv("LAGS", "60"))
INTERVAL = int(os.getenv("INTERVAL_SEC", "60"))

# -----------------------------------------------------------------------------
# Prometheus metric we expose from THIS service:
# predictor_cpu_forecast{deployment="cpu-demo"} = predicted utilization fraction
#   0.0..1.0 ~= 0%..100% of CPU limit; may exceed 1.0 briefly
# -----------------------------------------------------------------------------
G = Gauge(
    "predictor_cpu_forecast",
    "Predicted CPU utilization fraction (0..1+)",  # 1.0 ~ at CPU limit
    ["deployment"],
)

# Minimal HTTP surface: /metrics for Prom scrape, /healthz for probes
app = FastAPI(title="Predictor")


@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics for this predictor service."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    """Simple liveness/readiness check."""
    return {"ok": True}

# -----------------------------------------------------------------------------
# Prometheus helpers
# -----------------------------------------------------------------------------


def prom_query(q: str):
    """Instant query helper."""
    logger.info(f"Prometheus instant query: {q}")
    r = requests.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=30)
    logger.info(f"Prometheus response status: {r.status_code}")
    r.raise_for_status()
    result = r.json()
    logger.info(f"Prometheus instant query result: {result}")
    return result


def prom_range(q: str, start: dt.datetime, end: dt.datetime, step: str = "60s"):
    """Range query helper (returns vector of [ts, value] pairs)."""
    logger.info(
        f"Prometheus range query: {q} from {start} to {end} step {step}")
    r = requests.get(
        f"{PROM}/api/v1/query_range",
        params={"query": q, "start": int(start.timestamp()), "end": int(
            end.timestamp()), "step": step},
        timeout=45,
    )
    logger.info(f"Prometheus response status: {r.status_code}")
    r.raise_for_status()
    result = r.json()
    logger.info(f"Prometheus range query result: {result}")
    return result

# -----------------------------------------------------------------------------
# Feature engineering: convert a univariate series into supervised learning
# X = lag_1 .. lag_L, y = current value
# -----------------------------------------------------------------------------


def make_supervised(ts: pd.Series, lags: int) -> Tuple[pd.DataFrame, pd.Series]:
    logger.info(f"Building supervised dataset with {lags} lags")
    X = pd.DataFrame({f"lag_{i}": ts.shift(i) for i in range(1, lags + 1)})
    y = ts.copy()
    df = pd.concat([X, y.rename("y")], axis=1).dropna()
    logger.info(f"Supervised dataset shape: {df.shape}")
    return df.drop(columns=["y"]), df["y"]

# -----------------------------------------------------------------------------
# Main background loop:
#   1) Pull recent CPU usage and CPU limit from Prometheus
#   2) Build utilization fraction series (usage / limit)
#   3) Make supervised dataset of LAGS minutes → next-minute value
#   4) Fit a tiny model (Lasso) and forecast the next step
#   5) Clamp, publish forecast gauge
# -----------------------------------------------------------------------------


def loop():
    while True:
        try:
            logger.info("Starting predictor loop iteration")
            # --- Time window: much larger to ensure enough samples for lagging
            now = dt.datetime.now(dt.timezone.utc)
            start = now - dt.timedelta(minutes=LAGS + 10)
            logger.info(f"Time window: {start} to {now}")

            # --- Actual CPU usage (cores) across pods of the deployment (cadvisor)
            q_usage = (
                f'sum(rate(container_cpu_usage_seconds_total'
                f'{{namespace="{NAMESPACE}", pod=~"{DEPLOY}-.*", image!=""}}[2m]))'
            )
            logger.info(f"Querying CPU usage: {q_usage}")
            jr = prom_range(q_usage, start, now, step="60s")

            # Flatten range results -> [(ts, value), ...]
            points = [
                (float(v[0]), float(v[1]))
                for r in jr.get("data", {}).get("result", [])
                for v in r.get("values", [])
            ]
            logger.info(f"Received {len(points)} usage points from Prometheus")
            if not points:
                logger.warning(
                    "No usage data points found; sleeping and retrying.")
                time.sleep(INTERVAL)
                continue

            # Build a timestamp-indexed series at 1-min cadence and fill gaps
            df = pd.DataFrame(points, columns=["ts", "val"]).sort_values("ts")
            logger.info(f"Usage DataFrame head: {df.head()}")
            s = pd.Series(df["val"].values, index=pd.to_datetime(
                df["ts"], unit="s", utc=True))
            s = s.resample("5s").mean().ffill().bfill()
            logger.info(f"Resampled usage series: {s.tail(100)}")
            # Use all available points (or a very large buffer)
            s = s.tail(len(s))

            # --- CPU limit (cores) across the same pods (kube-state-metrics)
            q_limit = (
                f'sum(kube_pod_container_resource_limits'
                f'{{namespace="{NAMESPACE}", resource="cpu", pod=~"{DEPLOY}-.*"}})'
            )
            logger.info(f"Querying CPU limit: {q_limit}")
            jl = prom_query(q_limit)
            limit = float(jl["data"]["result"][0]["value"][1]
                          ) if jl["data"]["result"] else 0.1
            logger.info(f"CPU limit value: {limit}")
            if limit <= 0:
                logger.warning(f"CPU limit <= 0, using fallback value 0.1")
                limit = 0.1  # fallback guard; prevents div-by-zero and wild values

            # Utilization fraction series: usage (cores) / limit (cores)
            util = s / limit
            logger.info(f"Utilization series tail: {util.tail(5)}")

            # --- Build supervised dataset of lagged features
            X, y = make_supervised(util, LAGS)
            logger.info(f"Supervised X shape: {X.shape}, y shape: {y.shape}")
            if len(X) < 20:
                logger.warning(
                    f"Not enough samples ({len(X)}) for model; publishing 0.0 forecast.")
                G.labels(DEPLOY).set(0.0)
            else:
                # Scale features for Lasso (helps with stability)
                scaler = StandardScaler(with_mean=True)
                Xs = scaler.fit_transform(X)
                logger.info("Features scaled for Lasso regression.")

                # Train/test split (ordered, not shuffled) to avoid leakage
                Xtr, Xte, ytr, yte = train_test_split(
                    Xs, y, test_size=0.2, shuffle=False)
                logger.info(
                    f"Train set size: {Xtr.shape[0]}, Test set size: {Xte.shape[0]}")

                # Small L1-regularized linear model (fast, low memory)
                model = Lasso(alpha=0.0005, max_iter=5000)
                model.fit(Xtr, ytr)
                logger.info("Lasso model fitted.")

                # Build the latest lag window for 1-step-ahead prediction
                x_last_raw = pd.DataFrame([util.iloc[-i]
                                          for i in range(1, LAGS + 1)]).T
                x_last = scaler.transform(x_last_raw)
                logger.info(f"Latest lag window: {x_last_raw.values}")

                # Predicted next-minute utilization fraction
                yhat = float(model.predict(x_last)[0])
                logger.info(f"Predicted next-minute utilization: {yhat}")

                # Clamp to a sane range: >=0 and <=2.0 (200% of limit)
                yhat = max(0.0, min(yhat, 2.0))
                logger.info(f"Clamped forecast value: {yhat}")

                # Publish the forecast as a Prometheus gauge
                G.labels(DEPLOY).set(yhat)
                logger.info(
                    f"Published forecast gauge for deployment {DEPLOY}: {yhat}")

            # Now that we've used them, we can drop big intermediates
            del s, util

        except Exception as e:
            logger.error(f"Exception in predictor loop: {e}", exc_info=True)

        # Sleep until next iteration
        logger.info(f"Sleeping for {INTERVAL} seconds before next iteration.")
        time.sleep(INTERVAL)


# Start the background worker thread as soon as module loads (container start)
threading.Thread(target=loop, daemon=True).start()
