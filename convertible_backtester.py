#!/usr/bin/env python3
"""
==============================================================================
  Convertible VWAP Backtester — Put-Strategy Simulator
==============================================================================

  Reads qualified hits from data/qualified_hits.json (produced by
  convertible_scanner.py), downloads intraday price data, and simulates
  a put-buying strategy around each VWAP pricing window.

  INSTALLATION:
      pip install -r requirements.txt

  USAGE:
      # Backtest all hits
      python convertible_backtester.py

      # Limit to first 10 hits
      python convertible_backtester.py --limit 10

      # Filter to a specific ticker
      python convertible_backtester.py --ticker MSTR

      # Save chart of biggest wins/losses
      python convertible_backtester.py --chart

      # Verbose per-trade logging
      python convertible_backtester.py --verbose

  TEST (with sample data):
      python convertible_backtester.py --limit 5 --verbose --chart

  DATA:
      Uses yfinance (free, no API key) for 5-min intraday OHLC.
      Yahoo provides ~60 days of 1m/2m data, ~2 years of 5m+.
      Filings older than ~60 days may only get daily bars as fallback.

  OUTPUT:
      Console table + data/backtest_results.csv + optional matplotlib chart.

  # --- FUTURE HOOKS ---
  # HOOK: swap yfinance for broker API data (e.g., Alpaca, IBKR)
  # HOOK: replace Black-Scholes delta with real option chain data
  # HOOK: plug in live order execution after backtest validation
==============================================================================

DISCLAIMER:
  This is a personal/hobby project shared for educational purposes only.
  It is NOT financial advice and comes with NO warranty or guarantee.
  Trading securities involves substantial risk of loss. Do your own
  research before using this code or making any investment decisions.
  The author is not a licensed financial advisor. Use entirely at your
  own risk. Past performance does not guarantee future results.

  Option return estimates use simplified Black-Scholes modeling and are
  approximations only — real-world slippage, spreads, and IV changes
  will differ significantly.
==============================================================================
"""

import argparse
import csv
import datetime
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Optional imports — fail gracefully with clear messages
# ---------------------------------------------------------------------------

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy is required.  pip install numpy")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas is required.  pip install pandas")
    sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance is required.  pip install yfinance")
    sys.exit(1)

try:
    from tabulate import tabulate
except ImportError:
    print("ERROR: tabulate is required.  pip install tabulate")
    sys.exit(1)

# matplotlib is optional — charts disabled if missing
try:
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend (works headless)
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
HITS_JSON = DATA_DIR / "qualified_hits.json"
RESULTS_CSV = DATA_DIR / "backtest_results.csv"
CHART_PATH = DATA_DIR / "backtest_chart.png"

# Default window assumption when none can be parsed from the filing:
# "next trading day, 2:00 PM – 4:00 PM ET"
DEFAULT_WINDOW_START_HOUR = 14   # 2:00 PM ET
DEFAULT_WINDOW_START_MIN = 0
DEFAULT_WINDOW_END_HOUR = 16     # 4:00 PM ET
DEFAULT_WINDOW_END_MIN = 0

# Strategy parameters
PRE_WINDOW_HOURS = 2       # Observe price action this far before window
POST_WINDOW_MINUTES = 30   # Hold put this far past window start
RISK_FREE_RATE = 0.05      # Annualized (for Black-Scholes)
DEFAULT_IV = 0.60           # Implied volatility assumption (60%)
PUT_PREMIUM_PCT = 0.03      # Fallback: assume put costs ~3% of stock price

# Rate-limit for yfinance calls
YFINANCE_DELAY_SEC = 1.0


# ---------------------------------------------------------------------------
# Black-Scholes Put Pricing (analytical approximation)
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    """Standard normal CDF via error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_put(S: float, K: float, T: float,
                      r: float, sigma: float) -> float:
    """
    Black-Scholes European put price.
    S = spot price, K = strike, T = time to expiry (years),
    r = risk-free rate, sigma = implied volatility.
    """
    if T <= 0 or sigma <= 0 or S <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    put = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    return max(put, 0.0)


def estimate_put_return(entry_price: float, exit_price: float,
                        hold_hours: float, iv: float = DEFAULT_IV) -> dict:
    """
    Simulate buying an ATM put at entry_price, selling after hold_hours
    when the underlying is at exit_price.

    Returns dict with put_entry_cost, put_exit_value, pnl, return_pct.
    """
    if entry_price <= 0:
        return {"put_entry_cost": 0, "put_exit_value": 0,
                "pnl": 0, "return_pct": 0.0, "method": "skip"}

    K = entry_price  # ATM strike
    T_entry = max(hold_hours / (252 * 6.5), 1e-6)  # Convert hours to years (trading)
    T_exit = max(T_entry - (hold_hours / (252 * 6.5)), 1e-8)
    # Simplification: T_exit ≈ very small (we're selling immediately)
    # So exit value ≈ intrinsic + small time value

    put_cost = black_scholes_put(entry_price, K, T_entry, RISK_FREE_RATE, iv)
    if put_cost < 0.01:
        # Fallback: assume put costs ~3% of stock price
        put_cost = entry_price * PUT_PREMIUM_PCT

    # Exit value: BS with updated spot and near-zero time
    T_remaining = max(1e-6, T_entry * 0.1)  # ~10% of original T remains
    put_exit = black_scholes_put(exit_price, K, T_remaining, RISK_FREE_RATE, iv)
    # Floor at intrinsic value
    intrinsic = max(K - exit_price, 0.0)
    put_exit = max(put_exit, intrinsic)

    pnl = put_exit - put_cost
    return_pct = (pnl / put_cost * 100.0) if put_cost > 0 else 0.0

    return {
        "put_entry_cost": round(put_cost, 4),
        "put_exit_value": round(put_exit, 4),
        "pnl": round(pnl, 4),
        "return_pct": round(return_pct, 2),
        "method": "black-scholes",
    }


# ---------------------------------------------------------------------------
# Time Parsing Helpers
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)",
    re.IGNORECASE,
)


def parse_time_str(s: str) -> Optional[datetime.time]:
    """Parse a time string like '2:00 p.m.' into a datetime.time."""
    m = _TIME_RE.search(s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).replace(".", "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return datetime.time(hour, minute)
    return None


def next_trading_day(d: datetime.date) -> datetime.date:
    """Return the next weekday on or after d."""
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d += datetime.timedelta(days=1)
    return d


def resolve_window(hit: dict) -> tuple[datetime.datetime, datetime.datetime]:
    """
    Determine the VWAP pricing window datetimes for a hit.
    Returns (window_start, window_end) as naive datetimes in ET.
    Falls back to defaults if parsing fails.
    """
    # Base date: filing_date (the pricing window is typically next trading day)
    try:
        filed = datetime.date.fromisoformat(hit["date_filed"])
    except (ValueError, KeyError):
        filed = datetime.date.today() - datetime.timedelta(days=7)

    window_date = next_trading_day(filed + datetime.timedelta(days=1))

    # Try to parse explicit times from window_start / window_end fields
    ws = None
    we = None
    if hit.get("window_start"):
        ws = parse_time_str(hit["window_start"])
    if hit.get("window_end"):
        we = parse_time_str(hit["window_end"])

    # Fallback: try parsing from pricing_window text
    if (ws is None or we is None) and hit.get("pricing_window"):
        times_found = _TIME_RE.findall(hit["pricing_window"])
        parsed_times = []
        for h, m, ap in times_found:
            hour = int(h)
            minute = int(m)
            ampm = ap.replace(".", "").lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23:
                parsed_times.append(datetime.time(hour, minute))
        if len(parsed_times) >= 2:
            ws = ws or parsed_times[0]
            we = we or parsed_times[1]
        elif len(parsed_times) == 1:
            ws = ws or parsed_times[0]

    # Final fallback: default 2:00 PM – 4:00 PM
    if ws is None:
        ws = datetime.time(DEFAULT_WINDOW_START_HOUR, DEFAULT_WINDOW_START_MIN)
    if we is None:
        we = datetime.time(DEFAULT_WINDOW_END_HOUR, DEFAULT_WINDOW_END_MIN)

    start_dt = datetime.datetime.combine(window_date, ws)
    end_dt = datetime.datetime.combine(window_date, we)

    # Sanity: end must be after start
    if end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(hours=2)

    return start_dt, end_dt


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

def fetch_intraday(ticker: str, target_date: datetime.date,
                   interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Download intraday OHLCV data for ticker on target_date via yfinance.
    Returns a DataFrame indexed by datetime, or None on failure.

    HOOK: Replace this function with broker API calls (Alpaca, IBKR)
          for higher-quality or real-time data.
    """
    if not ticker:
        return None

    time.sleep(YFINANCE_DELAY_SEC)

    # yfinance needs a date range; fetch target_date ± 1 day
    start = target_date - datetime.timedelta(days=1)
    end = target_date + datetime.timedelta(days=2)

    try:
        tk = yf.Ticker(ticker)
        df = tk.history(start=start.isoformat(), end=end.isoformat(),
                        interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return None

        # Filter to target date only
        # yfinance returns timezone-aware timestamps; normalize to date
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        mask = df.index.date == target_date
        day_df = df.loc[mask].copy()

        if day_df.empty:
            # Try the day itself as a broader window
            return None

        return day_df

    except Exception as e:
        print(f"    ⚠ yfinance error for {ticker}: {e}")
        return None


def fetch_daily_fallback(ticker: str, target_date: datetime.date) -> Optional[dict]:
    """
    Fallback: get daily OHLC for target_date if intraday isn't available.
    Returns dict with open, high, low, close, or None.

    HOOK: Replace with broker daily bar API for better reliability.
    """
    if not ticker:
        return None

    time.sleep(YFINANCE_DELAY_SEC)

    start = target_date - datetime.timedelta(days=5)
    end = target_date + datetime.timedelta(days=2)

    try:
        tk = yf.Ticker(ticker)
        df = tk.history(start=start.isoformat(), end=end.isoformat(),
                        interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return None

        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        mask = df.index.date == target_date
        day_row = df.loc[mask]
        if day_row.empty:
            # Use nearest prior date
            prior = df.loc[df.index.date <= target_date]
            if prior.empty:
                return None
            day_row = prior.iloc[[-1]]

        row = day_row.iloc[0]
        return {
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Price Analysis
# ---------------------------------------------------------------------------

def analyze_trade(df: pd.DataFrame,
                  window_start: datetime.datetime,
                  window_end: datetime.datetime,
                  verbose: bool = False) -> Optional[dict]:
    """
    Given intraday 5-min bars and a VWAP window, compute:
      - pre_window:  price change 2 hours before window start
      - during_window: price change during the window
      - post_window: price change 30 min after window start
      - put entry/exit simulation

    Returns dict with all metrics, or None if data is insufficient.
    """
    if df is None or df.empty:
        return None

    # Define analysis periods
    pre_start = window_start - datetime.timedelta(hours=PRE_WINDOW_HOURS)
    post_end = window_start + datetime.timedelta(minutes=POST_WINDOW_MINUTES)

    # Put entry: 1 hour before window (gives time for the setup to develop)
    put_entry_time = window_start - datetime.timedelta(hours=1)
    put_exit_time = post_end  # 30 min after window opens

    # Find nearest bars to each timestamp
    def nearest_price(ts: datetime.datetime, col: str = "Close") -> Optional[float]:
        if df.empty:
            return None
        idx = df.index.get_indexer([ts], method="nearest")[0]
        if idx < 0 or idx >= len(df):
            return None
        # Only accept if within 15 minutes
        actual_ts = df.index[idx]
        if abs((actual_ts - ts).total_seconds()) > 900:
            return None
        return float(df.iloc[idx][col])

    # Key prices
    price_pre_start = nearest_price(pre_start)
    price_window_start = nearest_price(window_start)
    price_window_end = nearest_price(window_end)
    price_post = nearest_price(post_end)
    price_put_entry = nearest_price(put_entry_time)
    price_put_exit = nearest_price(put_exit_time)

    if verbose:
        print(f"    Prices: pre={price_pre_start}, ws={price_window_start}, "
              f"we={price_window_end}, post={price_post}")

    # Need at minimum: a window start price and either window end or post price
    if price_window_start is None:
        return None
    if price_window_end is None and price_post is None:
        return None

    # Calculate returns
    def pct_change(p1, p2):
        if p1 and p2 and p1 > 0:
            return round((p2 - p1) / p1 * 100.0, 3)
        return None

    pre_window_chg = pct_change(price_pre_start, price_window_start)
    during_window_chg = pct_change(price_window_start, price_window_end)
    post_window_chg = pct_change(price_window_start, price_post)

    # Maximum drawdown during window
    window_mask = (df.index >= window_start) & (df.index <= window_end)
    window_bars = df.loc[window_mask]
    max_drop = None
    if not window_bars.empty and price_window_start and price_window_start > 0:
        min_low = float(window_bars["Low"].min())
        max_drop = round((min_low - price_window_start) / price_window_start * 100.0, 3)

    # --- Put simulation ---
    put_result = {"put_entry_cost": 0, "put_exit_value": 0,
                  "pnl": 0, "return_pct": 0.0, "method": "skip"}

    if price_put_entry and price_put_exit:
        hold_hours = POST_WINDOW_MINUTES / 60.0 + 1.0  # ~1.5 hours total hold
        put_result = estimate_put_return(price_put_entry, price_put_exit, hold_hours)

    # Determine win/loss
    # Win = stock dropped during window (put gains value)
    price_drop = during_window_chg if during_window_chg is not None else 0
    is_win = put_result["return_pct"] > 0

    return {
        "price_pre_start": price_pre_start,
        "price_window_start": price_window_start,
        "price_window_end": price_window_end,
        "price_post": price_post,
        "pre_window_chg_pct": pre_window_chg,
        "during_window_chg_pct": during_window_chg,
        "post_window_chg_pct": post_window_chg,
        "max_drop_pct": max_drop,
        "put_entry_cost": put_result["put_entry_cost"],
        "put_exit_value": put_result["put_exit_value"],
        "put_pnl": put_result["pnl"],
        "put_return_pct": put_result["return_pct"],
        "put_method": put_result["method"],
        "is_win": is_win,
    }


def analyze_trade_daily(daily: dict, window_start: datetime.datetime) -> dict:
    """
    Fallback analysis using daily OHLC when intraday data isn't available.
    Assumes the window corresponds to afternoon selling pressure.
    """
    o, h, l, c = daily["open"], daily["high"], daily["low"], daily["close"]

    # Approximate: VWAP window causes drop from open/high toward close
    day_chg = ((c - o) / o * 100.0) if o > 0 else 0
    max_drop = ((l - o) / o * 100.0) if o > 0 else 0

    # Rough put sim: if stock dropped open→close, put profits
    put_result = estimate_put_return(o, c, hold_hours=2.0)

    return {
        "price_pre_start": None,
        "price_window_start": o,
        "price_window_end": c,
        "price_post": c,
        "pre_window_chg_pct": None,
        "during_window_chg_pct": round(day_chg, 3),
        "post_window_chg_pct": round(day_chg, 3),
        "max_drop_pct": round(max_drop, 3),
        "put_entry_cost": put_result["put_entry_cost"],
        "put_exit_value": put_result["put_exit_value"],
        "put_pnl": put_result["pnl"],
        "put_return_pct": put_result["return_pct"],
        "put_method": f"{put_result['method']} (daily fallback)",
        "is_win": put_result["return_pct"] > 0,
    }


# ---------------------------------------------------------------------------
# Charting
# ---------------------------------------------------------------------------

def make_chart(results: list[dict], out_path: Path):
    """
    Generate a bar chart of put returns for each trade,
    highlighting wins (green) and losses (red).
    """
    if not HAS_MATPLOTLIB:
        print("  ⚠ matplotlib not installed — skipping chart generation.")
        print("    Install with: pip install matplotlib")
        return

    if not results:
        return

    # Sort by put return
    sorted_results = sorted(results, key=lambda r: r["put_return_pct"])

    tickers = [r["ticker"] or r["company"][:10] for r in sorted_results]
    returns = [r["put_return_pct"] for r in sorted_results]
    colors = ["#2ecc71" if ret > 0 else "#e74c3c" for ret in returns]

    fig, ax = plt.subplots(figsize=(max(10, len(results) * 0.6), 6))
    bars = ax.bar(range(len(tickers)), returns, color=colors, edgecolor="white",
                  linewidth=0.5)

    ax.set_xticks(range(len(tickers)))
    ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Put Return (%)")
    ax.set_title("Convertible VWAP Backtest — Put Returns per Trade")
    ax.axhline(y=0, color="black", linewidth=0.8)
    ax.grid(axis="y", alpha=0.3)

    # Add value labels on bars
    for bar, val in zip(bars, returns):
        y_pos = bar.get_height()
        va = "bottom" if y_pos >= 0 else "top"
        ax.text(bar.get_x() + bar.get_width() / 2, y_pos,
                f"{val:+.1f}%", ha="center", va=va, fontsize=7, fontweight="bold")

    plt.tight_layout()
    fig.savefig(str(out_path), dpi=150)
    plt.close(fig)
    print(f"\n  📊 Chart saved to {out_path}")


# ---------------------------------------------------------------------------
# Main Backtest
# ---------------------------------------------------------------------------

def load_hits() -> list[dict]:
    """Load qualified hits from JSON."""
    if not HITS_JSON.exists():
        print(f"❌ No hits file found at {HITS_JSON}")
        print("   Run the scanner first: python convertible_scanner.py --broad --days-back 30")
        sys.exit(1)

    with open(HITS_JSON, "r", encoding="utf-8") as f:
        hits = json.load(f)

    if not hits:
        print("❌ Hits file is empty. Run the scanner to populate it.")
        sys.exit(1)

    return hits


def run_backtest(hits: list[dict], limit: Optional[int] = None,
                 ticker_filter: Optional[str] = None,
                 verbose: bool = False) -> list[dict]:
    """
    Run the full backtest across all qualified hits.
    Returns list of result dicts.
    """
    # Filter
    if ticker_filter:
        hits = [h for h in hits if (h.get("ticker") or "").upper() == ticker_filter.upper()]
        if not hits:
            print(f"  No hits found for ticker '{ticker_filter}'.")
            return []

    if limit:
        hits = hits[:limit]

    print(f"\n  Processing {len(hits)} qualified hit(s)...\n")

    results = []
    for i, hit in enumerate(hits, 1):
        ticker = hit.get("ticker", "")
        company = hit.get("company_name", "Unknown")
        display = ticker if ticker else company[:25]
        confidence = hit.get("confidence", "")
        broad = hit.get("broad_match", False)

        print(f"  [{i}/{len(hits)}] {display}  (filed {hit.get('date_filed', '?')}, "
              f"conf={confidence}{'  [Broad]' if broad else ''})")

        # Skip if no ticker — can't fetch price data
        if not ticker:
            print(f"    ⚠ No ticker symbol — skipping.")
            results.append({
                "ticker": "",
                "company": company,
                "date_filed": hit.get("date_filed", ""),
                "confidence": confidence,
                "broad_match": broad,
                "window_text": (hit.get("pricing_window") or "")[:80],
                "window_date": "",
                "during_window_chg_pct": None,
                "max_drop_pct": None,
                "put_return_pct": 0.0,
                "is_win": False,
                "data_source": "none",
                "status": "SKIP (no ticker)",
            })
            continue

        # Resolve window times
        window_start, window_end = resolve_window(hit)
        window_date = window_start.date()

        if verbose:
            print(f"    Window: {window_start.strftime('%Y-%m-%d %H:%M')} → "
                  f"{window_end.strftime('%H:%M')}")

        # Try intraday data first
        df = fetch_intraday(ticker, window_date, interval="5m")
        data_source = "intraday-5m"
        trade = None

        if df is not None and len(df) >= 5:
            trade = analyze_trade(df, window_start, window_end, verbose=verbose)
        else:
            if verbose:
                print(f"    No intraday data — trying daily fallback...")
            daily = fetch_daily_fallback(ticker, window_date)
            if daily:
                trade = analyze_trade_daily(daily, window_start)
                data_source = "daily-fallback"
            else:
                print(f"    ❌ No price data available for {ticker} on {window_date}")
                data_source = "none"

        if trade is None:
            results.append({
                "ticker": ticker,
                "company": company,
                "date_filed": hit.get("date_filed", ""),
                "confidence": confidence,
                "broad_match": broad,
                "window_text": (hit.get("pricing_window") or "")[:80],
                "window_date": str(window_date),
                "during_window_chg_pct": None,
                "max_drop_pct": None,
                "put_return_pct": 0.0,
                "is_win": False,
                "data_source": data_source,
                "status": "SKIP (no data)",
            })
            continue

        # Format outcome
        wl = "WIN" if trade["is_win"] else "LOSS"
        drop = trade["during_window_chg_pct"]
        put_ret = trade["put_return_pct"]
        print(f"    → Stock {drop:+.2f}% during window | "
              f"Put return: {put_ret:+.1f}% | {wl}"
              if drop is not None else
              f"    → Put return: {put_ret:+.1f}% | {wl} (daily data)")

        results.append({
            "ticker": ticker,
            "company": company,
            "date_filed": hit.get("date_filed", ""),
            "confidence": confidence,
            "broad_match": broad,
            "window_text": (hit.get("pricing_window") or "")[:80],
            "window_date": str(window_date),
            "during_window_chg_pct": drop,
            "max_drop_pct": trade["max_drop_pct"],
            "put_return_pct": put_ret,
            "is_win": trade["is_win"],
            "data_source": data_source,
            "status": wl,
            # Extra detail (not in CSV but useful for analysis)
            "pre_window_chg_pct": trade.get("pre_window_chg_pct"),
            "post_window_chg_pct": trade.get("post_window_chg_pct"),
            "put_entry_cost": trade.get("put_entry_cost"),
            "put_exit_value": trade.get("put_exit_value"),
        })

    return results


# ---------------------------------------------------------------------------
# Display & Export
# ---------------------------------------------------------------------------

def display_results(results: list[dict]):
    """Print a formatted summary table to the console."""
    if not results:
        print("\n  No results to display.\n")
        return

    # Console table
    table_data = []
    for r in results:
        ticker_col = r["ticker"] or r["company"][:15]
        drop = f"{r['during_window_chg_pct']:+.2f}%" if r["during_window_chg_pct"] is not None else "—"
        max_drop = f"{r['max_drop_pct']:+.2f}%" if r["max_drop_pct"] is not None else "—"
        put_ret = f"{r['put_return_pct']:+.1f}%"
        status = r["status"]
        source = r.get("data_source", "")

        table_data.append([
            ticker_col,
            r["date_filed"],
            (r["window_text"][:35] + "..." if len(r.get("window_text", "")) > 35
             else r.get("window_text", "")),
            drop,
            max_drop,
            put_ret,
            status,
            source,
        ])

    headers = ["Ticker", "Filed", "Window", "Drop %", "Max Drop",
               "Put Ret %", "W/L", "Source"]

    print("\n" + "=" * 110)
    print("  CONVERTIBLE VWAP BACKTEST RESULTS")
    print("=" * 110)
    print(tabulate(table_data, headers=headers, tablefmt="fancy_grid",
                   maxcolwidths=[12, 12, 35, 10, 10, 10, 8, 10]))
    print()


def display_summary(results: list[dict]):
    """Print aggregate summary statistics."""
    # Filter to only trades with actual data
    traded = [r for r in results if r["status"] in ("WIN", "LOSS")]
    skipped = [r for r in results if r["status"].startswith("SKIP")]

    print("=" * 60)
    print("  SUMMARY STATISTICS")
    print("=" * 60)
    print(f"  Total hits processed:  {len(results)}")
    print(f"  Trades with data:      {len(traded)}")
    print(f"  Skipped (no data):     {len(skipped)}")

    if not traded:
        print("\n  ⚠ No trades had sufficient data for analysis.\n")
        return

    wins = [r for r in traded if r["is_win"]]
    losses = [r for r in traded if not r["is_win"]]
    returns = [r["put_return_pct"] for r in traded]
    drops = [r["during_window_chg_pct"] for r in traded
             if r["during_window_chg_pct"] is not None]

    win_rate = len(wins) / len(traded) * 100.0
    avg_return = np.mean(returns)
    median_return = np.median(returns)
    max_win = max(returns) if returns else 0
    max_loss = min(returns) if returns else 0
    avg_drop = np.mean(drops) if drops else 0

    print(f"\n  Win rate:              {win_rate:.1f}% ({len(wins)}/{len(traded)})")
    print(f"  Avg put return:        {avg_return:+.2f}%")
    print(f"  Median put return:     {median_return:+.2f}%")
    print(f"  Best trade:            {max_win:+.2f}%")
    print(f"  Worst trade:           {max_loss:+.2f}%")
    print(f"  Avg stock drop in window: {avg_drop:+.2f}%")

    # Breakdown by confidence
    for conf in ["High", "Medium"]:
        subset = [r for r in traded if r["confidence"] == conf]
        if subset:
            sub_wr = len([r for r in subset if r["is_win"]]) / len(subset) * 100
            sub_avg = np.mean([r["put_return_pct"] for r in subset])
            print(f"\n  [{conf} confidence] {len(subset)} trades, "
                  f"win rate {sub_wr:.0f}%, avg return {sub_avg:+.1f}%")

    # Breakdown by broad vs strict
    broad_trades = [r for r in traded if r.get("broad_match")]
    strict_trades = [r for r in traded if not r.get("broad_match")]
    if broad_trades and strict_trades:
        bwr = len([r for r in broad_trades if r["is_win"]]) / len(broad_trades) * 100
        swr = len([r for r in strict_trades if r["is_win"]]) / len(strict_trades) * 100
        print(f"\n  [Strict mode] {len(strict_trades)} trades, win rate {swr:.0f}%")
        print(f"  [Broad mode]  {len(broad_trades)} trades, win rate {bwr:.0f}%")

    print()
    print("=" * 60)
    print()


def export_csv(results: list[dict], out_path: Path):
    """Write results to CSV."""
    if not results:
        return

    fieldnames = [
        "ticker", "company", "date_filed", "confidence", "broad_match",
        "window_text", "window_date", "during_window_chg_pct", "max_drop_pct",
        "put_return_pct", "is_win", "data_source", "status",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(f"  📄 Results saved to {out_path}")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backtest put-buying strategy on convertible VWAP setups from SEC EDGAR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python convertible_backtester.py                       # Test all hits
  python convertible_backtester.py --limit 10            # First 10 only
  python convertible_backtester.py --ticker MSTR         # One ticker
  python convertible_backtester.py --chart --verbose     # Full detail + chart
        """,
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit to first N hits (default: all)",
    )
    parser.add_argument(
        "--ticker", type=str, default=None,
        help="Only backtest hits for this ticker symbol",
    )
    parser.add_argument(
        "--chart", action="store_true",
        help="Generate matplotlib bar chart of returns",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print detailed per-trade analysis",
    )

    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   Convertible VWAP Backtester — Put Strategy        ║")
    print("╚══════════════════════════════════════════════════════╝")

    # Load data
    hits = load_hits()
    print(f"\n  Loaded {len(hits)} qualified hit(s) from {HITS_JSON.name}")

    # Run backtest
    results = run_backtest(
        hits,
        limit=args.limit,
        ticker_filter=args.ticker,
        verbose=args.verbose,
    )

    # Display
    display_results(results)
    display_summary(results)

    # Export
    export_csv(results, RESULTS_CSV)

    # Chart
    if args.chart:
        traded = [r for r in results if r["status"] in ("WIN", "LOSS")]
        make_chart(traded, CHART_PATH)

    print("  Done.\n")


if __name__ == "__main__":
    main()
