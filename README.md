# Convertible Strategy

Personal project for scanning SEC EDGAR 8-K filings for convertible financing setups and backtesting a simple put-buying strategy around disclosed pricing windows.

This repository contains two local-first Python scripts:

- `convertible_scanner.py`: scans SEC EDGAR for 8-K filings that mention convertible notes and VWAP/pricing-window language
- `convertible_backtester.py`: reads scanner hits from `data/qualified_hits.json` and simulates a basic put strategy using free Yahoo Finance intraday data

## What It Does

The scanner looks for filings that match patterns commonly associated with:

- convertible note offerings
- VWAP-based pricing windows
- pricing periods / observation periods
- broader SEPA / equity-line style financing language when `--broad` is enabled

Qualified hits are stored locally and can then be fed into the backtester to estimate how the stock and a simplified put position might have performed around the pricing window.

## Files In This Repo

- `convertible_scanner.py`
- `convertible_backtester.py`
- `requirements.txt`
- `LICENSE`

Generated runtime data is intentionally not included in GitHub:

- `data/qualified_hits.json`
- `data/backtest_results.csv`
- `data/filings_cache.db`
- `scanner.log`

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Before using the scanner, update the SEC User-Agent in `convertible_scanner.py` to your own contact email, per SEC guidance.

Current placeholder:

```python
"User-Agent": "Personal Convertible Scanner your-email@example.com"
```

## Usage

Run a basic scan:

```bash
python convertible_scanner.py
```

Scan a larger historical window in broad mode:

```bash
python convertible_scanner.py --days-back 90 --fresh --broad
```

Run watch mode:

```bash
python convertible_scanner.py --mode watch
```

Debug a single SEC filing:

```bash
python convertible_scanner.py --debug "SEC_FILING_URL"
```

Run the backtester:

```bash
python convertible_backtester.py
```

Limit the backtest set or generate a chart:

```bash
python convertible_backtester.py --limit 10 --chart --verbose
```

## Scanner Notes

- Uses SEC EDGAR daily index files and respects conservative request pacing
- Supports a stricter default mode and a broader pattern-matching mode
- Logs qualified hits locally for later analysis
- Keeps a local SQLite cache to avoid rescanning the same filings repeatedly

## Backtester Notes

- Uses `yfinance` for free historical price data
- Attempts to infer the pricing window from filing text
- Falls back to a default next-trading-day `2:00 PM - 4:00 PM ET` window when the filing is unclear
- Simulates a simplified ATM put entry before the window and exit shortly after it starts
- Option return estimates are approximate and not broker-grade analytics

## Limitations

- Filing language is messy; regex-based qualification will miss some setups and over-include others
- Ticker extraction is imperfect
- Yahoo intraday coverage can be limited for older dates or thinly traded names
- The options model is intentionally simplified and should not be treated as execution-quality pricing

## Disclaimer

This is a personal/hobby project shared for educational and research purposes only.

It is not financial advice, not legal advice, and not a recommendation to trade any security. If you use this code, do your own research and decide for yourself whether it is appropriate, compliant, or usable for your own situation. Trading involves substantial risk, including loss of capital.

## License

MIT. See `LICENSE`.