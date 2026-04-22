"""
utils/helpers.py
================
Shared utility functions used across the Flask app.
"""

import numpy as np
import pandas as pd


def mape(y_true, y_pred):
    """Mean Absolute Percentage Error"""
    y_true, y_pred = np.array(y_true), np.array(y_pred)
    mask = y_true != 0
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def rmse(y_true, y_pred):
    """Root Mean Squared Error"""
    return float(np.sqrt(np.mean((np.array(y_true) - np.array(y_pred)) ** 2)))


def format_inr(value: float) -> str:
    """Format a float as Indian Rupees string."""
    return f"₹{value:,.2f}"


def horizon_label(horizon: str) -> dict:
    """Return display metadata for a given horizon key."""
    meta = {
        'daily':    {'label': 'Daily',    'days': 1,  'blocks': 96},
        'weekly':   {'label': 'Weekly',   'days': 7,  'blocks': 672},
        'monthly':  {'label': 'Monthly',  'days': 30, 'blocks': 2880},
        'seasonal': {'label': 'Seasonal', 'days': 90, 'blocks': 8640},
    }
    return meta.get(horizon, meta['daily'])
