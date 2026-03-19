# Angel One Algo Trading Bot — Render Deployment

## Files
- `app.py`          — Flask app + trading engine + dashboard UI
- `requirements.txt`— Python dependencies
- `render.yaml`     — Render deployment config

## Deploy to Render in 5 Steps

1. Push these files to a GitHub repo (public or private)

2. Go to https://render.com → New → Web Service → connect your repo

3. Render auto-detects `render.yaml` — confirm settings

4. In Render dashboard → Environment → Add these variables:

   | Variable           | Value                        |
   |--------------------|------------------------------|
   | DASHBOARD_PASSWORD | your_secret_password         |
   | ANGEL_API_KEY      | from smartapi.angelone.in    |
   | ANGEL_CLIENT_CODE  | your Angel One client ID     |
   | ANGEL_PASSWORD     | your Angel One MPIN          |
   | ANGEL_TOTP_TOKEN   | TOTP secret from QR scan     |

5. Deploy → visit your Render URL → login with DASHBOARD_PASSWORD

## Get Angel One SmartAPI Credentials

1. Open demat account at angelone.in (free)
2. Go to smartapi.angelone.in → Login → Create App
3. Note your API Key
4. In Angel One mobile app: Settings → Security → Enable TOTP
5. Scan the QR with Google Authenticator — also copy the SECRET TEXT shown
6. That secret text = ANGEL_TOTP_TOKEN

## Strategy (Max Win / Min Loss)

Signals (need 4/6 to trade):
- Triple EMA alignment (9/21/50)
- RSI with momentum confirmation
- VWAP institutional flow
- Volume surge >140% average
- ATR volatility filter
- Supertrend direction

Risk Rules:
- 1% capital per trade (₹50 on ₹5K account)
- ₹200 min / ₹1000 max per trade
- 1:2.5 risk:reward ratio
- Trailing SL activates after 1× risk profit
- 3% daily loss = circuit breaker
- Auto square-off at 3:10 PM IST
- Max 2 concurrent positions
- Max 4 trades per day

## Small Account Mode
Default capital = ₹5,000
Trades low-price stocks: SBIN, ITC, YESBANK, SUZLON, IDEA, PNB, TATASTEEL, ONGC
Minimum qty = 1 share
