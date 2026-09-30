# C-Lab (Charlie's Lab)

Charlie's personal investing tracker. This app is separate from C.T.H.A.I (the business tool) and has its own logins and its own database.

```
backend/    FastAPI + SQLAlchemy on AWS Lambda (SAM, eu-west-1)
frontend/   Plain HTML/CSS/JS on Cloudflare Pages (no build step)
```

## What it does

- **Markets:** USD/ZAR, EUR/ZAR and GBP/ZAR, the JSE Top 40, SA property, gold and the S&P 500, with day, month and year moves.
- **Portfolio:** holdings from buys, sells and dividends (average cost), cash, gain and return, and where the money sits.
  - **Benchmark comparison:** what the same money would be worth if it had gone into the Satrix 40, the Satrix Property ETF or US dollars on the same days.
- **Holdings:** entered by hand or imported from CSV. Anything without a market price (e.g. EasyProperties) can be priced by hand.
- **Watchlist:** JSE shares and REITs with moves, dividend yield and 52-week range. Price alerts are emailed nightly at 18:00 SAST.
- **Property:** value, bond, equity, loan-to-value, gross and net yield, monthly cash flow and growth per year.

Prices come from Yahoo's public chart feed. JSE quotes arrive in cents and are shown in rand.

**Sign-up:** the first account created owns the app. After that, only emails listed in `SIGNUP_EMAILS` can sign up.

This is a tracker, not financial advice.

## Local dev

```bash
cd backend && pip install -r requirements-dev.txt && cp .env.example .env
uvicorn app.main:app --reload --port 8001
python -m pytest -q
cd ../frontend && npx wrangler pages dev . --port 8789
```

## Deploy

Every push to `main` runs `.github/workflows/deploy.yml`. It runs the tests, deploys the SAM stack `c-lab-api` to eu-west-1 and the Pages project `c-lab`, then smoke-tests the API.

**Repo secrets:**
- Required: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLAB_DATABASE_URL`. `CLAB_DATABASE_URL` should point to its **own** Neon database.
- Optional, for alert emails: `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`.

**Optional repo variable:** `SIGNUP_EMAILS`.

The app secret key is created on the first deploy and kept in AWS SSM at `/c-lab/secret-key`.
