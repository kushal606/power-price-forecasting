"""
models/evaluator.py — PowerCast Model Evaluation
==================================================
Performs backtesting on the held-out test set and returns
everything the evaluation dashboard needs.

DESIGN RULES:
  • Reads _STORE (already populated by warm_up) — no re-training
  • Uses the SAME features / leakage-removal logic as forecaster.py
  • Never imports from forecaster.py to avoid circular imports;
    it accesses _STORE via a passed-in argument instead.
  • Pure functions — no global state, safe to call repeatedly.
"""

import os
import io
import time
import tempfile
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Downsampling cap for Plotly series (keeps browser snappy)
_MAX_CHART_POINTS = 500


# =============================================================================
# 1.  METRICS
# =============================================================================

def _mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted)))


def _rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def _mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Mean Absolute Percentage Error — skips zero actuals to avoid div/0."""
    mask = actual != 0
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100)


def _smape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Symmetric MAPE — more stable when actuals pass through zero."""
    denom = (np.abs(actual) + np.abs(predicted)) / 2
    mask  = denom > 0
    return float(np.mean(np.abs(actual[mask] - predicted[mask]) / denom[mask]) * 100)


# =============================================================================
# 2.  BACKTESTING
# =============================================================================

def run_backtest(store: dict, split_date: str = '2025-01-01',
                 max_rows: int = 5000) -> dict:
    """
    Run a standard train/test backtest using the already-trained model.

    Parameters
    ----------
    store      : _STORE dict from forecaster.py  { model, features, df, ready }
    split_date : ISO date string — rows on/after this date form the test set
    max_rows   : cap test set to keep prediction fast (takes tail of test set)

    Returns
    -------
    dict with keys:
      metrics        — MAE, RMSE, MAPE, SMAPE, r2, n_test
      chart_data     — downsampled series for Plotly (times, actual, predicted)
      residuals      — downsampled residual series
      error_hist     — histogram bucket data for error distribution
      sample_table   — list of dicts [{datetime, actual, predicted, error, pct_error}]
      split_date     — the split used
      test_period    — { start, end }  human-readable
      timing_seconds — how long the backtest took
    """
    if not store.get('ready'):
        raise RuntimeError(store.get('error') or "Model not ready — warm_up() not complete.")

    t0       = time.perf_counter()
    model    = store['model']
    features = store['features']
    df       = store['df'].copy()

    # ── Split ─────────────────────────────────────────────────────────────────
    test_df = df[df['datetime'] >= split_date].copy()
    if test_df.empty:
        raise ValueError(
            f"No data on/after split_date={split_date}. "
            f"Dataset ends at {df['datetime'].max().date()}."
        )

    # Optional cap: use only the last max_rows rows of the test set
    if len(test_df) > max_rows:
        log.info(f"[evaluator] Capping test set from {len(test_df)} → {max_rows} rows")
        test_df = test_df.tail(max_rows).reset_index(drop=True)

    # ── Predict ───────────────────────────────────────────────────────────────
    X_test   = test_df[features]
    y_actual = test_df['MCP'].values

    log.info(f"[evaluator] Predicting on {len(test_df)} test rows …")
    y_pred = model.predict(X_test)
    y_pred = np.maximum(y_pred, 0)          # clip negative prices
    residuals = y_actual - y_pred

    # ── Metrics ───────────────────────────────────────────────────────────────
    from sklearn.metrics import r2_score
    metrics = {
        'mae':    round(_mae(y_actual, y_pred),   2),
        'rmse':   round(_rmse(y_actual, y_pred),  2),
        'mape':   round(_mape(y_actual, y_pred),  2),
        'smape':  round(_smape(y_actual, y_pred), 2),
        'r2':     round(float(r2_score(y_actual, y_pred)), 4),
        'n_test': int(len(test_df)),
        'n_train': int(len(df[df['datetime'] < split_date])),
    }
    log.info(f"[evaluator] MAE={metrics['mae']}  RMSE={metrics['rmse']}  "
             f"MAPE={metrics['mape']}%  R²={metrics['r2']}")

    # ── Chart data (downsampled) ───────────────────────────────────────────────
    step = max(1, len(test_df) // _MAX_CHART_POINTS)
    idx  = np.arange(0, len(test_df), step)

    times_raw   = test_df['datetime'].iloc[idx]
    times_str   = times_raw.dt.strftime('%d %b %H:%M').tolist()
    act_ds      = y_actual[idx].round(2).tolist()
    pred_ds     = y_pred[idx].round(2).tolist()
    resid_ds    = residuals[idx].round(2).tolist()

    # ── Error histogram buckets ────────────────────────────────────────────────
    # Use 40 bins; return bin centres and counts for Plotly bar chart
    counts, edges = np.histogram(residuals, bins=40)
    hist_x = ((edges[:-1] + edges[1:]) / 2).round(1).tolist()
    hist_y = counts.tolist()

    # ── Sample table (last 100 rows, most recent first) ───────────────────────
    sample_n   = min(100, len(test_df))
    sample_idx = list(range(len(test_df) - sample_n, len(test_df)))
    sample_rows = []
    for i in reversed(sample_idx):           # newest first
        a = float(y_actual[i])
        p = float(y_pred[i])
        e = round(a - p, 2)
        pct = round(abs(e) / a * 100, 2) if a != 0 else 0.0
        sample_rows.append({
            'datetime':  test_df['datetime'].iloc[i].strftime('%d %b %Y %H:%M'),
            'actual':    round(a, 2),
            'predicted': round(p, 2),
            'error':     e,
            'pct_error': pct,
        })

    # ── Test period ────────────────────────────────────────────────────────────
    test_period = {
        'start': test_df['datetime'].iloc[0].strftime('%d %b %Y'),
        'end':   test_df['datetime'].iloc[-1].strftime('%d %b %Y'),
    }

    elapsed = round(time.perf_counter() - t0, 2)
    log.info(f"[evaluator] Backtest done in {elapsed}s")

    return {
        'metrics':        metrics,
        'chart_data':     { 'times': times_str, 'actual': act_ds, 'predicted': pred_ds },
        'residuals':      { 'times': times_str, 'values': resid_ds },
        'error_hist':     { 'x': hist_x, 'y': hist_y },
        'sample_table':   sample_rows,
        'split_date':     split_date,
        'test_period':    test_period,
        'timing_seconds': elapsed,
    }


# =============================================================================
# 3.  CSV EXPORT
# =============================================================================

def build_csv(store: dict, split_date: str = '2025-01-01') -> str:
    """
    Generate a full evaluation CSV file and return its temp file path.
    Columns: DateTime, Actual_MCP, Predicted_MCP, Error, Pct_Error
    """
    if not store.get('ready'):
        raise RuntimeError("Model not ready.")

    model    = store['model']
    features = store['features']
    df       = store['df']

    test_df  = df[df['datetime'] >= split_date].copy()
    if test_df.empty:
        raise ValueError("No test data available for this split date.")

    y_actual = test_df['MCP'].values
    y_pred   = np.maximum(model.predict(test_df[features]), 0)
    errors   = y_actual - y_pred

    out = pd.DataFrame({
        'DateTime':      test_df['datetime'].dt.strftime('%Y-%m-%d %H:%M'),
        'Actual_MCP':    y_actual.round(2),
        'Predicted_MCP': y_pred.round(2),
        'Error':         errors.round(2),
        'Pct_Error':     np.where(
            y_actual != 0,
            (np.abs(errors) / y_actual * 100).round(2),
            0.0
        ),
    })

    tmp = tempfile.NamedTemporaryFile(suffix='.csv', delete=False, mode='w')
    out.to_csv(tmp.name, index=False)
    tmp.close()
    return tmp.name
