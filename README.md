# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out, manual corrections with an audit trail, and analytics.

## Setup

py -3.11 -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt

Copy `.env.example` to `.env` and set `MONGO_URI` / `MONGO_DB`.

## Run

uvicorn app.main:app --port 8000 --reload

Open http://127.0.0.1:8000/docs

## Notes

- All application code lives in `app/main.py`.
- Indexes are created at startup, idempotently.
- Real environment variables override `.env`.