# HROne Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Hiring assignment.

## Run
1. Install Python 3.11+ and MongoDB 6.0+.
2. Create a virtual environment and install dependencies:
   `python -m venv .venv`
   `pip install -r requirements.txt`
3. Set `MONGO_URI` and `MONGO_DB` in your environment (or `.env`; environment variables take precedence).
4. From this directory run: `uvicorn app.main:app --port 8000`
5. Check readiness at `GET /health`. Interactive API docs are at `/docs`.

The app creates indexes idempotently on startup. Do not commit `.env`, credentials, virtual environments, or database dumps.

## Notes
- All API instants are epoch milliseconds; MongoDB stores BSON UTC datetimes.
- Analytics use stored derived attendance fields.
- MongoDB 6.0+ is required for the aggregation stages used by the trend and leaderboard endpoints.
- `sample_seed.py` and `sample_data/` can be copied from the supplied candidate kit if available.
