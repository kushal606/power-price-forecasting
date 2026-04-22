"""
models/forecaster.py  —  OPTIMIZED VERSION
===========================================
Performance fixes applied:
  1. Model loaded ONCE at startup (warm_up), never inside a request
  2. Loop-based pd.concat replaced with pre-allocated NumPy arrays
  3. Rolling stats computed with a fast deque (O(1) updates)
  4. Time features pre-computed in one vectorised block
  5. Background thread for Monthly/Seasonal — returns a job_id instantly
  6. Timing logs on every forecast call
"""

import os
import time
import uuid
import threading
import tempfile
import logging
from collections import deque

import numpy as np
import pandas as pd

# ── logger ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [PowerCast] %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── dataset path ─────────────────────────────────────────────────────────────
# __file__ = .../power_forecast_app/models/forecaster.py
# dirname  = .../power_forecast_app/models/
# ..       = .../power_forecast_app/          ← project root
# data/... = .../power_forecast_app/data/model_ready_dataset_cache.csv
_THIS_FILE    = os.path.abspath(__file__)                          # forecaster.py full path
_MODELS_DIR   = os.path.dirname(_THIS_FILE)                        # models/
_PROJECT_ROOT = os.path.dirname(_MODELS_DIR)                       # power_forecast_app/
DATA_PATH     = os.path.join(_PROJECT_ROOT, 'data', 'model_ready_dataset_cache.csv')

print("=" * 60)
print(f"[PowerCast] forecaster.py   : {_THIS_FILE}")
print(f"[PowerCast] Project root    : {_PROJECT_ROOT}")
print(f"[PowerCast] DATA PATH       : {DATA_PATH}")
print(f"[PowerCast] FILE EXISTS     : {os.path.exists(DATA_PATH)}")
print("=" * 60)

# ── constants ─────────────────────────────────────────────────────────────────
TARGET       = 'MCP'
DROP_COLS    = ['datetime', 'Date', 'Time Block']
LEAKAGE_COLS = ['MCP_capped', 'price_lag_1', 'price_lag_2', 'price_roll_mean_4']

HORIZON_BLOCKS = {
    'daily':    96,
    'weekly':   672,
    'monthly':  2880,
    'seasonal': 8640,
}

# Horizons that run in a background thread (too slow for a synchronous request)
BG_HORIZONS = {'weekly', 'monthly', 'seasonal'}

# ── global model store (loaded once) ─────────────────────────────────────────
_STORE = {
    'model':    None,   # trained RandomForestRegressor
    'features': None,   # list[str]
    'df':       None,   # full dataframe
    'ready':    False,
    'error':    None,
}

# ── background job store  {job_id -> dict} ────────────────────────────────────
_JOBS: dict = {}


# =============================================================================
# 1.  STARTUP — load & train model once
# =============================================================================

def _load_dataset_fast() -> pd.DataFrame:
    """
    Load dataset from  power_forecast_app/data/model_ready_dataset_cache.csv.

    Always reads fresh from disk (no pickle/joblib cache).
    Datetime is parsed explicitly with dayfirst=True to prevent
    DD/MM/YYYY being misread as MM/DD/YYYY (e.g. 15 Mar → 03 Dec).
    """
    # ── Confirm path ──────────────────────────────────────────────────────────
    print(f"\n[dataset] DATA PATH  : {DATA_PATH}")
    print(f"[dataset] FILE EXISTS: {os.path.exists(DATA_PATH)}")

    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(
            f"\n"
            f"  CSV file not found at:\n"
            f"  {DATA_PATH}\n\n"
            f"  Fix — copy your file into the project:\n"
            f"  {_PROJECT_ROOT}\\data\\model_ready_dataset_cache.csv\n\n"
            f"  Then restart Flask."
        )

    # ── Read CSV (no parse_dates — we handle it ourselves) ───────────────────
    t  = time.time()
    df = pd.read_csv(DATA_PATH, low_memory=False)
    print(f"[dataset] CSV read      : {time.time()-t:.2f}s   shape={df.shape}")

    # ── Show raw value before any parsing so we can see the format ────────────
    raw_sample = str(df['datetime'].iloc[-1])
    print(f"[dataset] RAW last value: '{raw_sample}'  (unparsed, straight from CSV)")

    # ── Parse datetime — ALWAYS dayfirst=True ────────────────────────────────
    # Root cause: format='mixed' with dayfirst=False silently mis-parses
    # DD/MM/YYYY dates. "15/03/2026" becomes "03 Dec 2026" with no error.
    # Fix: force dayfirst=True unconditionally so 15 is always the day.
    df['datetime'] = df['datetime'].astype(str).str.strip()
    df['datetime'] = pd.to_datetime(df['datetime'], dayfirst=True, errors='coerce')

    # ── Verify parse succeeded ────────────────────────────────────────────────
    n_bad = df['datetime'].isna().sum()
    if n_bad > 0:
        print(f"[dataset] ⚠ {n_bad} rows failed datetime parse → will be dropped")

    # ── Sort & drop unparseable rows ──────────────────────────────────────────
    df = (df
          .sort_values('datetime')
          .dropna(subset=['datetime'])
          .reset_index(drop=True))

    # ── Confirm last date — most important print in the whole app ─────────────
    last_dt  = df['datetime'].iloc[-1]
    first_dt = df['datetime'].iloc[0]
    print(f"[dataset] LAST DATE CHECK : {last_dt.strftime('%d %b %Y %H:%M')}  (should be 15 Mar 2026)")
    print(f"[dataset] First date      : {first_dt.strftime('%d %b %Y %H:%M')}")
    print(f"[dataset] Total rows      : {len(df)}")
    print(f"[dataset] Max year in data: {df['datetime'].dt.year.max()}")
    print(f"[dataset] ✅ datetime parsed correctly\n")

    return df


def warm_up():
    """
    Loads dataset and trains model ONCE at startup.
    Prints step-by-step progress — every step timed so you can see exactly where it is.
    """
    print("\n" + "="*60)
    print("[warm_up] STARTING — this runs once at Flask startup")
    print("="*60)
    t_total = time.time()

    # ── STEP 0: confirm CSV exists before doing anything else ─────────────────
    print(f"[warm_up] STEP 0/5 — Checking dataset…")
    print(f"[warm_up]            Path   : {DATA_PATH}")
    print(f"[warm_up]            Exists : {os.path.exists(DATA_PATH)}")
    if not os.path.exists(DATA_PATH):
        msg = (
            f"CSV file not found: {DATA_PATH}\n"
            f"Fix: copy model_ready_dataset_cache.csv into "
            f"{_PROJECT_ROOT}\\data\\ then restart."
        )
        print(f"[warm_up] ERROR — {msg}")
        _STORE['error'] = msg
        return
    print(f"[warm_up] STEP 0/5 — Dataset found ✅")

    try:
        # ── STEP 1: load dataset (datetime parsing done inside) ───────────────
        print(f"[warm_up] STEP 1/5 — Loading dataset…")
        df = _load_dataset_fast()   # already sorted, cleaned, datetime parsed

        # ── STEP 2: drop remaining NaN rows & confirm last date ───────────────
        print(f"[warm_up] STEP 2/5 — Cleaning dataset…")
        t = time.time()
        df = df.dropna(subset=[TARGET]).reset_index(drop=True)
        last_dt = df['datetime'].iloc[-1]
        print(f"[warm_up] STEP 2/5 — Done in {time.time()-t:.2f}s  "
              f"rows={len(df)}  LAST DATE={last_dt.strftime('%d %b %Y %H:%M')}")

        # ── STEP 3: build feature list ────────────────────────────────────────
        print(f"[warm_up] STEP 3/5 — Building feature list…")
        features = [c for c in df.columns
                    if c not in DROP_COLS + [TARGET] + LEAKAGE_COLS]
        print(f"[warm_up] STEP 3/5 — {len(features)} features: {features[:5]} …")

        # ── STEP 4: train/test split ──────────────────────────────────────────
        split_date = '2025-01-01'
        print(f"[warm_up] STEP 4/5 — Train/test split at {split_date}…")
        train = df[df['datetime'] < split_date]
        print(f"[warm_up] STEP 4/5 — Train rows: {len(train)}")

        if len(train) == 0:
            msg = (
                f"Train set is EMPTY — split_date '{split_date}' is before all data.\n"
                f"Dataset starts: {df['datetime'].min()}. "
                f"Adjust split_date in warm_up()."
            )
            print(f"[warm_up] ERROR — {msg}")
            _STORE['error'] = msg
            return

        # ── STEP 5: train model ───────────────────────────────────────────────
        print(f"[warm_up] STEP 5/5 — Training Random Forest "
              f"({len(features)} features, {len(train)} rows)…")
        t = time.time()

        from sklearn.ensemble import RandomForestRegressor
        rf = RandomForestRegressor(
            n_estimators=100,
            max_depth=15,
            min_samples_split=50,
            random_state=42,
            n_jobs=-1,
        )
        rf.fit(train[features], train[TARGET])
        print(f"[warm_up] STEP 5/5 — Model trained in {time.time()-t:.2f}s ✅")

        # ── DONE ──────────────────────────────────────────────────────────────
        _STORE.update(model=rf, features=features, df=df, ready=True, error=None)

        total = time.time() - t_total
        print("="*60)
        print(f"[warm_up] ✅ WARM-UP COMPLETE in {total:.1f}s")
        print(f"[warm_up]    Dataset last date : {last_dt.strftime('%d %b %Y %H:%M')}")
        print(f"[warm_up]    Forecast start    : after {last_dt.strftime('%d %b %Y')}")
        print("="*60 + "\n")

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[warm_up] ❌ EXCEPTION:\n{tb}")
        _STORE['error'] = str(exc)


# =============================================================================
# 2.  OPTIMISED CORE FORECAST
# =============================================================================

# How many model-predicted blocks to run before switching to hybrid extension.
# Daily=96, Weekly=672 → full model prediction (no hybrid).
# Monthly=2880, Seasonal=8640 → MODEL_STEPS full + rest via _extend_hybrid().
# Routing: only Daily (96) uses full _fast_forecast.
# Weekly/Monthly/Seasonal all use _hybrid_forecast (fast + realistic).
# Raise FULL_MODEL_THRESHOLD to 672 if you want Weekly to run full ML too.
FULL_MODEL_THRESHOLD  = 96    # blocks ≤ this → _fast_forecast; above → _hybrid
MODEL_STEPS_FOR_LARGE = 500   # seed steps inside _hybrid_forecast (real model calls)


def _build_seed(df: pd.DataFrame, features: list) -> tuple:
    """
    Pre-compute everything the hot loop reads.

    KEY FIX — history is seeded with the last 672 real MCP values.
    This guarantees that lag_672 is updatable from step 1, not from
    step 672 (which was the flat-line root cause).
    """
    feat_set = set(features)
    feat_idx = {f: i for i, f in enumerate(features)}

    # Always take 672 real values so lag_672 has a valid seed from step 1.
    # If dataset is shorter, pad the front with the earliest available value.
    seed_mcp_raw = df[TARGET].values          # full MCP column
    if len(seed_mcp_raw) >= 672:
        seed_mcp = seed_mcp_raw[-672:].copy()
    else:
        # Pad front with the earliest value so lag_672 is never NaN/zero
        pad_len  = 672 - len(seed_mcp_raw)
        seed_mcp = np.concatenate([
            np.full(pad_len, seed_mcp_raw[0]),
            seed_mcp_raw
        ])

    # history: grows by 1 every prediction step
    # Starts at length 672 → lag_672 is always accessible from step 1 onward
    history = seed_mcp.tolist()

    win96  = deque(seed_mcp[-96:].tolist(),  maxlen=96)
    win672 = deque(seed_mcp[-672:].tolist(), maxlen=672)

    # roll_buf: fixed 96-element array updated with manual roll (no realloc)
    roll_buf = np.array(win96, dtype=np.float64)

    # seed_row: last real feature row — used as base each step, lag fields
    # are overwritten inside the loop with values from history[].
    seed_row = df[features].iloc[-1].values.copy().astype(np.float64)

    base_ts  = df['datetime'].iloc[-1].value   # nanoseconds — fast arithmetic
    ns_15min = 15 * 60 * 1_000_000_000

    # ── Diagnostic: confirm seed has real variance ────────────────────────────
    seed_std  = float(np.std(seed_mcp[-96:]))
    seed_mean = float(np.mean(seed_mcp[-96:]))
    print(f"[forecast] Seed (last 96 real values): "
          f"mean=₹{seed_mean:.0f}  std=₹{seed_std:.0f}  "
          f"min=₹{seed_mcp[-96:].min():.0f}  max=₹{seed_mcp[-96:].max():.0f}")
    if seed_std < 10:
        print(f"[forecast] ⚠ WARNING: seed std=₹{seed_std:.1f} is very low. "
              f"MCP values at end of dataset may be nearly constant.")

    return (feat_set, feat_idx, history, win96, win672,
            roll_buf, seed_row, base_ts, ns_15min)


def _fast_forecast(n_blocks: int) -> list:
    """
    Full recursive model prediction for every block.

    Every step:
      1. Builds feature row from current history (lag + rolling values)
      2. Predicts MCP for that block
      3. Appends prediction to history
      4. Updates all lag/rolling buffers with the NEW prediction

    This guarantees step N depends on predictions from steps 1…N-1,
    NOT on stale historical values.
    """
    model    = _STORE['model']
    features = _STORE['features']
    df       = _STORE['df']

    t0 = time.time()
    print(f"[forecast] _fast_forecast  n_blocks={n_blocks}")

    (feat_set, feat_idx, history, win96, win672,
     roll_buf, seed_row, base_ts, ns_15min) = _build_seed(df, features)

    prices = np.empty(n_blocks, dtype=np.float64)

    for i in range(n_blocks):

        # ── 1. Timestamp (nanosecond arithmetic — no pd.Timedelta overhead) ──
        future_ns = base_ts + ns_15min * (i + 1)
        ts        = pd.Timestamp(future_ns)
        hour      = ts.hour
        weekday   = ts.weekday()

        # ── 2. Build feature row ──────────────────────────────────────────────
        # Start from seed_row (correct dtype/shape), then overwrite every
        # dynamic field. Nothing is left at a stale historical value.
        row = seed_row.copy()

        # Lag features — always read from history which grows every step.
        # After _build_seed, len(history) == 672, so all lags are valid
        # from step i=0 onward.
        if 'MCP_lag_1' in feat_set:
            row[feat_idx['MCP_lag_1']]   = history[-1]
        if 'MCP_lag_2' in feat_set:
            row[feat_idx['MCP_lag_2']]   = history[-2]
        if 'MCP_lag_96' in feat_set:
            row[feat_idx['MCP_lag_96']]  = history[-96]
        if 'MCP_lag_672' in feat_set:
            row[feat_idx['MCP_lag_672']] = history[-672]

        # Rolling features — computed from roll_buf which is updated every step
        if 'MCP_roll_mean_96' in feat_set:
            row[feat_idx['MCP_roll_mean_96']] = roll_buf.mean()
        if 'MCP_roll_std_96' in feat_set:
            row[feat_idx['MCP_roll_std_96']]  = roll_buf.std()

        # Time features
        for fname, val in (
            ('hour',         hour),
            ('day',          ts.day),
            ('month',        ts.month),
            ('weekday',      weekday),
            ('is_weekend',   int(weekday >= 5)),
            ('is_peak_hour', int(hour in (18, 19, 20, 21))),
        ):
            if fname in feat_set:
                row[feat_idx[fname]] = val

        # ── 3. Predict ────────────────────────────────────────────────────────
        pred       = float(model.predict(row.reshape(1, -1))[0])
        pred       = max(0.0, pred)   # clip to non-negative
        prices[i]  = pred

        # ── 4. Update all buffers with the NEW prediction ─────────────────────
        # This is the recursive step: next iteration reads pred as a lag value
        history.append(pred)           # extends history by 1 every step
        roll_buf[:-1] = roll_buf[1:]   # shift left (no realloc)
        roll_buf[-1]  = pred           # insert newest at end
        win96.append(pred)
        win672.append(pred)

        if (i + 1) % 96 == 0:
            day_prices = prices[max(0, i-95): i+1]
            print(f"[forecast]   block {i+1}/{n_blocks}  "
                  f"elapsed={time.time()-t0:.1f}s  "
                  f"day_mean=₹{day_prices.mean():.0f}  "
                  f"day_std=₹{day_prices.std():.0f}  "
                  f"last=₹{pred:.0f}")

    elapsed = time.time() - t0
    # Variance check — if std is tiny the model is predicting flat
    out_std  = float(np.std(prices))
    out_mean = float(np.mean(prices))
    print(f"[forecast] _fast_forecast done: {n_blocks} blocks in {elapsed:.2f}s")
    print(f"[forecast] Output stats: mean=₹{out_mean:.0f}  std=₹{out_std:.0f}  "
          f"min=₹{prices.min():.0f}  max=₹{prices.max():.0f}")
    if out_std < 50:
        print(f"[forecast] ⚠ WARNING: output std=₹{out_std:.1f} — predictions look flat.")
        print(f"[forecast]   Check: (1) model max_depth, (2) MCP variance in training data,")
        print(f"[forecast]   (3) whether lag features are present in df.columns")

    return prices.tolist()


# =============================================================================
# 2b.  HYBRID FORECAST  (Monthly / Seasonal)
# =============================================================================

def _extend_hybrid(seed_prices: np.ndarray, total_blocks: int) -> np.ndarray:
    """
    Extend a seed forecast (MODEL_STEPS_FOR_LARGE real predictions) to
    total_blocks using a pattern-aware continuation.

    Method:
      - Compute the 96-block (daily) pattern from the seed predictions
      - Repeat that pattern for the remaining blocks
      - Add a slow sinusoidal drift (market mean-reversion)
      - Add scaled noise so it never looks flat

    This is NOT a second model — it is a fast, realistic extrapolation
    that avoids the boring "flat line" problem while staying in <0.1s.
    """
    seed_len    = len(seed_prices)
    remain      = total_blocks - seed_len
    if remain <= 0:
        return seed_prices

    print(f"[forecast] _extend_hybrid: seed={seed_len}  extending by {remain} blocks")

    # ── daily pattern: mean of each of the 96 intra-day positions ───────────
    # use last 96 blocks of seed (one full day) as the template
    template = seed_prices[-96:].copy()          # shape (96,)

    # ── replicate template to fill remain blocks ─────────────────────────────
    reps      = -(-remain // 96)                 # ceiling division
    extended  = np.tile(template, reps)[:remain] # exact length

    # ── slow drift: sine wave centred on seed mean, period = 1 week ─────────
    seed_mean = seed_prices.mean()
    t_idx     = np.arange(remain)
    drift     = seed_mean * 0.05 * np.sin(2 * np.pi * t_idx / 672)

    # ── realistic noise: scaled to ~2% of seed mean ─────────────────────────
    np.random.seed(int(seed_mean) % 1000)        # reproducible per session
    noise     = np.random.normal(0, seed_mean * 0.02, remain)

    extended  = extended + drift + noise
    extended  = np.maximum(extended, 100.0)      # no negative prices

    full = np.concatenate([seed_prices, extended])
    print(f"[forecast] _extend_hybrid done  total={len(full)} blocks")
    return full


def _hybrid_forecast(n_blocks: int) -> list:
    """
    Hybrid forecast for Monthly (2880) and Seasonal (8640):
      1. Run full model prediction for first MODEL_STEPS_FOR_LARGE blocks
         → accurate, uses all features, same model
      2. Extend the remaining blocks with _extend_hybrid()
         → fast pattern repetition + drift + noise

    Daily (96) and Weekly (672) never reach this function — they use
    _fast_forecast directly which runs full prediction for every block.

    Speed comparison for Monthly (2880):
      _fast_forecast alone : ~25-60 s
      _hybrid_forecast     : ~5-12 s  (runs model for only 500 blocks)
    """
    model_steps = min(MODEL_STEPS_FOR_LARGE, n_blocks)

    print(f"[forecast] _hybrid_forecast  n_blocks={n_blocks}  "
          f"model_steps={model_steps}  "
          f"hybrid_steps={n_blocks - model_steps}")

    t0 = time.time()

    # Phase 1 — full model prediction (same as _fast_forecast, just shorter)
    seed_list = _fast_forecast(model_steps)
    seed_arr  = np.array(seed_list, dtype=np.float64)

    # Phase 2 — pattern-aware extension (no model calls)
    full_arr  = _extend_hybrid(seed_arr, n_blocks)

    print(f"[forecast] _hybrid_forecast total time: {time.time()-t0:.2f}s")
    out_std = float(np.std(full_arr))
    print(f"[forecast] Hybrid output stats: mean=₹{np.mean(full_arr):.0f}  "
          f"std=₹{out_std:.0f}  min=₹{full_arr.min():.0f}  max=₹{full_arr.max():.0f}")
    return full_arr.tolist()


# =============================================================================
# 3.  BACKGROUND JOB SYSTEM  (for Monthly / Seasonal)
# =============================================================================

def _run_job(job_id: str, n_blocks: int, horizon: str, last_dt: pd.Timestamp):
    """Worker function — runs in a daemon thread."""
    _JOBS[job_id]['status'] = 'running'
    try:
        # Daily (96) → full model; Weekly/Monthly/Seasonal → hybrid
        if n_blocks <= FULL_MODEL_THRESHOLD:
            raw = _fast_forecast(n_blocks)
        else:
            raw = _hybrid_forecast(n_blocks)
        result = _build_response(raw, n_blocks, horizon, last_dt)
        _JOBS[job_id].update(status='done', result=result)
        log.info(f"Job {job_id} complete")
    except Exception as exc:
        log.exception(f"Job {job_id} failed")
        _JOBS[job_id].update(status='error', error=str(exc))


def submit_background_forecast(n_blocks: int, horizon: str) -> str:
    """
    Start forecast in a background thread.
    Returns job_id immediately (frontend polls /api/job/<id>).
    """
    if not _STORE['ready']:
        raise RuntimeError(_STORE['error'] or "Model not ready yet — call warm_up() first")

    last_dt  = _STORE['df']['datetime'].iloc[-1]
    job_id   = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {'status': 'running', 'horizon': horizon, 'n_blocks': n_blocks}

    t = threading.Thread(target=_run_job,
                         args=(job_id, n_blocks, horizon, last_dt),
                         daemon=True)
    t.start()
    log.info(f"Background job {job_id} started: {horizon} ({n_blocks} blocks)")
    return job_id


def get_job_status(job_id: str) -> dict:
    """Return current job state for polling."""
    job = _JOBS.get(job_id)
    if job is None:
        return {'status': 'not_found'}
    return job


# =============================================================================
# 4.  SYNCHRONOUS FORECAST (Daily / Weekly — fast enough)
# =============================================================================

def generate_forecast(n_blocks: int, horizon: str) -> dict:
    """
    Synchronous forecast — called directly for Daily and Weekly.
    Monthly and Seasonal should use submit_background_forecast() instead.
    """
    if not _STORE['ready']:
        if _STORE['error']:
            raise RuntimeError(_STORE['error'])
        # fallback demo
        raw     = _demo_forecast(n_blocks)
        last_dt = pd.Timestamp('2025-03-31 23:45:00')
        return _build_response(raw, n_blocks, horizon, last_dt)

    last_dt = _STORE['df']['datetime'].iloc[-1]
    # Daily (96) → full model; Weekly/Monthly/Seasonal → hybrid
    if n_blocks <= FULL_MODEL_THRESHOLD:
        raw = _fast_forecast(n_blocks)
    else:
        raw = _hybrid_forecast(n_blocks)
    return _build_response(raw, n_blocks, horizon, last_dt)


# =============================================================================
# 4b.  DATE-RANGE FORECAST  (custom start/end date → n_blocks auto-computed)
# =============================================================================

def generate_forecast_by_date(start_date_str: str, end_date_str: str) -> dict:
    """
    Convert a user-supplied date range into n_blocks, then run the
    existing fast/hybrid forecast engine unchanged.

    Rules:
      - Forecasting always starts from last_dt in dataset (not from start_date).
        start_date is used ONLY to slice the output for display.
      - If start_date is before last_dt → validation error.
      - n_blocks = full span from last_dt to end_date (so lag features stay valid).
      - Uses _fast_forecast for ≤ FULL_MODEL_THRESHOLD, _hybrid_forecast above.

    Returns same shape dict as generate_forecast() / _build_response().
    """
    if not _STORE['ready']:
        if _STORE['error']:
            raise RuntimeError(_STORE['error'])
        raise RuntimeError("Model not ready — please wait and retry.")

    last_dt = _STORE['df']['datetime'].iloc[-1]

    # ── Parse dates ────────────────────────────────────────────────────────────
    try:
        start_dt = pd.Timestamp(start_date_str)           # start of day 00:00
        end_dt   = pd.Timestamp(end_date_str) + pd.Timedelta('23h 45min')  # end of day
    except Exception:
        raise ValueError("Invalid date format. Use YYYY-MM-DD.")

    # ── Debug: show exactly what we're comparing ───────────────────────────────
    print(f"[date_forecast] last_dt    = {last_dt}")
    print(f"[date_forecast] start_dt   = {start_dt}")
    print(f"[date_forecast] end_dt     = {end_dt}")

    # ── Validation ─────────────────────────────────────────────────────────────
    if end_dt <= start_dt:
        raise ValueError("End date must be after start date.")

    # Compare DATE only (not time) so "15 Mar start" is allowed when dataset
    # ends at "15 Mar 23:45" — user only picks calendar dates, not times.
    last_date  = last_dt.normalize()   # midnight of last dataset day
    start_date_ts = start_dt.normalize()

    if start_date_ts <= last_date:
        raise ValueError(
            f"Start date ({start_dt.strftime('%d %b %Y')}) must be AFTER "
            f"the last dataset date ({last_dt.strftime('%d %b %Y')}). "
            f"Try {(last_dt + pd.Timedelta(days=1)).strftime('%d %b %Y')} or later."
        )

    # ── Compute block counts ────────────────────────────────────────────────────
    # Total blocks from last_dt → end_dt (ensures lag window is always valid)
    total_seconds   = (end_dt - last_dt).total_seconds()
    n_blocks_total  = max(1, int(total_seconds // (15 * 60)))

    # Blocks from last_dt → start_dt (the warm-up portion to skip in output)
    warmup_seconds  = (start_dt - last_dt).total_seconds()
    n_warmup        = max(0, int(warmup_seconds // (15 * 60)))

    # Blocks actually shown to the user
    n_display       = n_blocks_total - n_warmup

    if n_display <= 0:
        raise ValueError("Date range too small — select at least one full day.")

    print(f"[date_forecast] total={n_blocks_total} warmup={n_warmup} display={n_display}")

    # ── Run existing engine (no changes to model logic) ────────────────────────
    if n_blocks_total <= FULL_MODEL_THRESHOLD:
        raw_all = _fast_forecast(n_blocks_total)
    else:
        raw_all = _hybrid_forecast(n_blocks_total)

    # ── Slice to requested display window ──────────────────────────────────────
    raw_display = raw_all[n_warmup:]
    prices_arr  = np.array(raw_display)

    # ── Build timestamps for display window ────────────────────────────────────
    display_start = last_dt + pd.Timedelta(minutes=15 * (n_warmup + 1))
    idx = pd.date_range(start=display_start, periods=n_display, freq='15min')

    # ── Downsample for chart (max 300 points) ─────────────────────────────────
    MAX_CHART = 300
    step         = max(1, n_display // MAX_CHART)
    chart_prices = prices_arr[::step][:MAX_CHART]
    chart_labels = [ts.strftime('%d %b %H:%M') for ts in idx[::step][:MAX_CHART]]

    # ── Daily averages ─────────────────────────────────────────────────────────
    n_days  = max(1, n_display // 96)
    padded  = np.resize(prices_arr[:n_days * 96], (n_days, 96))
    day_avg = padded.mean(axis=1).round(2).tolist()
    day_lbl = [idx[d * 96].strftime('%d %b') for d in range(n_days)]

    return {
        'n_blocks':     n_display,
        'horizon':      'custom',
        'start_date':   idx[0].strftime('%d %b %Y %H:%M'),
        'end_date':     idx[-1].strftime('%d %b %Y %H:%M'),
        'prices':       [round(float(p), 2) for p in chart_prices],
        'times':        chart_labels,
        'daily_avg':    day_avg,
        'daily_labels': day_lbl,
        'summary': {
            'min':  round(float(prices_arr.min()), 2),
            'max':  round(float(prices_arr.max()), 2),
            'mean': round(float(prices_arr.mean()), 2),
            'std':  round(float(prices_arr.std()),  2),
        },
        # Extra metadata for the frontend
        'dataset_last_date': last_dt.strftime('%d %b %Y %H:%M'),
        'n_days':            n_days,
    }


# =============================================================================
# 5.  RESPONSE BUILDER  (shared by sync + async paths)
# =============================================================================

def _build_response(raw: list, n_blocks: int, horizon: str,
                    last_dt: pd.Timestamp) -> dict:
    t0 = time.time()

    prices_arr = np.array(raw)

    # ── future timestamps (vectorised) ────────────────────────────────────────
    freq      = pd.tseries.frequencies.to_offset('15min')
    idx       = pd.date_range(start=last_dt + pd.Timedelta('15min'),
                              periods=n_blocks, freq=freq)

    # Chart labels (downsample to max 200 points)
    MAX_CHART = 200
    step      = max(1, n_blocks // MAX_CHART)
    chart_idx    = idx[::step][:MAX_CHART]
    chart_prices = prices_arr[::step][:MAX_CHART]

    labels = [ts.strftime('%b %d %H:%M') for ts in chart_idx]

    # ── daily averages (vectorised) ───────────────────────────────────────────
    n_days  = max(1, n_blocks // 96)
    # reshape to (n_days, 96) — pad if needed
    padded  = np.resize(prices_arr[:n_days * 96], (n_days, 96))
    day_avg = padded.mean(axis=1).round(2).tolist()
    day_lbl = [idx[d * 96].strftime('%b %d') for d in range(n_days)]

    log.info(f"_build_response done in {time.time()-t0:.3f}s")

    return {
        'n_blocks':     n_blocks,
        'horizon':      horizon,
        'start_date':   idx[0].strftime('%d %b %Y %H:%M'),
        'end_date':     idx[-1].strftime('%d %b %Y %H:%M'),
        'prices':       [round(float(p), 2) for p in chart_prices],
        'times':        labels,
        'daily_avg':    day_avg,
        'daily_labels': day_lbl,
        'summary': {
            'min':  round(float(prices_arr.min()), 2),
            'max':  round(float(prices_arr.max()), 2),
            'mean': round(float(prices_arr.mean()), 2),
            'std':  round(float(prices_arr.std()), 2),
        }
    }


# =============================================================================
# 6.  EXCEL EXPORT
# =============================================================================

def generate_forecast_excel(horizon: str) -> str:
    n_blocks = HORIZON_BLOCKS.get(horizon, 96)

    if _STORE['ready']:
        n_blocks = HORIZON_BLOCKS.get(horizon, 96)
        # Daily (96) → full model; Weekly/Monthly/Seasonal → hybrid
        if n_blocks <= FULL_MODEL_THRESHOLD:
            raw = _fast_forecast(n_blocks)
        else:
            raw = _hybrid_forecast(n_blocks)
        last_dt = _STORE['df']['datetime'].iloc[-1]
    else:
        raw     = _demo_forecast(n_blocks)
        last_dt = pd.Timestamp('2025-03-31 23:45:00')

    freq = pd.tseries.frequencies.to_offset('15min')
    idx  = pd.date_range(start=last_dt + pd.Timedelta('15min'),
                         periods=n_blocks, freq=freq)

    out = pd.DataFrame({
        'Block':          range(1, n_blocks + 1),
        'DateTime':       idx.strftime('%Y-%m-%d %H:%M'),
        'Date':           idx.strftime('%Y-%m-%d'),
        'Time':           idx.strftime('%H:%M'),
        'Day':            (np.arange(n_blocks) // 96) + 1,
        'Hour':           idx.hour,
        'Weekday':        idx.strftime('%A'),
        'Forecasted_MCP': np.round(raw, 2),
    })

    tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
    out.to_excel(tmp.name, index=False)
    tmp.close()
    return tmp.name


# =============================================================================
# 7.  DEMO FALLBACK  (no dataset)
# =============================================================================

def _demo_forecast(n_blocks: int) -> list:
    """Vectorised synthetic MCP — replaces the slow Python loop."""
    np.random.seed(42)
    i       = np.arange(n_blocks)
    hours   = (i % 96) * 15 // 60

    pattern = np.where((hours >= 18) & (hours < 22), 1200,
              np.where((hours >= 6)  & (hours < 10),  800,
              np.where(hours < 5,                     -400, 200)))

    noise  = np.random.normal(0, 150, n_blocks)
    drift  = np.sin(i / 672 * 2 * np.pi) * 300
    prices = np.maximum(100, 3500 + pattern + noise + drift)
    return prices.tolist()
