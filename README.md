# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the API implementation. It starts with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: starter review and implementation decisions.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
Requires Python 3.11+ and MongoDB 6.0+. Create and activate a virtual environment, then install the dependencies. In PowerShell:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

For local MongoDB, start a server (for example, with Docker):
```powershell
docker run --name attendance-mongo -p 27017:27017 -d mongo:7
```
The app defaults to `mongodb://localhost:27017` and database `attendance_db`. For Atlas, configure a database user,
allow your IP in Network Access, and use the Atlas cluster URI. Put the settings in a local `.env` at the repository root:
```dotenv
MONGO_URI=mongodb+srv://<user>:<url-encoded-password>@<cluster-host>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```
Environment variables override values in `.env`. Never commit `.env` or credentials. Atlas free tier (M0) works; the grader
uses MongoDB 7.0.

Start the API with `python -m uvicorn app.main:app --port 8000 --reload`. Open `http://localhost:8000/docs` for the API
or `http://localhost:8000/health` to check database readiness. To load sample data, run `python sample_seed.py` against a
disposable database only: the script clears both collections before inserting the sample records.

## Integration tests
The integration suite exercises analytics edge cases and concurrent writes against MongoDB. It deletes only its
`EMP98xx` fixtures, and refuses to run if its test database matches `MONGO_DB`. In PowerShell, choose a disposable
Atlas database name (38 characters maximum) and run:
```powershell
$env:ATTENDANCE_TEST_DB = "ck_tests_20261015"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
