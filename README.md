# ⚡ Power Price Forecasting System

## 📌 Overview
This project is an end-to-end data science application that predicts electricity prices using real-world market data and weather data.

It includes data processing, feature engineering, machine learning models, and a web-based dashboard for visualization.

---

## 🚀 Features
- 📊 Time-series forecasting (15-minute interval data)
- 🌦️ Integration of weather data from multiple cities
- 🧠 Machine Learning models (XGBoost, Regression)
- 📈 Interactive dashboard (Flask)
- 📡 Monitoring using Prometheus & Grafana
- 🐳 Docker-ready deployment

---

## 🛠️ Tech Stack
- Python
- Pandas, NumPy
- Scikit-learn, XGBoost
- Flask
- Prometheus & Grafana
- Docker

---

## 📂 Project Structure
## 📁 Project Structure
```bash
power-price-forecasting/
│
├── app.py                      # Flask routes & API endpoints
├── config.py                   # Environment & app configuration
├── monitoring.py               # Prometheus metrics
├── requirements.txt
├── prometheus.yml
├── grafana_dashboard.json
│
├── data/
├── models/
├── templates/
├── static/
├── utils/
```


---

## 📊 Dataset
Due to large size, dataset is not included in this repository.

It consists of:
- Electricity price data (IEX)
- Weather data (multiple cities)

---

## ⚙️ How to Run

```bash
git clone https://github.com/your-username/power-price-forecasting.git
cd power-price-forecasting

pip install -r requirements.txt
python app.py