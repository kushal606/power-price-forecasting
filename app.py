"""
Power Trading Forecast — Flask Application  (OPTIMISED)
========================================================
Changes:
  • warm_up() called at startup → model ready before first request
  • /api/forecast returns instantly for Monthly/Seasonal (background job)
  • /api/job/<id> polling endpoint for frontend to track progress
  • Synchronous path kept for Daily and Weekly (fast enough)
"""

import logging
import os
import time
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, g

# ── Monitoring (Prometheus) ───────────────────────────────────────────────────
# Safe import: app works normally if prometheus_client is not installed
from monitoring import (
    metrics_blueprint,
    record_request,
    record_forecast,
    record_error,
    update_mcp_gauge,
    set_model_ready,
)

from models.forecaster import (
    BG_HORIZONS,
    HORIZON_BLOCKS,
    _STORE,
    generate_forecast,
    generate_forecast_excel,
    generate_forecast_by_date,
    get_job_status,
    submit_background_forecast,
    warm_up,
)
from models.evaluator import run_backtest, build_csv

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object('config.Config')

# ── Register Prometheus /metrics endpoint ─────────────────────────────────────
app.register_blueprint(metrics_blueprint)


# ── Request timing hooks ──────────────────────────────────────────────────────
# before_request: stamp the start time on Flask's per-request context (g)
# after_request:  compute duration and record to Prometheus
# These hooks add < 0.1 ms overhead per request.

@app.before_request
def _start_timer():
    g._start_time = time.perf_counter()


@app.after_request
def _record_metrics(response):
    duration = time.perf_counter() - getattr(g, '_start_time', time.perf_counter())
    endpoint = request.endpoint or request.path   # fallback if no named route
    record_request(
        method=request.method,
        endpoint=endpoint,
        status_code=response.status_code,
        duration=duration,
    )
    return response   # must return response unchanged


# ── Train model at startup (before first request) ────────────────────────────
def _warm_up_and_signal():
    """Wraps warm_up() so Prometheus gauge reflects model state after training."""
    warm_up()
    set_model_ready(_STORE['ready'])

with app.app_context():
    import threading
    t = threading.Thread(target=_warm_up_and_signal, daemon=True)
    t.start()


# =============================================================================
# PAGE ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/dashboard')
def dashboard():
    model_results = [
        {"model": "Linear Regression", "train_mape": 14.52, "test_mape": 16.31, "train_rmse": 820.10, "test_rmse": 910.45},
        {"model": "Decision Tree",     "train_mape": 10.21, "test_mape": 13.74, "train_rmse": 615.30, "test_rmse": 780.20},
        {"model": "Random Forest",     "train_mape":  7.85, "test_mape":  9.09, "train_rmse": 480.15, "test_rmse": 560.70},
        {"model": "XGBoost",           "train_mape":  6.90, "test_mape":  9.83, "train_rmse": 420.40, "test_rmse": 590.30},
        {"model": "LightGBM",          "train_mape":  7.10, "test_mape": 10.05, "train_rmse": 440.60, "test_rmse": 605.10},
    ]
    best_model = min(model_results, key=lambda x: x["test_mape"])
    return render_template('dashboard.html', model_results=model_results, best_model=best_model)


@app.route('/forecast')
def forecast():
    return render_template('forecast.html')


@app.route('/about')
def about():
    return render_template('about.html')


# =============================================================================
# EVALUATION DASHBOARD
# =============================================================================

@app.route('/evaluation')
def evaluation():
    """
    Model Evaluation page.
    Runs backtesting on the test set (2025-01-01 onward) using the
    already-trained model stored in _STORE — no re-training.
    All heavy work is done here so the template stays pure HTML/JS.
    """
    # ── Guard: model must be ready ────────────────────────────────────────────
    if not _STORE.get('ready'):
        error_msg = _STORE.get('error') or 'Model is still loading — please wait and refresh.'
        return render_template('evaluation.html',
                               error=error_msg,
                               result=None)

    try:
        result = run_backtest(_STORE, split_date='2025-01-01', max_rows=5000)
        return render_template('evaluation.html', result=result, error=None)

    except Exception as exc:
        import traceback
        log.exception("Evaluation failed")
        return render_template('evaluation.html',
                               error=str(exc),
                               result=None)


@app.route('/api/evaluation_csv')
def evaluation_csv():
    """Download full test-set evaluation as a CSV file."""
    if not _STORE.get('ready'):
        return jsonify({'status': 'error', 'message': 'Model not ready'}), 503
    try:
        path = build_csv(_STORE, split_date='2025-01-01')
        return send_file(
            path,
            as_attachment=True,
            download_name='powercast_evaluation.csv',
            mimetype='text/csv',
        )
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


# =============================================================================
# API — MODEL INFO
# =============================================================================

@app.route('/api/test')
def api_test():
    return jsonify({
        'status':      'success',
        'message':     'Flask API is working ✅',
        'model_ready': _STORE['ready'],
        'model_error': _STORE['error'],
        'timestamp':   str(datetime.now()),
    })


@app.route('/api/model_metrics')
def api_model_metrics():
    metrics = [
        {"model": "Linear Regression", "train_mape": 14.52, "test_mape": 16.31, "train_rmse": 820.10, "test_rmse": 910.45},
        {"model": "Decision Tree",     "train_mape": 10.21, "test_mape": 13.74, "train_rmse": 615.30, "test_rmse": 780.20},
        {"model": "Random Forest",     "train_mape":  7.85, "test_mape":  9.09, "train_rmse": 480.15, "test_rmse": 560.70},
        {"model": "XGBoost",           "train_mape":  6.90, "test_mape":  9.83, "train_rmse": 420.40, "test_rmse": 590.30},
        {"model": "LightGBM",          "train_mape":  7.10, "test_mape": 10.05, "train_rmse": 440.60, "test_rmse": 605.10},
    ]
    return jsonify(metrics)


@app.route('/api/feature_importance')
def api_feature_importance():
    features = [
        {"feature": "MCP_lag_96",       "importance": 0.312},
        {"feature": "MCP_lag_1",        "importance": 0.198},
        {"feature": "MCP_roll_mean_96", "importance": 0.154},
        {"feature": "hour",             "importance": 0.112},
        {"feature": "MCP_lag_2",        "importance": 0.078},
        {"feature": "MCP_roll_std_96",  "importance": 0.056},
        {"feature": "MCP_lag_672",      "importance": 0.042},
        {"feature": "weekday",          "importance": 0.031},
        {"feature": "is_peak_hour",     "importance": 0.011},
        {"feature": "is_weekend",       "importance": 0.006},
    ]
    return jsonify(features)


# =============================================================================
# API — CUSTOM DATE FORECAST
# =============================================================================

@app.route('/api/forecast_custom', methods=['POST'])
def api_forecast_custom():
    """
    POST { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" }

    Converts date range → n_blocks → runs existing fast/hybrid engine.
    Always forecasts forward from last dataset date.
    Large ranges (> FULL_MODEL_THRESHOLD blocks) automatically use hybrid.
    """
    if not _STORE['ready']:
        if _STORE['error']:
            return jsonify({'status': 'error', 'message': _STORE['error']}), 500
        return jsonify({'status': 'error',
                        'message': 'Model still loading — retry in a moment.'}), 503

    body       = request.get_json(silent=True) or {}
    start_date = body.get('start_date', '').strip()
    end_date   = body.get('end_date',   '').strip()

    if not start_date or not end_date:
        return jsonify({'status': 'error',
                        'message': 'Both start_date and end_date are required (YYYY-MM-DD).'}), 400

    # ── MONITORING: time the forecast ─────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        data = generate_forecast_by_date(start_date, end_date)

        # Record metrics for successful forecast
        duration = time.perf_counter() - t0
        record_forecast(horizon='custom', duration=duration)
        update_mcp_gauge(data.get('prices', []))

        return jsonify({'status': 'success', 'data': data})

    except ValueError as ve:
        record_error(endpoint='api_forecast_custom', error_type='ValueError')
        return jsonify({'status': 'error', 'message': str(ve)}), 400
    except Exception as exc:
        import traceback
        record_error(endpoint='api_forecast_custom', error_type=type(exc).__name__)
        log.exception("generate_forecast_by_date failed")
        return jsonify({'status': 'error', 'message': str(exc),
                        'traceback': traceback.format_exc()}), 500


@app.route('/api/model_info')
def api_model_info():
    """
    Returns dataset date bounds so the frontend can:
      - Show 'dataset ends on X' hint
      - Set date-picker min to day AFTER dataset end
      - Pre-fill default custom date range
    """
    if not _STORE['ready']:
        return jsonify({'status': 'error', 'message': 'Model not ready'}), 503

    last_dt     = _STORE['df']['datetime'].iloc[-1]
    # First selectable date = day after last dataset date
    min_select  = last_dt.normalize() + pd.Timedelta(days=1)

    return jsonify({
        'status':           'success',
        'dataset_end':      last_dt.strftime('%Y-%m-%d'),
        'dataset_end_fmt':  last_dt.strftime('%d %b %Y %H:%M'),
        'min_start_date':   min_select.strftime('%Y-%m-%d'),   # ← use this for date-picker min
        'model_ready':      True,
    })


# =============================================================================
# API — FORECAST  (main endpoint)
# =============================================================================

@app.route('/api/forecast', methods=['POST'])
def api_forecast():
    """
    POST  { "horizon": "daily" | "weekly" | "monthly" | "seasonal" }

    Daily / Weekly  → synchronous, returns result immediately
    Monthly / Seasonal → background job, returns job_id for polling
    """
    body    = request.get_json(silent=True) or {}
    horizon = body.get('horizon', 'daily')

    if horizon not in HORIZON_BLOCKS:
        return jsonify({'status': 'error',
                        'message': f'Unknown horizon: {horizon}'}), 400

    n_blocks = HORIZON_BLOCKS[horizon]

    # ── model still warming up? ───────────────────────────────────────────────
    if not _STORE['ready']:
        if _STORE['error']:
            return jsonify({'status': 'error', 'message': _STORE['error']}), 500
        return jsonify({'status': 'error',
                        'message': 'Model is still loading — wait a few seconds and retry'}), 503

    # ── Monthly / Seasonal → background thread ───────────────────────────────
    if horizon in BG_HORIZONS:
        try:
            job_id = submit_background_forecast(n_blocks, horizon)
            # Count the submission (duration tracked when job completes)
            record_forecast(horizon=horizon, duration=0)
            return jsonify({
                'status':  'accepted',
                'job_id':  job_id,
                'horizon': horizon,
                'message': f'{horizon.capitalize()} forecast started in background. Poll /api/job/{job_id}',
            }), 202

        except Exception as exc:
            record_error(endpoint='api_forecast', error_type=type(exc).__name__)
            log.exception("submit_background_forecast failed")
            return jsonify({'status': 'error', 'message': str(exc)}), 500

    # ── Daily / Weekly → synchronous ─────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        data = generate_forecast(n_blocks, horizon)

        # Record metrics for successful forecast
        duration = time.perf_counter() - t0
        record_forecast(horizon=horizon, duration=duration)
        update_mcp_gauge(data.get('prices', []))

        return jsonify({'status': 'success', 'data': data})

    except Exception as exc:
        import traceback
        record_error(endpoint='api_forecast', error_type=type(exc).__name__)
        log.exception("generate_forecast failed")
        return jsonify({
            'status':    'error',
            'message':   str(exc),
            'traceback': traceback.format_exc(),
        }), 500


# =============================================================================
# API — JOB POLLING  (for Monthly / Seasonal background jobs)
# =============================================================================

@app.route('/api/job/<job_id>')
def api_job_status(job_id):
    """
    Poll this endpoint every 3 seconds until status == 'done' or 'error'.
    Response when running:  { status: 'running' }
    Response when done:     { status: 'done', data: { ... } }
    Response when error:    { status: 'error', error: '...' }
    """
    job = get_job_status(job_id)

    if job['status'] == 'not_found':
        return jsonify({'status': 'error', 'message': 'Job not found'}), 404

    if job['status'] == 'done':
        return jsonify({'status': 'success', 'data': job['result']})

    if job['status'] == 'error':
        return jsonify({'status': 'error', 'message': job.get('error', 'Unknown error')}), 500

    # still running
    return jsonify({'status': 'running', 'horizon': job.get('horizon'), 'n_blocks': job.get('n_blocks')})


# =============================================================================
# API — DEBUG / QUICK TEST
# =============================================================================

@app.route('/api/forecast_test')
def api_forecast_test():
    """Browser-accessible GET test — always runs daily (96 blocks)."""
    try:
        data = generate_forecast(96, 'daily')
        return jsonify({'status': 'success', 'data': data})
    except Exception as exc:
        import traceback
        return jsonify({'status': 'error', 'message': str(exc),
                        'traceback': traceback.format_exc()}), 500


# =============================================================================
# API — DOWNLOAD
# =============================================================================

@app.route('/api/download_forecast/<horizon>')
def download_forecast(horizon):
    try:
        file_path = generate_forecast_excel(horizon)
        return send_file(
            file_path,
            as_attachment=True,
            download_name=f'{horizon}_forecast.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


# =============================================================================
# API — UPLOAD (kept for compatibility, no longer required)
# =============================================================================

@app.route('/api/upload_dataset', methods=['POST'])
def upload_dataset():
    return jsonify({'status': 'info',
                    'message': 'Upload not needed — dataset is read directly from disk.'})


# =============================================================================
# RUN
# =============================================================================

if __name__ == '__main__':
    os.makedirs(app.config['DATA_FOLDER'], exist_ok=True)
    # use_reloader=False → prevents warm_up from running twice
    app.run(debug=True, port=5000, use_reloader=False, threaded=True)
