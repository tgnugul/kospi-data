"""일봉 시세(OHLCV) + 시가총액 수집.

OHLCV: pykrx (수정주가). 시가총액/상장주식수: pykrx get_market_cap 1차 시도,
KRX 로그인(.env KRX_ID/KRX_PW) 불가로 실패하면 다음(Daum) 금융 현재 상장주식수
× 수정 종가로 근사한다 (분할·병합은 수정주가로 반영되나 증자·자사주 소각에 의한
주식수 변동은 반영되지 않음 — src/data/README.md 참조).
"""

import time
from datetime import date, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

from . import DATA_DIR

load_dotenv()  # KRX_ID / KRX_PW 가 있으면 pykrx가 사용

PRICES_DIR = DATA_DIR / "prices"
DEFAULT_YEARS = 10
SLEEP_SEC = 0.3  # KRX/네이버 서버 부하 방지
DAUM_QUOTE_URL = "https://finance.daum.net/api/quotes/A{ticker}"
DAUM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.daum.net/"}

OHLCV_RENAME = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
}
CAP_RENAME = {
    "시가총액": "market_cap",
    "상장주식수": "shares_outstanding",
}


def default_date_range(years: int = DEFAULT_YEARS) -> tuple[str, str]:
    """(fromdate, todate) 'YYYYMMDD' 문자열. 오늘 기준 최근 N년."""
    today = date.today()
    start = today - timedelta(days=365 * years)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def fetch_shares_daum(ticker: str) -> int | None:
    """다음 금융에서 현재 상장주식수 조회. 실패 시 None."""
    try:
        resp = requests.get(DAUM_QUOTE_URL.format(ticker=ticker),
                            headers=DAUM_HEADERS, timeout=15)
        resp.raise_for_status()
        n = resp.json().get("listedShareCount")
        return int(n) if n else None
    except Exception as e:
        print(f"[prices] {ticker} daum shares FAILED: {e}")
        return None


def attach_market_cap(ohlcv: pd.DataFrame, cap: pd.DataFrame,
                      fallback_shares: int | None) -> pd.DataFrame:
    """OHLCV에 시가총액·상장주식수 결합 (순수 함수).

    cap(pykrx 결과)이 비어 있으면 fallback_shares × close 근사치 사용.
    """
    if not cap.empty:
        cap = cap.rename(columns=CAP_RENAME)[list(CAP_RENAME.values())]
        return ohlcv.join(cap, how="left")
    df = ohlcv.copy()
    if fallback_shares:
        df["market_cap"] = df["close"] * fallback_shares
        df["shares_outstanding"] = fallback_shares
    else:
        df["market_cap"] = pd.NA
        df["shares_outstanding"] = pd.NA
    return df


def fetch_prices(ticker: str, fromdate: str, todate: str) -> pd.DataFrame:
    """단일 종목 일봉 OHLCV + 시가총액. 컬럼: date, open, high, low, close,
    volume, market_cap, shares_outstanding, ticker."""
    from pykrx import stock

    ohlcv = stock.get_market_ohlcv(fromdate, todate, ticker)
    if ohlcv.empty:
        return pd.DataFrame()
    ohlcv = ohlcv.rename(columns=OHLCV_RENAME)[list(OHLCV_RENAME.values())]

    try:
        cap = stock.get_market_cap(fromdate, todate, ticker)
    except Exception:
        cap = pd.DataFrame()
    if cap.empty or "시가총액" not in cap.columns:
        cap = pd.DataFrame()
        fallback_shares = fetch_shares_daum(ticker)
    else:
        fallback_shares = None

    df = attach_market_cap(ohlcv, cap, fallback_shares)
    df.index.name = "date"
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"])
    df["ticker"] = ticker
    return df


def collect_prices(
    tickers: list[str],
    fromdate: str | None = None,
    todate: str | None = None,
    sleep_sec: float = SLEEP_SEC,
) -> dict[str, int]:
    """종목별 시세를 수집해 data/prices/{ticker}.parquet 저장.

    반환: {ticker: 행 수} (수집 실패/빈 데이터는 0).
    """
    if fromdate is None or todate is None:
        fromdate, todate = default_date_range()
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    result: dict[str, int] = {}
    for i, ticker in enumerate(tickers, 1):
        try:
            df = fetch_prices(ticker, fromdate, todate)
        except Exception as e:
            print(f"[prices] {ticker} FAILED: {e}")
            result[ticker] = 0
            continue
        if df.empty:
            print(f"[prices] {ticker} empty")
            result[ticker] = 0
        else:
            df.to_parquet(PRICES_DIR / f"{ticker}.parquet", index=False)
            result[ticker] = len(df)
            print(f"[prices] ({i}/{len(tickers)}) {ticker}: {len(df)} rows")
        time.sleep(sleep_sec)
    return result
