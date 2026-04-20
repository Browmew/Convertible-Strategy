#!/usr/bin/env python3
"""
==============================================================================
  Convertible Note VWAP Scanner — SEC EDGAR 8-K Filing Monitor
==============================================================================

  Scans SEC EDGAR for new convertible note issuances with public VWAP
  pricing windows. 100% local, no paid APIs, no API keys.

  INSTALLATION:
      pip install -r requirements.txt

  USAGE:
      # Scan today + yesterday (default)
      python convertible_scanner.py

      # Scan last 3 trading days
      python convertible_scanner.py --days-back 3

      # Watch mode: poll every 5 min during market hours (9:30 AM – 4:00 PM ET)
      python convertible_scanner.py --mode watch

      # Auto-open high-confidence hits in browser
      python convertible_scanner.py --open

      # Combine flags
      python convertible_scanner.py --mode watch --days-back 2 --open

  SCHEDULING (runs daily at 9:35 AM):
      Windows Task Scheduler:
          Action: Start a program
          Program: python
          Arguments: "C:\\path\\to\\convertible_scanner.py" --mode scan
          Trigger: Daily at 9:35 AM

      Linux/macOS cron:
          35 9 * * 1-5 cd /path/to/dir && python convertible_scanner.py >> cron.log 2>&1

  STOPPING WATCH MODE:
      Press Ctrl+C in the terminal. The script handles SIGINT gracefully.

  TEST / DEBUG A SINGLE FILING:
      python convertible_scanner.py --debug https://www.sec.gov/Archives/edgar/data/1829635/000110465926040171/tm2611272d1_8k.htm

  BROAD MODE (90-day backtest):
      python convertible_scanner.py --days-back 90 --fresh --broad

  # --broad = includes common SEPA/equity-line deals (frequent signals, still highly tradeable)
  # Normal mode = only rare intraday VWAP-window convertibles (original Reddit strategy)

  # --- FUTURE HOOKS ---
  # To add trading automation, look for "# HOOK:" comments throughout.
  # HOOK: on_new_setup(filing) — called when a qualified setup is found.
  # HOOK: on_scan_complete(results) — called after each full scan cycle.

==============================================================================

DISCLAIMER:
  This is a personal/hobby project shared for educational purposes only.
  It is NOT financial advice and comes with NO warranty or guarantee.
  Trading securities involves substantial risk of loss. Do your own
  research before using this code or making any investment decisions.
  The author is not a licensed financial advisor. Use entirely at your
  own risk. Past performance does not guarantee future results.

  SEC EDGAR fair-use: this tool respects SEC rate limits (max 10 req/sec).
  You MUST set a valid User-Agent with your own email per SEC guidelines:
  https://www.sec.gov/os/webmaster-faq#developers
==============================================================================
"""

import argparse
import datetime
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from tabulate import tabulate

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
DB_PATH = DATA_DIR / "filings_cache.db"
LOG_PATH = SCRIPT_DIR / "scanner.log"
HITS_JSON = DATA_DIR / "qualified_hits.json"

EDGAR_BASE = "https://www.sec.gov"
EDGAR_ARCHIVES = f"{EDGAR_BASE}/Archives/edgar"
EDGAR_DAILY_INDEX = f"{EDGAR_ARCHIVES}/daily-index"
EDGAR_FULL_INDEX = f"{EDGAR_ARCHIVES}/full-index"

HEADERS = {
    "User-Agent": "Personal Convertible Scanner your-email@example.com",
    "Accept-Encoding": "gzip, deflate",
}

# SEC rate-limit: max 10 req/sec, we stay well under (retries handle 429/503)
REQUEST_DELAY_SEC = 1.0

# Convertible keywords (case-insensitive)
CONVERTIBLE_KEYWORDS = [
    r"convertible\s+(?:senior\s+)?notes?",
    r"convertible\s+debentures?",
    r"convertible\s+bonds?",
    r"convertible\s+(?:subordinated\s+)?notes?",
]

# VWAP / pricing-window keywords
VWAP_KEYWORDS = [
    r"VWAP",
    r"volume[\s-]*weighted\s+average\s+price",
    r"pricing\s+period",
    r"pricing\s+window",
    r"observation\s+period",
    r"pricing\s+determination\s+period",
    r"pricing\s+determination",
]

# Time-range pattern (e.g., "from 9:30 a.m. to 4:00 p.m.")
TIME_RANGE_PATTERN = re.compile(
    r"(?:from|between)\s+"
    r"\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)"
    r"\s+(?:to|and|through|–|-)\s+"
    r"\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)",
    re.IGNORECASE,
)

# Ticker extraction patterns
TICKER_PATTERNS = [
    re.compile(r"\((?:NASDAQ|Nasdaq)\s*:\s*([A-Z]{1,5})\)"),
    re.compile(r"\((?:NYSE|Nyse)\s*:\s*([A-Z]{1,5})\)"),
    re.compile(r"\((?:NYSE\s*MKT|AMEX)\s*:\s*([A-Z]{1,5})\)"),
    re.compile(r"\((?:NASDAQ|NYSE|AMEX)\s*(?:GS|GM|CM)?\s*:\s*([A-Z]{1,5})\)"),
    re.compile(r"ticker\s+symbol\s+[\"']?([A-Z]{1,5})[\"']?", re.IGNORECASE),
    re.compile(r"common\s+stock.*?traded.*?under.*?symbol\s+[\"']?([A-Z]{1,5})", re.IGNORECASE),
]

# Time extraction pattern — captures individual times with am/pm
TIME_EXTRACT_PATTERN = re.compile(
    r"(\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))",
    re.IGNORECASE,
)

# Exhibit 99.1 detection
EXHIBIT_99_PATTERN = re.compile(r"exhibit\s*99\.1", re.IGNORECASE)

# Additional broad-mode keywords (SEPA / equity-line / looser pricing language)
BROAD_EXTRA_KEYWORDS = [
    r"lowest\s+daily",
    r"advance\s+notice",
    r"equity\s+line",
    r"standby\s+equity",
    r"SEPA",
    r"purchase\s+price.*?(?:VWAP|volume[\s-]*weighted)",
]

# Compiled convertible and VWAP patterns
CONVERTIBLE_RE = re.compile("|".join(CONVERTIBLE_KEYWORDS), re.IGNORECASE)
VWAP_RE = re.compile("|".join(VWAP_KEYWORDS), re.IGNORECASE)
BROAD_RE = re.compile("|".join(VWAP_KEYWORDS + BROAD_EXTRA_KEYWORDS), re.IGNORECASE)

# ---------------------------------------------------------------------------
# Globals for graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logging.info("Shutdown requested (Ctrl+C). Finishing current cycle...")
    print("\n⏹  Shutdown requested. Finishing current cycle...")


signal.signal(signal.SIGINT, _signal_handler)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging():
    """Configure dual logging: file + console."""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # File handler — detailed
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console handler — info only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db() -> sqlite3.Connection:
    """Initialize SQLite cache database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_filings (
            accession_number TEXT PRIMARY KEY,
            cik             TEXT,
            company_name    TEXT,
            form_type       TEXT,
            date_filed      TEXT,
            filing_url      TEXT,
            is_qualified     INTEGER DEFAULT 0,
            ticker          TEXT,
            confidence      TEXT,
            pricing_window  TEXT,
            exhibit_url     TEXT,
            scanned_at      TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, accession: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM seen_filings WHERE accession_number = ?", (accession,)
    )
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, record: dict):
    conn.execute("""
        INSERT OR REPLACE INTO seen_filings
        (accession_number, cik, company_name, form_type, date_filed,
         filing_url, is_qualified, ticker, confidence, pricing_window,
         exhibit_url, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record["accession_number"],
        record.get("cik", ""),
        record.get("company_name", ""),
        record.get("form_type", ""),
        record.get("date_filed", ""),
        record.get("filing_url", ""),
        1 if record.get("is_qualified") else 0,
        record.get("ticker", ""),
        record.get("confidence", ""),
        record.get("pricing_window", ""),
        record.get("exhibit_url", ""),
        datetime.datetime.now().isoformat(),
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update(HEADERS)


def fetch_with_retry(url: str, max_retries: int = 3, timeout: int = 30) -> Optional[requests.Response]:
    """
    Fetch a URL with exponential backoff on 429, 503, timeouts.
    Returns the Response object or None.
    """
    for attempt in range(max_retries + 1):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code in (429, 503):
                if attempt < max_retries:
                    wait = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                    logging.debug(f"  {resp.status_code} on {url}, retry in {wait}s "
                                  f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
            return resp
        except (requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                logging.debug(f"  {type(e).__name__} on {url}, retry in {wait}s "
                              f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                logging.warning(f"  All {max_retries} retries exhausted for {url}: {e}")
                return None
    return None


def fetch_url(url: str, as_text: bool = True) -> Optional[str]:
    """Fetch a URL with rate-limiting, retries, and error handling."""
    time.sleep(REQUEST_DELAY_SEC)
    try:
        logging.debug(f"GET {url}")
        resp = fetch_with_retry(url)
        if resp is None:
            return None
        if resp.status_code == 404:
            logging.debug(f"  404: {url}")
            return None
        resp.raise_for_status()
        if as_text:
            # Try to decode properly
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        return resp.content
    except requests.RequestException as e:
        logging.warning(f"Request failed for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# EDGAR Index Parsing
# ---------------------------------------------------------------------------


def get_quarter(dt: datetime.date) -> str:
    """Return EDGAR quarter string (QTR1..QTR4) for a date."""
    return f"QTR{(dt.month - 1) // 3 + 1}"


def get_trading_days(days_back: int) -> list[datetime.date]:
    """Return the last N trading days (Mon-Fri, excluding weekends)."""
    dates = []
    d = datetime.date.today()
    while len(dates) < days_back:
        if d.weekday() < 5:  # Mon=0 .. Fri=4
            dates.append(d)
        d -= datetime.timedelta(days=1)
    return dates


def fetch_daily_index(target_date: datetime.date) -> list[dict]:
    """
    Fetch and parse the EDGAR daily master index for a given date.
    Returns list of dicts: {cik, company_name, form_type, date_filed, filename}.
    """
    year = target_date.year
    qtr = get_quarter(target_date)
    date_str = target_date.strftime("%Y%m%d")

    # Try master.YYYYMMDD.idx first, then master.idx
    urls_to_try = [
        f"{EDGAR_DAILY_INDEX}/{year}/{qtr}/master.{date_str}.idx",
        f"{EDGAR_FULL_INDEX}/{year}/{qtr}/master.idx",
    ]

    for url in urls_to_try:
        text = fetch_url(url)
        if text is None:
            continue

        entries = []
        lines = text.splitlines()

        # Skip header lines (usually first ~11 lines until we see the dashes)
        data_started = False
        for line in lines:
            if line.strip().startswith("---"):
                data_started = True
                continue
            if not data_started:
                continue

            parts = line.split("|")
            if len(parts) < 5:
                continue

            cik = parts[0].strip()
            company_name = parts[1].strip()
            form_type = parts[2].strip()
            date_filed = parts[3].strip()
            filename = parts[4].strip()

            entries.append({
                "cik": cik,
                "company_name": company_name,
                "form_type": form_type,
                "date_filed": date_filed,
                "filename": filename,
            })

        if entries:
            logging.info(f"Fetched {len(entries)} entries from index for {target_date}")
            return entries

        # If we got the full master.idx, filter by date
        if "master.idx" in url and not date_str in url:
            entries = [e for e in entries if e["date_filed"] == target_date.isoformat()]

    logging.debug(f"No index data found for {target_date}")
    return []


def filter_8k_filings(entries: list[dict]) -> list[dict]:
    """Filter to only 8-K and 8-K/A filings."""
    return [e for e in entries if e["form_type"] in ("8-K", "8-K/A")]


# ---------------------------------------------------------------------------
# Filing Retrieval & Parsing
# ---------------------------------------------------------------------------


def build_filing_index_url(entry: dict) -> str:
    """Build URL for the filing index page from an index entry."""
    filename = entry["filename"]
    # filename is like: edgar/data/123456/0001234567-26-012345.txt
    # We want the index page: .../-index.htm
    if filename.startswith("edgar/"):
        filename = filename[len("edgar/"):]

    # Extract accession from the filename
    base = filename.rsplit("/", 1)
    if len(base) == 2:
        folder = base[0]
        acc_file = base[1]
        acc_number = acc_file.replace(".txt", "")
        # Build the accession folder path (with dashes removed for folder name)
        acc_nodash = acc_number.replace("-", "")
        index_url = f"{EDGAR_ARCHIVES}/{folder}/{acc_nodash}/{acc_number}-index.htm"
        return index_url

    return f"{EDGAR_ARCHIVES}/{filename}"


def build_filing_txt_url(entry: dict) -> str:
    """Build URL for the raw filing .txt from an index entry."""
    filename = entry["filename"]
    if not filename.startswith("edgar/"):
        return f"{EDGAR_ARCHIVES}/{filename}"
    return f"{EDGAR_BASE}/Archives/{filename}"


def get_accession_number(entry: dict) -> str:
    """Extract accession number from the filename field."""
    filename = entry["filename"]
    # e.g., edgar/data/123456/0001234567-26-012345.txt
    basename = filename.rsplit("/", 1)[-1]
    return basename.replace(".txt", "")


def fetch_filing_documents(entry: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Fetch the filing content. Returns (full_text, exhibit_99_url).
    First tries the index page to find document links, then falls back
    to the raw .txt submission.
    """
    index_url = build_filing_index_url(entry)
    exhibit_url = None

    # Try index page first to find individual documents
    index_html = fetch_url(index_url)
    if index_html:
        soup = BeautifulSoup(index_html, "lxml")

        # Collect all document URLs from the filing index
        doc_urls = []
        primary_doc_url = None
        table = soup.find("table", class_="tableFile")
        if table:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 4:
                    doc_link = cells[2].find("a")
                    description = cells[1].get_text(strip=True).lower() if len(cells) > 1 else ""
                    doc_type = cells[3].get_text(strip=True).lower() if len(cells) > 3 else ""

                    if doc_link and doc_link.get("href"):
                        href = doc_link["href"]
                        if not href.startswith("http"):
                            href = f"{EDGAR_BASE}{href}"
                        doc_urls.append((href, description, doc_type))

                        # Identify primary 8-K document
                        if not primary_doc_url and ("8-k" in doc_type or "8-k" in description):
                            primary_doc_url = href

                        # Identify Exhibit 99.1 (broad matching)
                        href_lower = href.lower()
                        if any(kw in description for kw in
                               ("ex-99", "99.1", "ex99", "exhibit 99")):
                            exhibit_url = href
                        elif any(kw in href_lower for kw in
                                 ("ex-99", "ex99", "ex_99")):
                            exhibit_url = href

        # Fetch primary document + exhibit text
        all_text_parts = []

        if primary_doc_url:
            txt = fetch_url(primary_doc_url)
            if txt:
                all_text_parts.append(txt)

        # Also fetch exhibit 99.1 if found
        if exhibit_url:
            txt = fetch_url(exhibit_url)
            if txt:
                all_text_parts.append(txt)
                logging.debug(f"  Fetched Exhibit 99.1: {exhibit_url}")

        # If we didn't find specific docs, fetch remaining docs
        if not all_text_parts:
            for url, desc, dtype in doc_urls[:5]:  # Limit to first 5 docs
                if url.endswith((".htm", ".html", ".txt")):
                    txt = fetch_url(url)
                    if txt:
                        all_text_parts.append(txt)
                        break  # Usually the first doc is the main filing

        if all_text_parts:
            return "\n\n".join(all_text_parts), exhibit_url

    # Fallback: fetch raw .txt submission file
    raw_url = build_filing_txt_url(entry)
    raw_text = fetch_url(raw_url)
    if raw_text:
        # Try to extract Exhibit 99.1 URL from the raw text (broad pattern)
        ex_match = re.search(
            r'<FILENAME>(ex[\-_]?99[\-_.]?1[^\s<]*)',
            raw_text, re.IGNORECASE
        )
        if ex_match:
            ex_filename = ex_match.group(1)
            # Build exhibit URL from the filing folder
            folder = raw_url.rsplit("/", 1)[0]
            accession = get_accession_number(entry)
            acc_nodash = accession.replace("-", "")
            cik = entry["cik"]
            exhibit_url = f"{EDGAR_ARCHIVES}/data/{cik}/{acc_nodash}/{ex_filename}"

        return raw_text, exhibit_url

    return None, None


def extract_text_from_html(html: str) -> str:
    """Strip HTML tags and return plain text."""
    soup = BeautifulSoup(html, "lxml")
    # Remove script and style elements
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def extract_ticker(text: str, company_name: str) -> str:
    """Try to extract stock ticker from filing text. Fallback to company name."""
    for pattern in TICKER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def extract_pricing_window_sentence(text: str) -> str:
    """
    Extract the sentence(s) containing VWAP/pricing window language.
    Returns the most relevant sentence or a combined excerpt.
    """
    # Clean HTML if needed
    if "<" in text and ">" in text:
        text = extract_text_from_html(text)

    sentences = re.split(r'(?<=[.!?])\s+', text)
    relevant = []

    for sent in sentences:
        has_vwap = VWAP_RE.search(sent)
        has_time = TIME_RANGE_PATTERN.search(sent)
        if has_vwap or has_time:
            # Clean up whitespace
            clean = re.sub(r'\s+', ' ', sent).strip()
            if len(clean) > 20:  # Skip trivial matches
                relevant.append(clean)

    if relevant:
        # Return up to 2 most relevant sentences, capped at 500 chars
        combined = " ... ".join(relevant[:2])
        if len(combined) > 500:
            combined = combined[:497] + "..."
        return combined

    return ""


def extract_window_times(pricing_text: str) -> tuple[str, str]:
    """
    Parse start/end times from pricing window text.
    Returns (start_time, end_time) as strings like '9:30 a.m.', or ('', '').
    """
    # Look for a time range phrase first
    range_match = TIME_RANGE_PATTERN.search(pricing_text)
    if range_match:
        times = TIME_EXTRACT_PATTERN.findall(range_match.group(0))
        if len(times) >= 2:
            return (times[0].strip(), times[1].strip())

    # Fallback: grab any two times from the full text
    all_times = TIME_EXTRACT_PATTERN.findall(pricing_text)
    if len(all_times) >= 2:
        return (all_times[0].strip(), all_times[1].strip())
    elif len(all_times) == 1:
        return (all_times[0].strip(), "")

    return ("", "")


def load_hits_json() -> list[dict]:
    """Load existing qualified hits from JSON file."""
    if HITS_JSON.exists():
        try:
            with open(HITS_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_hit_to_json(record: dict):
    """Append a qualified hit to the JSON log file."""
    hits = load_hits_json()
    # Avoid duplicates by accession number
    existing_accessions = {h["accession_number"] for h in hits}
    if record["accession_number"] not in existing_accessions:
        hits.append(record)
        with open(HITS_JSON, "w", encoding="utf-8") as f:
            json.dump(hits, f, indent=2, ensure_ascii=False)


def analyze_filing(text: str, broad: bool = False) -> dict:
    """
    Analyze filing text for convertible note + VWAP signals.
    Returns analysis dict with is_qualified, confidence, details.

    If broad=True, any convertible + BROAD_RE keyword qualifies
    (catches SEPA/equity-line deals). If broad=False, the original
    strict rules apply (only VWAP_RE + time-range for High).
    """
    # Clean HTML for text analysis
    plain_text = text
    if "<" in text and ">" in text:
        plain_text = extract_text_from_html(text)

    result = {
        "is_convertible": False,
        "has_vwap": False,
        "has_broad_keyword": False,
        "has_time_range": False,
        "has_exhibit_99": False,
        "is_qualified": False,
        "confidence": "",
        "pricing_window": "",
        "convertible_matches": [],
        "vwap_matches": [],
        "broad_match": False,
        "qualification_reason": "",
    }

    # Check for convertible keywords
    conv_matches = CONVERTIBLE_RE.findall(plain_text)
    if conv_matches:
        result["is_convertible"] = True
        result["convertible_matches"] = list(set(conv_matches))

    # Check for strict VWAP / pricing window keywords
    vwap_matches = VWAP_RE.findall(plain_text)
    if vwap_matches:
        result["has_vwap"] = True
        result["vwap_matches"] = list(set(vwap_matches))

    # Check broad keywords (superset of VWAP_RE — only used when broad=True)
    broad_kw_matches = []
    if broad:
        broad_kw_matches = BROAD_RE.findall(plain_text)
        if broad_kw_matches:
            result["has_broad_keyword"] = True

    # Check for time range patterns
    if TIME_RANGE_PATTERN.search(plain_text):
        result["has_time_range"] = True

    # Check for Exhibit 99.1
    if EXHIBIT_99_PATTERN.search(text):  # Search raw text (including HTML tags)
        result["has_exhibit_99"] = True

    # --- Determine qualification ---
    # Would this qualify under strict rules?
    strict_ok = (result["is_convertible"]
                 and (result["has_vwap"] or result["has_time_range"]))

    if broad:
        # Broad mode: convertible + (VWAP_RE OR BROAD_RE OR time_range)
        if result["is_convertible"] and (
            result["has_vwap"] or result["has_broad_keyword"]
            or result["has_time_range"]
        ):
            result["is_qualified"] = True
            # broad_match = True only if it would NOT qualify under strict rules
            result["broad_match"] = not strict_ok
            if result["has_time_range"]:
                result["confidence"] = "High"
            else:
                result["confidence"] = "Medium"
            # Log qualification reason
            if result["has_vwap"]:
                matched = result["vwap_matches"][0] if result["vwap_matches"] else "VWAP"
                result["qualification_reason"] = f"convertible + {matched}"
            elif result["has_broad_keyword"]:
                matched = broad_kw_matches[0] if broad_kw_matches else "broad keyword"
                result["qualification_reason"] = f"Broad mode: convertible + {matched}"
            else:
                result["qualification_reason"] = "convertible + time range"
            logging.debug(f"  Qualified: {result['qualification_reason']}")
    else:
        # Strict mode: convertible + (VWAP_RE OR time_range)
        if strict_ok:
            result["is_qualified"] = True
            if result["has_vwap"] and result["has_time_range"]:
                result["confidence"] = "High"
            else:
                result["confidence"] = "Medium"
            if result["has_vwap"]:
                matched = result["vwap_matches"][0] if result["vwap_matches"] else "VWAP"
                result["qualification_reason"] = f"convertible + {matched}"
            else:
                result["qualification_reason"] = "convertible + time range"

    # Extract pricing window text
    if result["is_qualified"]:
        result["pricing_window"] = extract_pricing_window_sentence(text)

    return result


# ---------------------------------------------------------------------------
# Main Scanner
# ---------------------------------------------------------------------------


def scan_filings(conn: sqlite3.Connection, days_back: int = 2,
                 open_browser: bool = False, broad: bool = False) -> tuple:
    """
    Main scan: fetch EDGAR daily indexes, filter 8-Ks, analyze for
    convertible VWAP setups.
    Returns (qualified_records, near_misses, broad_high, broad_medium).
    """
    trading_days = get_trading_days(days_back)
    logging.info(f"Scanning {len(trading_days)} trading day(s): "
                 f"{', '.join(d.isoformat() for d in trading_days)}")

    all_8k_entries = []
    for day in trading_days:
        entries = fetch_daily_index(day)
        eightk = filter_8k_filings(entries)
        logging.info(f"  {day}: {len(entries)} total filings, {len(eightk)} 8-K filings")
        all_8k_entries.extend(eightk)

    logging.info(f"Total 8-K filings to check: {len(all_8k_entries)}")

    new_qualified = []
    near_misses = 0
    broad_high = 0
    broad_medium = 0
    strict_count = 0
    skipped = 0
    errors = 0

    for i, entry in enumerate(all_8k_entries, 1):
        if _shutdown_requested:
            logging.info("Shutdown requested, stopping scan.")
            break

        accession = get_accession_number(entry)

        # Skip already-seen filings
        if is_seen(conn, accession):
            skipped += 1
            continue

        # Print progress to console (overwrites same line)
        print(f"\r  [{i}/{len(all_8k_entries)}] Checking: {entry['company_name'][:50]:<50}", end="", flush=True)
        logging.debug(f"[{i}/{len(all_8k_entries)}] Checking {entry['company_name']} "
                      f"(CIK {entry['cik']}) — {accession}")

        # Fetch and analyze
        filing_text, exhibit_url = fetch_filing_documents(entry)
        if not filing_text:
            errors += 1
            # Still mark as seen so we don't retry on transient errors
            mark_seen(conn, {
                "accession_number": accession,
                "cik": entry["cik"],
                "company_name": entry["company_name"],
                "form_type": entry["form_type"],
                "date_filed": entry["date_filed"],
                "filing_url": build_filing_index_url(entry),
                "is_qualified": False,
            })
            continue

        analysis = analyze_filing(filing_text, broad=broad)

        # Extract ticker
        ticker = extract_ticker(filing_text, entry["company_name"])

        # Extract structured time window
        window_start, window_end = extract_window_times(analysis["pricing_window"])

        # Build filing record
        filing_url = build_filing_index_url(entry)
        record = {
            "accession_number": accession,
            "cik": entry["cik"],
            "company_name": entry["company_name"],
            "form_type": entry["form_type"],
            "date_filed": entry["date_filed"],
            "filing_url": filing_url,
            "is_qualified": analysis["is_qualified"],
            "ticker": ticker,
            "confidence": analysis["confidence"],
            "pricing_window": analysis["pricing_window"],
            "window_start": window_start,
            "window_end": window_end,
            "exhibit_url": exhibit_url or "",
            "broad_match": analysis.get("broad_match", False),
            "qualification_reason": analysis.get("qualification_reason", ""),
        }

        mark_seen(conn, record)

        if analysis["is_qualified"]:
            new_qualified.append(record)
            display_name = ticker if ticker else f"{entry['company_name']} (CIK {entry['cik']})"
            broad_tag = " [Broad]" if analysis.get("broad_match") else ""
            reason = analysis.get("qualification_reason", "")
            logging.info(f"  ✓ QUALIFIED{broad_tag}: {display_name} — "
                         f"{analysis['confidence']} confidence"
                         f"{f' ({reason})' if reason else ''}")

            # Track broad vs strict breakdown
            if analysis.get("broad_match"):
                if analysis["confidence"] == "High":
                    broad_high += 1
                else:
                    broad_medium += 1
            else:
                strict_count += 1

            # Log to JSON for backtester
            save_hit_to_json(record)

            # HOOK: on_new_setup(record) — add trading automation here
            # e.g., send_alert(record), queue_order(record), etc.

            if open_browser and analysis["confidence"] == "High":
                logging.info(f"  Opening filing in browser: {filing_url}")
                webbrowser.open(filing_url)
                if exhibit_url:
                    webbrowser.open(exhibit_url)

        elif analysis["is_convertible"]:
            near_misses += 1
            logging.debug(f"  ~ NEAR MISS (convertible, no pricing window): "
                          f"{entry['company_name']} (CIK {entry['cik']})")

    # Clear the progress line
    print("\r" + " " * 80 + "\r", end="", flush=True)
    logging.info(f"Scan complete: {len(new_qualified)} qualified, "
                 f"{near_misses} near-misses, {skipped} cached/skipped, {errors} errors")

    # HOOK: on_scan_complete(new_qualified) — post-scan automation here

    return new_qualified, near_misses, broad_high, broad_medium, strict_count


def display_results(results: list[dict]):
    """Print a formatted table of qualified setups."""
    if not results:
        print("\nNo new qualified convertible VWAP setups found this scan.\n")
        return

    table_data = []
    for r in results:
        display_name = r["ticker"] if r["ticker"] else f"{r['company_name'][:30]}"
        if not r["ticker"]:
            display_name += f"\n(CIK {r['cik']})"

        # Truncate pricing window for table display
        pw = r["pricing_window"]
        if len(pw) > 80:
            pw = pw[:77] + "..."

        # Mode column: "Strict" or "Broad"
        mode_label = "Broad" if r.get("broad_match") else "Strict"

        # Highlight Medium broad matches
        conf_display = r["confidence"]
        if r.get("broad_match") and r["confidence"] == "Medium":
            conf_display = "Medium *"

        table_data.append([
            display_name,
            r["date_filed"],
            pw or "(see filing)",
            conf_display,
            mode_label,
            r["filing_url"][:60] + "..." if len(r["filing_url"]) > 60 else r["filing_url"],
            ("Yes" if r["exhibit_url"] else "No"),
        ])

    headers = ["Ticker/Company", "Filed", "Pricing Window", "Confidence",
               "Mode", "Filing URL", "Ex 99.1"]

    print("\n" + "=" * 100)
    print("  CONVERTIBLE NOTE VWAP SETUPS")
    print("=" * 100)
    print(tabulate(table_data, headers=headers, tablefmt="fancy_grid",
                   maxcolwidths=[20, 12, 35, 10, 7, 45, 8]))
    print()

    # Print full URLs separately for easy clicking
    print("Direct Links:")
    print("-" * 60)
    for r in results:
        name = r["ticker"] if r["ticker"] else r["company_name"][:40]
        print(f"  {name}:")
        print(f"    8-K:        {r['filing_url']}")
        if r["exhibit_url"]:
            print(f"    Exhibit 99: {r['exhibit_url']}")
        print()


def is_market_hours() -> bool:
    """Check if current time is within US market hours (9:30 AM – 4:00 PM ET)."""
    try:
        # Simple ET calculation: UTC-4 (EDT) or UTC-5 (EST)
        # For robustness, approximate EDT (March–November)
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        month = utc_now.month
        # Rough DST: EDT from March to November
        if 3 <= month <= 10:
            et_offset = datetime.timedelta(hours=-4)
        else:
            et_offset = datetime.timedelta(hours=-5)

        et_now = utc_now + et_offset
        market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)

        # Also check it's a weekday
        if et_now.weekday() >= 5:
            return False

        return market_open <= et_now <= market_close
    except Exception:
        # If timezone calculation fails, assume market hours
        return True


def run_watch_mode(conn: sqlite3.Connection, days_back: int,
                   open_browser: bool, poll_interval: int = 300):
    """
    Watch mode: poll every `poll_interval` seconds during market hours.
    """
    logging.info(f"Starting watch mode (poll every {poll_interval}s during market hours)")
    print(f"\n👁  Watch mode active — polling every {poll_interval // 60} minutes")
    print("   Press Ctrl+C to stop.\n")

    total_found = 0
    cycle = 0

    while not _shutdown_requested:
        cycle += 1

        if not is_market_hours():
            logging.debug("Outside market hours, sleeping 60s...")
            print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] "
                  f"Outside market hours. Sleeping...")
            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(60):
                if _shutdown_requested:
                    break
                time.sleep(1)
            continue

        logging.info(f"Watch cycle #{cycle}")
        print(f"\n  [{datetime.datetime.now().strftime('%H:%M:%S')}] "
              f"Scan cycle #{cycle}...")

        results, _, _, _, _ = scan_filings(conn, days_back, open_browser, broad=False)
        if results:
            display_results(results)
            total_found += len(results)

        print(f"  Cycle #{cycle} done. "
              f"{len(results)} new hits this cycle, {total_found} total. "
              f"Next scan in {poll_interval // 60} min.")

        # Sleep in small increments
        for _ in range(poll_interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    print(f"\n⏹  Watch mode stopped. Total setups found: {total_found}")


# ---------------------------------------------------------------------------
# Debug Mode
# ---------------------------------------------------------------------------


def run_debug_mode(url: str):
    """
    Fetch a single filing URL, run the full analysis pipeline, and print
    detailed results including text excerpts around keyword matches.
    """
    print(f"🔍 DEBUG MODE — Analyzing single filing:")
    print(f"   URL: {url}\n")

    text = fetch_url(url)
    if not text:
        print("❌ Failed to fetch the URL.")
        return

    print(f"   Fetched {len(text):,} characters.\n")

    # Clean to plain text
    plain_text = text
    if "<" in text and ">" in text:
        plain_text = extract_text_from_html(text)

    # Run analysis
    analysis = analyze_filing(text)

    print("=" * 70)
    print("  ANALYSIS RESULT")
    print("=" * 70)
    for k, v in analysis.items():
        if k == "pricing_window" and v:
            print(f"  {k}: {v[:200]}")
        else:
            print(f"  {k}: {v}")
    print()

    # Extract ticker
    ticker = extract_ticker(text, "")
    print(f"  Extracted ticker: {ticker or '(none)'}")

    # Show text excerpts around VWAP/pricing keywords
    print("\n" + "-" * 70)
    print("  KEYWORD CONTEXT EXCERPTS")
    print("-" * 70)

    patterns_to_show = [
        ("CONVERTIBLE", CONVERTIBLE_RE),
        ("VWAP/PRICING", VWAP_RE),
        ("TIME RANGE", TIME_RANGE_PATTERN),
    ]

    for label, pat in patterns_to_show:
        matches = list(pat.finditer(plain_text))
        if matches:
            print(f"\n  [{label}] — {len(matches)} match(es):")
            for j, m in enumerate(matches[:5]):  # Show up to 5
                start = max(0, m.start() - 120)
                end = min(len(plain_text), m.end() + 120)
                excerpt = plain_text[start:end].replace("\n", " ")
                excerpt = re.sub(r"\s+", " ", excerpt).strip()
                print(f"    {j + 1}. ...{excerpt}...")
        else:
            print(f"\n  [{label}] — no matches")

    # Show exhibit detection
    print(f"\n  Exhibit 99.1 in raw text: {bool(EXHIBIT_99_PATTERN.search(text))}")

    # Window times
    if analysis["pricing_window"]:
        ws, we = extract_window_times(analysis["pricing_window"])
        print(f"  Extracted window times: {ws or '(none)'} → {we or '(none)'}")

    print("\n" + "=" * 70)
    if analysis["is_qualified"]:
        print(f"  ✅ QUALIFIED — {analysis['confidence']} confidence")
    elif analysis["is_convertible"]:
        print(f"  ⚠  NEAR MISS — convertible detected but no pricing window")
    else:
        print(f"  ❌ NOT QUALIFIED — no convertible keywords found")
    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Scan SEC EDGAR for convertible note issuances with VWAP pricing windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python convertible_scanner.py                    # Scan today + yesterday
  python convertible_scanner.py --days-back 3      # Look back 3 trading days
  python convertible_scanner.py --mode watch        # Continuous monitoring
  python convertible_scanner.py --mode watch --open # Watch + open browser for hits
        """
    )
    parser.add_argument(
        "--mode", choices=["scan", "watch"], default="scan",
        help="scan = one-time check (default); watch = poll every 5 min during market hours"
    )
    parser.add_argument(
        "--days-back", type=int, default=2,
        help="Number of trading days to look back (default: 2)"
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Auto-open high-confidence hits in the default browser"
    )
    parser.add_argument(
        "--poll-interval", type=int, default=300,
        help="Seconds between watch-mode polls (default: 300 = 5 min)"
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Clear the filing cache and rescan everything (use with large --days-back)"
    )
    parser.add_argument(
        "--debug", type=str, default=None, metavar="URL",
        help="Debug mode: fetch ONE filing URL, run full analysis, print results"
    )
    parser.add_argument(
        "--broad", action="store_true",
        help="Broad mode: qualify any convertible + VWAP/pricing keyword "
             "(catches SEPA/equity-line deals, more hits)"
    )

    args = parser.parse_args()

    # Setup
    setup_logging()
    logging.info("=" * 60)
    logging.info("Convertible VWAP Scanner started")
    logging.info(f"Mode: {args.mode} | Days back: {args.days_back} | "
                 f"Open browser: {args.open} | Broad: {args.broad}")
    logging.info("=" * 60)

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   Convertible Note VWAP Scanner — SEC EDGAR         ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    # Initialize
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db()

    if args.fresh:
        logging.info("--fresh flag: clearing filing cache for rescan")
        conn.execute("DELETE FROM seen_filings")
        conn.commit()
        # Also reset the JSON hits log
        if HITS_JSON.exists():
            HITS_JSON.unlink()
        print("  Cache cleared. All filings will be re-scanned.\n")

    try:
        if args.debug:
            run_debug_mode(args.debug)
        elif args.mode == "watch":
            run_watch_mode(conn, args.days_back, args.open, args.poll_interval)
        else:
            results, near_misses, b_high, b_med, strict_ct = scan_filings(
                conn, args.days_back, args.open, broad=args.broad
            )
            display_results(results)
            count = len(results)
            print(f"✅ {count} new qualified convertible VWAP setup{'s' if count != 1 else ''} found")
            if args.broad:
                broad_total = b_high + b_med
                print(f"   Broad-mode hits: {broad_total} "
                      f"(High: {b_high} | Medium: {b_med}) "
                      f"| Strict-mode qualified: {strict_ct}")
            if near_misses:
                print(f"   Near-miss convertibles (no pricing window): {near_misses}")
            print()
    finally:
        conn.close()
        logging.info("Scanner shut down cleanly.")


if __name__ == "__main__":
    main()
