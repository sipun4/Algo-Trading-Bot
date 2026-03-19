"""
Angel One SmartAPI — Production Algo Trading Bot
Flask web server with password-protected dashboard
Deploy on Render.com — all keys via environment variables
"""

import os, json, math, time, threading, logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from SmartApi import SmartConnect
import pyotp

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change_this_secret_key_in_render")

# ─── ENVIRONMENT VARIABLES (set in Render dashboard) ───
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")
ANGEL_API_KEY      = os.environ.get("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE  = os.environ.get("ANGEL_CLIENT_CODE", "")
ANGEL_PASSWORD     = os.environ.get("ANGEL_PASSWORD", "")
ANGEL_TOTP_TOKEN   = os.environ.get("ANGEL_TOTP_TOKEN", "")

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()])
log = logging.getLogger("ALGO_BOT")

# ─── SYMBOL MASTER (token → symbol mapping for small-cap & low-price stocks) ───
SYMBOLS = [
    {"name": "SBIN",      "token": "3045",  "exchange": "NSE", "min_qty": 1},
    {"name": "ITC",       "token": "1660",  "exchange": "NSE", "min_qty": 1},
    {"name": "TATASTEEL", "token": "3499",  "exchange": "NSE", "min_qty": 1},
    {"name": "ONGC",      "token": "2475",  "exchange": "NSE", "min_qty": 1},
    {"name": "IDEA",      "token": "14366", "exchange": "NSE", "min_qty": 1},
    {"name": "YESBANK",   "token": "11915", "exchange": "NSE", "min_qty": 1},
    {"name": "SUZLON",    "token": "19234", "exchange": "NSE", "min_qty": 1},
    {"name": "PNB",       "token": "2730",  "exchange": "NSE", "min_qty": 1},
]

# ─── BOT STATE ───
state = {
    "running": False,
    "smart": None,
    "auth_token": None,
    "capital": 5000,          # Start very small — ₹5,000
    "risk_per_trade_pct": 1,  # 1% = ₹50 per trade at ₹5K capital
    "min_trade_value": 200,   # Minimum ₹200 per trade
    "max_trade_value": 1000,  # Maximum ₹1,000 per trade (safety cap for small account)
    "sl_atr_mult": 1.5,
    "rr_ratio": 2.5,          # 1:2.5 risk reward — maximizes wins
    "min_signals": 4,
    "max_trades_day": 4,
    "daily_loss_pct": 3,
    "trail_sl_activate_rr": 1.0,  # Activate trailing SL after 1× risk profit
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "wins": 0,
    "losses": 0,
    "open_positions": {},
    "trade_history": [],
    "log_entries": [],
    "scan_thread": None,
    "connected": False,
    "last_scan": None,
    "market_open": False,
    "circuit_broken": False,
}

def add_log(level, tag, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"time": ts, "level": level, "tag": tag, "msg": msg}
    state["log_entries"].insert(0, entry)
    state["log_entries"] = state["log_entries"][:200]
    fn = getattr(log, level if level in ("info","warning","error") else "info")
    fn(f"[{tag}] {msg}")

# ─────────────────────────────────────────────────────────
#  AUTH — non-bypassable password gate
# ─────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET"])
def login_page():
    return render_template_string(LOGIN_HTML)

@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json()
    pwd  = (data or {}).get("password", "")
    if pwd == DASHBOARD_PASSWORD:
        session["authenticated"] = True
        session.permanent = False
        return jsonify({"ok": True})
    time.sleep(2)   # Slow brute-force
    return jsonify({"ok": False, "error": "Invalid password"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ─────────────────────────────────────────────────────────
#  ANGEL ONE CONNECTION
# ─────────────────────────────────────────────────────────
def connect_angel():
    if not all([ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD, ANGEL_TOTP_TOKEN]):
        add_log("error","API","Missing credentials — set env vars in Render")
        return False
    try:
        totp  = pyotp.TOTP(ANGEL_TOTP_TOKEN).now()
        smart = SmartConnect(api_key=ANGEL_API_KEY)
        data  = smart.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
        if not data.get("status"):
            add_log("error","API", f"Login failed: {data.get('message','Unknown error')}")
            return False
        state["smart"]      = smart
        state["auth_token"] = data["data"]["jwtToken"]
        state["connected"]  = True
        refresh             = data["data"]["refreshToken"]
        smart.generateToken(refresh)
        add_log("info","API",f"Connected to Angel One | Client: {ANGEL_CLIENT_CODE}")
        return True
    except Exception as e:
        add_log("error","API",f"Connection error: {e}")
        return False

def get_ltp(symbol, token, exchange):
    try:
        resp = state["smart"].ltpData(exchange, symbol + "-EQ", token)
        if resp.get("status"):
            return float(resp["data"]["ltp"])
    except Exception as e:
        add_log("warning","LTP",f"{symbol} LTP error: {e}")
    return None

def get_candles(token, exchange, interval="FIFTEEN_MINUTE", days=15):
    try:
        to_dt   = datetime.now().strftime("%Y-%m-%d %H:%M")
        from_dt = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        resp = state["smart"].getCandleData({
            "exchange": exchange, "symboltoken": token,
            "interval": interval, "fromdate": from_dt, "todate": to_dt
        })
        if resp.get("status") and resp.get("data"):
            import pandas as pd
            df = pd.DataFrame(resp["data"], columns=["ts","open","high","low","close","volume"])
            df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":int})
            return df
    except Exception as e:
        add_log("warning","DATA",f"Candle fetch error: {e}")
    return None

# ─────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100/(1 + g/l.replace(0, 1e-9))

def atr(df, n=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    import pandas as pd
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, 1e-9)

def supertrend(df, period=7, mult=3.0):
    a = atr(df, period)
    mid = (df["high"] + df["low"]) / 2
    up  = mid + mult * a
    dn  = mid - mult * a
    direction = [1] * len(df)
    for i in range(1, len(df)):
        direction[i] = 1 if df["close"].iloc[i] > up.iloc[i-1] else \
                       -1 if df["close"].iloc[i] < dn.iloc[i-1] else direction[i-1]
    import pandas as pd
    return pd.Series(direction, index=df.index)

# ─────────────────────────────────────────────────────────
#  MAX WIN / MIN LOSS — Core Strategy Engine
#  Philosophy: only enter when ALL conditions align perfectly
#  Use trailing SL to lock profits, never let a winner turn loser
# ─────────────────────────────────────────────────────────
def analyze(df):
    if df is None or len(df) < 30:
        return "NONE", 0, {}

    c = df["close"]
    e9  = ema(c, 9);  e21 = ema(c, 21);  e50 = ema(c, 50)
    r   = rsi(c, 14)
    a14 = atr(df, 14)
    vw  = vwap(df)
    st  = supertrend(df)

    price   = c.iloc[-1]
    atr_val = a14.iloc[-1]
    rsi_val = r.iloc[-1]

    # ── Signal 1: Triple EMA alignment (strongest trend filter)
    triple_bull = e9.iloc[-1] > e21.iloc[-1] > e50.iloc[-1]
    triple_bear = e9.iloc[-1] < e21.iloc[-1] < e50.iloc[-1]
    # EMA crossover in last 3 candles
    cross_up   = e9.iloc[-3] <= e21.iloc[-3] and e9.iloc[-1] > e21.iloc[-1]
    cross_down = e9.iloc[-3] >= e21.iloc[-3] and e9.iloc[-1] < e21.iloc[-1]

    # ── Signal 2: RSI with divergence check
    rsi_oversold  = rsi_val < 40 and r.iloc[-3] < r.iloc[-1]   # Rising from oversold
    rsi_overbought= rsi_val > 60 and r.iloc[-3] > r.iloc[-1]   # Falling from overbought
    rsi_mid_bull  = 40 <= rsi_val <= 65  # Healthy bullish zone
    rsi_mid_bear  = 35 <= rsi_val <= 60  # Healthy bearish zone

    # ── Signal 3: VWAP (institutional money flow)
    above_vwap    = price > vw.iloc[-1] and c.iloc[-2] > vw.iloc[-2]  # Sustained above
    below_vwap    = price < vw.iloc[-1] and c.iloc[-2] < vw.iloc[-2]

    # ── Signal 4: Volume confirmation
    vol_avg       = df["volume"].rolling(20).mean().iloc[-1]
    vol_surge     = df["volume"].iloc[-1] > vol_avg * 1.4
    vol_dry       = df["volume"].iloc[-1] < vol_avg * 0.8  # Low vol = weak move

    # ── Signal 5: ATR — enough volatility to make profit on small account
    atr_ok        = atr_val > price * 0.004   # At least 0.4% ATR
    atr_not_crazy = atr_val < price * 0.04    # Not more than 4% (too risky)

    # ── Signal 6: Supertrend
    st_bull = st.iloc[-1] == 1
    st_bear = st.iloc[-1] == -1

    # ── Candle pattern: Last candle body direction
    last_bull_candle = c.iloc[-1] > df["open"].iloc[-1]
    last_bear_candle = c.iloc[-1] < df["open"].iloc[-1]

    # ── BUY score: higher = more confident
    bull_score = sum([
        triple_bull or cross_up,
        rsi_oversold or (rsi_mid_bull and not rsi_overbought),
        above_vwap,
        vol_surge and not vol_dry,
        atr_ok and atr_not_crazy,
        st_bull,
    ])
    # ── SELL score
    bear_score = sum([
        triple_bear or cross_down,
        rsi_overbought or (rsi_mid_bear and not rsi_oversold),
        below_vwap,
        vol_surge and not vol_dry,
        atr_ok and atr_not_crazy,
        st_bear,
    ])

    details = {
        "price": round(price,2), "atr": round(atr_val,2),
        "rsi": round(rsi_val,2), "vwap": round(vw.iloc[-1],2),
        "ema9": round(e9.iloc[-1],2), "ema21": round(e21.iloc[-1],2),
        "ema50": round(e50.iloc[-1],2), "st_bull": bool(st_bull),
        "above_vwap": bool(above_vwap), "vol_surge": bool(vol_surge),
        "triple_bull": bool(triple_bull), "triple_bear": bool(triple_bear),
        "cross_up": bool(cross_up), "cross_down": bool(cross_down),
        "bull_score": bull_score, "bear_score": bear_score,
    }

    min_sig = state["min_signals"]
    if bull_score >= min_sig and bull_score > bear_score:
        return "BUY",  bull_score, details
    if bear_score >= min_sig and bear_score > bull_score:
        return "SELL", bear_score, details
    return "NONE", max(bull_score, bear_score), details

def calc_position(price, atr_val, direction):
    """
    Position sizing for SMALL accounts:
    Risk = capital × 1% but clamped between min_trade and max_trade
    SL   = ATR × multiplier
    Qty  = max(1, floor(risk / sl_distance))
    """
    risk_amt  = state["capital"] * (state["risk_per_trade_pct"] / 100)
    risk_amt  = max(state["min_trade_value"], min(state["max_trade_value"], risk_amt))
    sl_dist   = atr_val * state["sl_atr_mult"]
    if sl_dist < 0.5:
        sl_dist = price * 0.01    # Minimum 1% SL

    qty = max(1, math.floor(risk_amt / sl_dist))
    sl  = round(price - sl_dist, 2) if direction == "BUY" else round(price + sl_dist, 2)
    tgt = round(price + sl_dist * state["rr_ratio"], 2) if direction == "BUY" \
          else round(price - sl_dist * state["rr_ratio"], 2)
    # Trailing SL threshold
    trail_activate = round(price + sl_dist * state["trail_sl_activate_rr"], 2) if direction == "BUY" \
                     else round(price - sl_dist * state["trail_sl_activate_rr"], 2)
    return qty, sl, tgt, trail_activate

# ─────────────────────────────────────────────────────────
#  ORDER EXECUTION — Angel One SmartAPI exact params
# ─────────────────────────────────────────────────────────
def place_order(symbol, token, exchange, side, qty, price=0, variety="NORMAL",
                order_type="MARKET", trigger=0):
    params = {
        "variety":         variety,
        "tradingsymbol":   symbol + "-EQ",
        "symboltoken":     token,
        "transactiontype": side,
        "exchange":        exchange,
        "ordertype":       order_type,
        "producttype":     "INTRADAY",
        "duration":        "DAY",
        "price":           str(round(price, 2)) if price else "0",
        "triggerprice":    str(round(trigger, 2)) if trigger else "0",
        "squareoff":       "0",
        "stoploss":        "0",
        "quantity":        str(qty),
    }
    try:
        resp = state["smart"].placeOrderFullResponse(params)
        if resp.get("status"):
            oid = resp["data"]["orderid"]
            add_log("info","ORDER",f"{side} {qty}×{symbol} @ ₹{price or 'MKT'} | ID:{oid}")
            return oid
        else:
            add_log("error","ORDER",f"FAILED {side} {symbol}: {resp.get('message','')}")
            return None
    except Exception as e:
        add_log("error","ORDER",f"Exception placing {symbol}: {e}")
        return None

def modify_sl_order(order_id, symbol, token, exchange, side, qty, new_trigger):
    """Modify SL order for trailing stop-loss."""
    params = {
        "variety":         "STOPLOSS",
        "orderid":         order_id,
        "tradingsymbol":   symbol + "-EQ",
        "symboltoken":     token,
        "transactiontype": side,
        "exchange":        exchange,
        "ordertype":       "STOPLOSS_MARKET",
        "producttype":     "INTRADAY",
        "duration":        "DAY",
        "price":           "0",
        "triggerprice":    str(round(new_trigger, 2)),
        "quantity":        str(qty),
    }
    try:
        resp = state["smart"].modifyOrder(params)
        if resp.get("status"):
            add_log("info","TSL",f"Trailing SL modified → ₹{new_trigger} for {symbol}")
            return True
    except Exception as e:
        add_log("warning","TSL",f"TSL modify error {symbol}: {e}")
    return False

def square_off_position(symbol):
    if symbol not in state["open_positions"]:
        return
    pos = state["open_positions"][symbol]
    ltp = get_ltp(symbol, pos["token"], pos["exchange"]) or pos["entry"]
    exit_side = "SELL" if pos["direction"] == "BUY" else "BUY"
    place_order(symbol, pos["token"], pos["exchange"], exit_side, pos["qty"])
    pnl = (ltp - pos["entry"]) * pos["qty"] if pos["direction"] == "BUY" \
          else (pos["entry"] - ltp) * pos["qty"]
    state["daily_pnl"] += pnl
    if pnl >= 0:
        state["wins"] += 1
        add_log("info","WIN",f"{symbol} closed +₹{pnl:.0f} | Daily P&L: ₹{state['daily_pnl']:+.0f}")
    else:
        state["losses"] += 1
        add_log("warning","LOSS",f"{symbol} closed ₹{pnl:.0f} | Daily P&L: ₹{state['daily_pnl']:+.0f}")
    state["trade_history"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol, "direction": pos["direction"],
        "entry": pos["entry"], "exit": round(ltp,2),
        "qty": pos["qty"], "pnl": round(pnl,2),
        "sl": pos["sl"], "target": pos["target"],
    })
    state["trade_history"] = state["trade_history"][:100]
    del state["open_positions"][symbol]

# ─────────────────────────────────────────────────────────
#  MARKET HOURS CHECK
# ─────────────────────────────────────────────────────────
def market_open():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9*60+30) <= t <= (15*60+10)

def eod_squareoff_time():
    now = datetime.now()
    t   = now.hour * 60 + now.minute
    return t >= (15*60+10)

# ─────────────────────────────────────────────────────────
#  MAIN SCAN LOOP
# ─────────────────────────────────────────────────────────
def scan_loop():
    while state["running"]:
        try:
            state["market_open"] = market_open()

            if eod_squareoff_time() and state["open_positions"]:
                add_log("warning","EOD","3:10 PM — squaring off all positions")
                for sym in list(state["open_positions"].keys()):
                    square_off_position(sym)

            if not state["market_open"]:
                time.sleep(30)
                continue

            max_loss = state["capital"] * (state["daily_loss_pct"] / 100)
            if state["daily_pnl"] < -max_loss:
                add_log("warning","CIRCUIT",f"Daily loss limit ₹{max_loss:.0f} hit — halting today")
                state["circuit_broken"] = True
                for sym in list(state["open_positions"].keys()):
                    square_off_position(sym)
                time.sleep(300)
                continue

            if state["daily_trades"] >= state["max_trades_day"]:
                add_log("info","RISK",f"Max {state['max_trades_day']} trades done today — monitoring only")
                _monitor_positions()
                time.sleep(60)
                continue

            state["last_scan"] = datetime.now().strftime("%H:%M:%S")
            add_log("info","SCAN",f"Scanning {len(SYMBOLS)} symbols | Open: {list(state['open_positions'].keys())} | "
                    f"Daily P&L: ₹{state['daily_pnl']:+.0f}")

            # Monitor & trail existing positions
            _monitor_positions()

            # Find new setups
            if len(state["open_positions"]) < 2:  # Max 2 concurrent
                for sym_info in SYMBOLS:
                    sym = sym_info["name"]
                    if sym in state["open_positions"]:
                        continue
                    df = get_candles(sym_info["token"], sym_info["exchange"])
                    signal, score, details = analyze(df)
                    add_log("info","ANALYZE",
                        f"{sym}: {signal} {score}/6 | RSI:{details.get('rsi','?')} "
                        f"EMA:{'↑' if details.get('triple_bull') else '↓'} "
                        f"VWAP:{'A' if details.get('above_vwap') else 'B'}")
                    if signal == "NONE":
                        continue
                    price   = details["price"]
                    atr_val = details["atr"]
                    qty, sl, tgt, trail_at = calc_position(price, atr_val, signal)
                    add_log("info","SIGNAL",
                        f"★ {signal} {sym} | Entry≈₹{price} SL:₹{sl} Target:₹{tgt} Qty:{qty}")
                    # Entry order (MARKET)
                    oid = place_order(sym, sym_info["token"], sym_info["exchange"],
                                      signal, qty, order_type="MARKET")
                    if oid:
                        # SL order
                        sl_side = "SELL" if signal == "BUY" else "BUY"
                        sl_oid  = place_order(sym, sym_info["token"], sym_info["exchange"],
                                              sl_side, qty,
                                              variety="STOPLOSS",
                                              order_type="STOPLOSS_MARKET",
                                              trigger=sl)
                        state["open_positions"][sym] = {
                            "token": sym_info["token"], "exchange": sym_info["exchange"],
                            "direction": signal, "entry": price, "qty": qty,
                            "sl": sl, "target": tgt, "trail_activate": trail_at,
                            "order_id": oid, "sl_order_id": sl_oid,
                            "trailing": False, "trail_sl": sl,
                            "time": datetime.now().strftime("%H:%M:%S"),
                        }
                        state["daily_trades"] += 1
                    time.sleep(1)

        except Exception as e:
            add_log("error","LOOP",f"Scan error: {e}")

        time.sleep(60)   # Scan every 60 seconds

def _monitor_positions():
    """Check SL/Target hits and manage trailing stop-loss."""
    for sym in list(state["open_positions"].keys()):
        pos = state["open_positions"][sym]
        ltp = get_ltp(sym, pos["token"], pos["exchange"])
        if ltp is None:
            continue

        d   = pos["direction"]
        hit_tgt = (d=="BUY" and ltp>=pos["target"]) or (d=="SELL" and ltp<=pos["target"])
        hit_sl  = (d=="BUY" and ltp<=pos["trail_sl"]) or (d=="SELL" and ltp>=pos["trail_sl"])

        # Activate trailing SL
        trail_cond = (d=="BUY" and ltp>=pos["trail_activate"]) or \
                     (d=="SELL" and ltp<=pos["trail_activate"])
        if trail_cond and not pos["trailing"]:
            pos["trailing"] = True
            add_log("info","TSL",f"{sym} ★ Trailing SL activated at ₹{ltp}")

        # Move trailing SL
        if pos["trailing"]:
            atr_val = pos["sl"] if not state["smart"] else \
                      (pos["target"] - pos["entry"]) / state["rr_ratio"] / state["sl_atr_mult"]
            new_sl = round(ltp - atr_val * state["sl_atr_mult"], 2) if d=="BUY" \
                     else round(ltp + atr_val * state["sl_atr_mult"], 2)
            moved  = (d=="BUY" and new_sl > pos["trail_sl"]) or \
                     (d=="SELL" and new_sl < pos["trail_sl"])
            if moved:
                sl_side = "SELL" if d=="BUY" else "BUY"
                if pos.get("sl_order_id"):
                    modify_sl_order(pos["sl_order_id"], sym, pos["token"],
                                    pos["exchange"], sl_side, pos["qty"], new_sl)
                pos["trail_sl"] = new_sl

        if hit_tgt:
            add_log("info","TARGET",f"★ TARGET HIT: {sym} @ ₹{ltp}")
            square_off_position(sym)
        elif hit_sl:
            add_log("warning","SL",f"STOP-LOSS: {sym} @ ₹{ltp}")
            square_off_position(sym)

# ─────────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/status")
@login_required
def api_status():
    tot = state["wins"] + state["losses"]
    return jsonify({
        "running":     state["running"],
        "connected":   state["connected"],
        "market_open": state["market_open"],
        "circuit":     state["circuit_broken"],
        "capital":     state["capital"],
        "daily_pnl":   round(state["daily_pnl"],2),
        "daily_trades":state["daily_trades"],
        "wins":        state["wins"],
        "losses":      state["losses"],
        "win_rate":    round(state["wins"]/tot*100,1) if tot else 0,
        "open_positions": len(state["open_positions"]),
        "positions":   {k: {
            "direction":v["direction"],"entry":v["entry"],"sl":v["sl"],
            "target":v["target"],"trail_sl":v["trail_sl"],
            "trailing":v["trailing"],"qty":v["qty"],"time":v["time"]
        } for k,v in state["open_positions"].items()},
        "last_scan":   state["last_scan"],
        "max_loss_daily": round(state["capital"] * state["daily_loss_pct"]/100, 2),
    })

@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify(state["log_entries"][:80])

@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify(state["trade_history"][:50])

@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    if state["running"]:
        return jsonify({"ok":False,"error":"Already running"})
    if not state["connected"]:
        ok = connect_angel()
        if not ok:
            return jsonify({"ok":False,"error":"Angel One connection failed — check env vars"})
    # Apply config from request
    data = request.get_json() or {}
    for k in ["capital","risk_per_trade_pct","min_signals","max_trades_day",
              "sl_atr_mult","rr_ratio","min_trade_value","max_trade_value","daily_loss_pct"]:
        if k in data:
            try: state[k] = float(data[k])
            except: pass
    state["running"]       = True
    state["circuit_broken"]= False
    state["daily_pnl"]     = 0.0
    state["daily_trades"]  = 0
    state["scan_thread"]   = threading.Thread(target=scan_loop, daemon=True)
    state["scan_thread"].start()
    add_log("info","SYSTEM","★ BOT STARTED — LIVE MODE | Angel One SmartAPI")
    add_log("info","RISK",f"Capital: ₹{state['capital']:,.0f} | Risk/trade: {state['risk_per_trade_pct']}% | "
            f"Max loss/day: ₹{state['capital']*state['daily_loss_pct']/100:.0f}")
    return jsonify({"ok":True})

@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    state["running"] = False
    add_log("warning","SYSTEM","Bot stopped — squaring off all open positions")
    for sym in list(state["open_positions"].keys()):
        square_off_position(sym)
    return jsonify({"ok":True})

@app.route("/api/squareoff", methods=["POST"])
@login_required
def api_squareoff():
    sym = (request.get_json() or {}).get("symbol")
    if sym and sym in state["open_positions"]:
        square_off_position(sym)
        return jsonify({"ok":True})
    return jsonify({"ok":False,"error":"Symbol not in open positions"})

@app.route("/api/connect", methods=["POST"])
@login_required
def api_connect():
    ok = connect_angel()
    return jsonify({"ok":ok})

# ─────────────────────────────────────────────────────────
#  HTML TEMPLATES
# ─────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Algo Bot — Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
     background:#0a0f1a;font-family:'Courier New',monospace}
.card{background:#0f1623;border:1px solid #1e3a5f;border-radius:12px;padding:40px 36px;
      width:340px;text-align:center;box-shadow:0 0 40px rgba(24,95,165,.15)}
.logo{font-size:22px;font-weight:700;color:#59a6f0;letter-spacing:.1em;margin-bottom:4px}
.sub{font-size:11px;color:#4a6fa5;margin-bottom:32px;letter-spacing:.06em}
.field{width:100%;background:#080d14;border:1px solid #1e3a5f;border-radius:6px;
       padding:12px 14px;color:#c8d8f0;font-family:'Courier New',monospace;font-size:14px;
       margin-bottom:14px;outline:none;transition:border .2s}
.field:focus{border-color:#59a6f0}
.btn{width:100%;background:#185FA5;color:#e6f1fb;border:none;border-radius:6px;
     padding:13px;font-size:14px;font-family:'Courier New',monospace;cursor:pointer;
     letter-spacing:.06em;transition:background .2s}
.btn:hover{background:#0c447c}
.err{color:#e24b4a;font-size:12px;margin-top:10px;display:none}
.shield{font-size:36px;margin-bottom:16px}
</style>
</head>
<body>
<div class="card">
  <div class="shield">🔐</div>
  <div class="logo">ALGO TRADING BOT</div>
  <div class="sub">ANGEL ONE · NSE/BSE · SECURE ACCESS</div>
  <input class="field" type="password" id="pwd" placeholder="Enter dashboard password"
         onkeydown="if(event.key==='Enter')login()">
  <button class="btn" onclick="login()">UNLOCK DASHBOARD</button>
  <div class="err" id="err">Incorrect password. Try again.</div>
</div>
<script>
async function login(){
  const pwd=document.getElementById('pwd').value;
  if(!pwd)return;
  const r=await fetch('/auth',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pwd})});
  const d=await r.json();
  if(d.ok){window.location.href='/';}
  else{const e=document.getElementById('err');e.style.display='block';
       setTimeout(()=>e.style.display='none',3000);}
}
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Angel One Algo Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#080d14;color:#c8d8f0;font-family:'Courier New',monospace;font-size:13px;min-height:100vh}
a{color:inherit;text-decoration:none}
.navbar{background:#0a0f1a;border-bottom:1px solid #1e3a5f;padding:10px 20px;
        display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.brand{font-size:14px;font-weight:700;color:#59a6f0;letter-spacing:.08em}
.brand-sub{font-size:10px;color:#4a6fa5;margin-top:1px}
.nav-right{display:flex;align-items:center;gap:16px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.dot.live{background:#3B6D11;animation:pulse 1.5s infinite}
.dot.paper{background:#BA7517}
.dot.off{background:#444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.status-txt{font-size:11px;color:#4a6fa5}
.logout{font-size:11px;color:#4a6fa5;cursor:pointer;padding:4px 10px;border:1px solid #1e3a5f;border-radius:4px}
.logout:hover{color:#e24b4a;border-color:#e24b4a}
.main{padding:16px;max-width:1200px;margin:0 auto}
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px}
.met{background:#0f1623;border:1px solid #1e3a5f;border-radius:8px;padding:12px}
.met-l{font-size:10px;color:#4a6fa5;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.met-v{font-size:18px;font-weight:700;color:#c8d8f0}
.met-v.g{color:#5fad2e}.met-v.r{color:#e24b4a}.met-v.b{color:#59a6f0}.met-v.y{color:#ef9f27}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.box{background:#0f1623;border:1px solid #1e3a5f;border-radius:8px;padding:14px}
.box-t{font-size:10px;font-weight:700;color:#4a6fa5;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
.cfg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cg label{font-size:10px;color:#4a6fa5;display:block;margin-bottom:3px}
.cg input,.cg select{width:100%;background:#080d14;border:1px solid #1e3a5f;border-radius:4px;
  padding:7px 9px;color:#c8d8f0;font-family:'Courier New',monospace;font-size:12px;outline:none}
.cg input:focus,.cg select:focus{border-color:#59a6f0}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.btn{padding:8px 16px;border-radius:5px;border:1px solid #1e3a5f;background:transparent;
     cursor:pointer;font-size:12px;font-family:'Courier New',monospace;color:#c8d8f0;transition:.2s}
.btn:hover{background:#1a2a3a}
.btn.go{background:#185FA5;color:#e6f1fb;border-color:#185FA5}.btn.go:hover{background:#0c447c}
.btn.stop{background:#7a1f1f;color:#f9c5c5;border-color:#7a1f1f}.btn.stop:hover{background:#a32d2d}
.btn.warn{background:#633806;color:#faeeda;border-color:#633806}.btn.warn:hover{background:#854f0b}
.btn:disabled{opacity:.4;cursor:not-allowed}
.log-box{background:#060a10;border:1px solid #132030;border-radius:6px;height:220px;overflow-y:auto;padding:8px;font-size:11px}
.le{padding:3px 0;border-bottom:1px solid #0d1820;line-height:1.6;display:flex;gap:6px}
.le:last-child{border:none}
.le .ts{color:#2a4a6a;min-width:56px}
.le .tag{font-weight:700;min-width:56px}
.le.info .tag{color:#59a6f0}
.le.warning .tag{color:#ef9f27}
.le.error .tag{color:#e24b4a}
.le.win .tag{color:#5fad2e}
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{text-align:left;color:#2a4a6a;padding:5px 8px;border-bottom:1px solid #0d1820;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.tbl td{padding:6px 8px;border-bottom:1px solid #0d1820;color:#c8d8f0}
.tbl tr:last-child td{border:none}
.badge{padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700}
.badge.BUY{background:#1a3a1a;color:#5fad2e}
.badge.SELL{background:#3a1a1a;color:#e24b4a}
.pnl-pos{color:#5fad2e}.pnl-neg{color:#e24b4a}
.pos-row{background:#060a10;border:1px solid #132030;border-radius:6px;padding:10px;margin-bottom:8px}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.pos-sym{font-size:14px;font-weight:700;color:#59a6f0}
.pos-meta{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;font-size:11px}
.pos-meta-item label{color:#2a4a6a;display:block;font-size:9px;text-transform:uppercase}
.bar-wrap{margin-top:8px}
.bar-lbl{display:flex;justify-content:space-between;font-size:10px;color:#2a4a6a;margin-bottom:3px}
.bar-track{height:4px;background:#0d1820;border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .5s}
.tab-row{display:flex;gap:4px;margin-bottom:10px;border-bottom:1px solid #132030;padding-bottom:8px}
.tab{font-size:11px;padding:4px 10px;border-radius:4px;cursor:pointer;border:none;
     background:transparent;color:#4a6fa5;font-family:'Courier New',monospace}
.tab.on{background:#1a2a3a;color:#59a6f0;font-weight:700}
.panel{display:none}.panel.on{display:block}
.alert{background:#1a0f0a;border:1px solid #7a3a1a;border-radius:6px;padding:10px;
       color:#ef9f27;font-size:11px;margin-bottom:10px;display:none}
.alert.show{display:block}
@media(max-width:768px){.metrics{grid-template-columns:repeat(3,1fr)}.grid2{grid-template-columns:1fr}}
</style>
</head>
<body>

<div class="navbar">
  <div>
    <div class="brand">ANGEL ONE ALGO BOT</div>
    <div class="brand-sub">NSE/BSE · INTRADAY · LIVE TRADING</div>
  </div>
  <div class="nav-right">
    <div><span class="dot off" id="st-dot"></span><span class="status-txt" id="st-txt">Idle</span></div>
    <div style="font-size:11px;color:#2a4a6a" id="clock">--:--:-- IST</div>
    <a href="/logout" class="logout">Logout</a>
  </div>
</div>

<div class="main">

  <div class="alert" id="circuit-alert">
    ⚠ CIRCUIT BREAKER ACTIVE — Daily loss limit reached. Bot halted for today.
  </div>

  <div class="metrics">
    <div class="met"><div class="met-l">Capital</div><div class="met-v b" id="m-cap">—</div></div>
    <div class="met"><div class="met-l">Today P&L</div><div class="met-v" id="m-pnl">₹0</div></div>
    <div class="met"><div class="met-l">Win Rate</div><div class="met-v" id="m-wr">—</div></div>
    <div class="met"><div class="met-l">W / L</div><div class="met-v" id="m-wl">0 / 0</div></div>
    <div class="met"><div class="met-l">Trades Today</div><div class="met-v y" id="m-tr">0</div></div>
    <div class="met"><div class="met-l">Open Pos.</div><div class="met-v b" id="m-op">0</div></div>
  </div>

  <div class="grid2">

    <div class="box">
      <div class="box-t">⚙ Bot Configuration</div>
      <div class="cfg">
        <div class="cg"><label>Capital (₹)</label>
          <input type="number" id="cfg-cap" value="5000" min="500" step="500"></div>
        <div class="cg"><label>Min Trade Value (₹)</label>
          <input type="number" id="cfg-min-trade" value="200" min="100"></div>
        <div class="cg"><label>Max Trade Value (₹)</label>
          <input type="number" id="cfg-max-trade" value="1000" min="200"></div>
        <div class="cg"><label>Risk Per Trade %</label>
          <input type="number" id="cfg-risk" value="1" min="0.5" max="2" step="0.5"></div>
        <div class="cg"><label>SL × ATR Multiplier</label>
          <input type="number" id="cfg-sl" value="1.5" min="1" max="3" step="0.5"></div>
        <div class="cg"><label>Risk:Reward Ratio</label>
          <input type="number" id="cfg-rr" value="2.5" min="1.5" max="5" step="0.5"></div>
        <div class="cg"><label>Min Signals (of 6)</label>
          <select id="cfg-sig"><option>3</option><option selected>4</option><option>5</option><option>6</option></select></div>
        <div class="cg"><label>Max Trades/Day</label>
          <input type="number" id="cfg-mt" value="4" min="1" max="8"></div>
        <div class="cg"><label>Daily Loss Limit %</label>
          <input type="number" id="cfg-dl" value="3" min="1" max="5" step="0.5"></div>
      </div>
      <div class="btn-row">
        <button class="btn go" id="btn-start" onclick="startBot()">▶ START BOT</button>
        <button class="btn stop" id="btn-stop" onclick="stopBot()" disabled>■ STOP</button>
        <button class="btn" onclick="connectApi()">⚡ Connect API</button>
      </div>
    </div>

    <div class="box">
      <div class="box-t">📊 Open Positions</div>
      <div id="positions-wrap">
        <div style="color:#2a4a6a;font-size:12px;padding:20px 0;text-align:center">No open positions</div>
      </div>
      <div class="box-t" style="margin-top:12px">📉 Risk Gauges</div>
      <div class="bar-wrap">
        <div class="bar-lbl"><span>Daily Loss</span><span id="r1v">₹0 / ₹0</span></div>
        <div class="bar-track"><div class="bar-fill" id="r1b" style="width:0%;background:#e24b4a"></div></div>
      </div>
      <div class="bar-wrap" style="margin-top:8px">
        <div class="bar-lbl"><span>Trades Used</span><span id="r2v">0 / 4</span></div>
        <div class="bar-track"><div class="bar-fill" id="r2b" style="width:0%;background:#59a6f0"></div></div>
      </div>
      <div class="bar-wrap" style="margin-top:8px">
        <div class="bar-lbl"><span>Win Rate</span><span id="r3v">0%</span></div>
        <div class="bar-track"><div class="bar-fill" id="r3b" style="width:0%;background:#5fad2e"></div></div>
      </div>
    </div>

  </div>

  <div class="box">
    <div class="tab-row">
      <button class="tab on" onclick="showTab('log',this)">Activity Log</button>
      <button class="tab" onclick="showTab('trades',this)">Trade History</button>
    </div>
    <div class="panel on" id="panel-log">
      <div class="log-box" id="log-box">
        <div class="le info"><span class="ts">--:--:--</span><span class="tag">[SYSTEM]</span><span>Dashboard loaded — configure and press START</span></div>
      </div>
    </div>
    <div class="panel" id="panel-trades">
      <table class="tbl">
        <thead><tr><th>Time</th><th>Symbol</th><th>Dir</th><th>Entry ₹</th><th>Exit ₹</th><th>SL ₹</th><th>Target ₹</th><th>Qty</th><th>P&L ₹</th></tr></thead>
        <tbody id="trade-tbody"><tr><td colspan="9" style="color:#2a4a6a;padding:14px 8px">No trades yet</td></tr></tbody>
      </table>
    </div>
  </div>

</div>

<script>
function showTab(id,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.getElementById('panel-'+id).classList.add('on');
  el.classList.add('on');
}

async function startBot(){
  const cfg={
    capital:      +document.getElementById('cfg-cap').value,
    min_trade_value: +document.getElementById('cfg-min-trade').value,
    max_trade_value: +document.getElementById('cfg-max-trade').value,
    risk_per_trade_pct: +document.getElementById('cfg-risk').value,
    sl_atr_mult:  +document.getElementById('cfg-sl').value,
    rr_ratio:     +document.getElementById('cfg-rr').value,
    min_signals:  +document.getElementById('cfg-sig').value,
    max_trades_day:+document.getElementById('cfg-mt').value,
    daily_loss_pct:+document.getElementById('cfg-dl').value,
  };
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  const d=await r.json();
  if(!d.ok) alert('Error: '+d.error);
}

async function stopBot(){
  if(!confirm('Stop bot and square off all positions?')) return;
  await fetch('/api/stop',{method:'POST'});
}

async function connectApi(){
  const r=await fetch('/api/connect',{method:'POST'});
  const d=await r.json();
  alert(d.ok?'Connected to Angel One!':'Connection failed — check Render env vars');
}

async function squareOff(sym){
  if(!confirm('Square off '+sym+'?')) return;
  await fetch('/api/squareoff',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
}

function fmt(n){return(n>=0?'+':'')+'\u20b9'+Math.abs(n).toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0})}

async function refreshStatus(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    // Status dot
    const dot=document.getElementById('st-dot');
    const txt=document.getElementById('st-txt');
    if(d.running){dot.className='dot live';txt.textContent='LIVE';}
    else{dot.className='dot off';txt.textContent='Stopped';}
    // Circuit breaker
    document.getElementById('circuit-alert').classList.toggle('show',d.circuit);
    // Metrics
    document.getElementById('m-cap').textContent='\u20b9'+d.capital.toLocaleString('en-IN');
    const pnlEl=document.getElementById('m-pnl');
    pnlEl.textContent=fmt(d.daily_pnl);
    pnlEl.className='met-v '+(d.daily_pnl>=0?'g':'r');
    const wrEl=document.getElementById('m-wr');
    wrEl.textContent=d.win_rate+'%';
    wrEl.className='met-v '+(d.win_rate>=55?'g':d.win_rate>=45?'y':'r');
    document.getElementById('m-wl').textContent=d.wins+' / '+d.losses;
    document.getElementById('m-tr').textContent=d.daily_trades;
    document.getElementById('m-op').textContent=d.open_positions;
    // Buttons
    document.getElementById('btn-start').disabled=d.running;
    document.getElementById('btn-stop').disabled=!d.running;
    // Risk bars
    const maxL=d.max_loss_daily;
    const lossUsed=Math.max(0,-d.daily_pnl);
    document.getElementById('r1v').textContent='\u20b9'+lossUsed.toFixed(0)+' / \u20b9'+maxL.toFixed(0);
    document.getElementById('r1b').style.width=Math.min(100,(lossUsed/maxL)*100)+'%';
    document.getElementById('r2v').textContent=d.daily_trades+' / '+d.max_trades_day||4;
    document.getElementById('r2b').style.width=Math.min(100,(d.daily_trades/(d.max_trades_day||4))*100)+'%';
    document.getElementById('r3v').textContent=d.win_rate+'%';
    document.getElementById('r3b').style.width=Math.min(100,d.win_rate)+'%';
    // Positions
    const posWrap=document.getElementById('positions-wrap');
    if(Object.keys(d.positions).length===0){
      posWrap.innerHTML='<div style="color:#2a4a6a;font-size:12px;padding:14px 0;text-align:center">No open positions</div>';
    } else {
      posWrap.innerHTML=Object.entries(d.positions).map(([sym,p])=>`
        <div class="pos-row">
          <div class="pos-head">
            <span class="pos-sym">${sym}</span>
            <span class="badge ${p.direction}">${p.direction}</span>
            <button class="btn warn" style="padding:4px 10px;font-size:10px" onclick="squareOff('${sym}')">Square Off</button>
          </div>
          <div class="pos-meta">
            <div class="pos-meta-item"><label>Entry</label>\u20b9${p.entry}</div>
            <div class="pos-meta-item"><label>${p.trailing?'Trail SL':'SL'}</label>\u20b9${p.trail_sl}</div>
            <div class="pos-meta-item"><label>Target</label>\u20b9${p.target}</div>
            <div class="pos-meta-item"><label>Qty</label>${p.qty}</div>
          </div>
          ${p.trailing?'<div style="color:#5fad2e;font-size:10px;margin-top:5px">★ TRAILING SL ACTIVE</div>':''}
        </div>`).join('');
    }
  } catch(e){console.error(e)}
}

async function refreshLogs(){
  try{
    const r=await fetch('/api/logs');
    const logs=await r.json();
    const box=document.getElementById('log-box');
    box.innerHTML=logs.map(l=>`
      <div class="le ${l.level}">
        <span class="ts">${l.time}</span>
        <span class="tag">[${l.tag}]</span>
        <span>${l.msg}</span>
      </div>`).join('');
  } catch(e){}
}

async function refreshTrades(){
  try{
    const r=await fetch('/api/trades');
    const trades=await r.json();
    const tb=document.getElementById('trade-tbody');
    if(!trades.length){tb.innerHTML='<tr><td colspan="9" style="color:#2a4a6a;padding:14px 8px">No trades yet</td></tr>';return;}
    tb.innerHTML=trades.map(t=>`<tr>
      <td>${t.time}</td>
      <td style="font-weight:700;color:#59a6f0">${t.symbol}</td>
      <td><span class="badge ${t.direction}">${t.direction}</span></td>
      <td>\u20b9${t.entry}</td><td>\u20b9${t.exit}</td>
      <td>\u20b9${t.sl}</td><td>\u20b9${t.target}</td><td>${t.qty}</td>
      <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}\u20b9${Math.abs(t.pnl)}</td>
    </tr>`).join('');
  } catch(e){}
}

// Clock
setInterval(()=>{
  document.getElementById('clock').textContent=
    new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false})+' IST';
},1000);

// Poll
setInterval(refreshStatus,4000);
setInterval(refreshLogs,3000);
setInterval(refreshTrades,8000);
refreshStatus();refreshLogs();
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
