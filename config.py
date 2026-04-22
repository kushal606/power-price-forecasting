"""
Configuration for Power Trading Forecast Flask App
"""

import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'power-trading-secret-2025')
    DATA_FOLDER = os.path.join(BASE_DIR, 'data')
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB upload limit
    DEBUG = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
