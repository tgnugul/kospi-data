"""KOSPI 200 구성종목 수집.

1차: pykrx 지수 구성종목(지수코드 1028). 단, 2025년 이후 KRX 정보데이터시스템은
로그인 없이 이 엔드포인트를 막고 있어 .env 에 KRX_ID/KRX_PW 가 없으면 빈 결과가 온다.
2차 폴백: 네이버 금융 KOSPI200 편입종목 페이지(공식 편입 목록) 파싱.
"""

import re
import time

import pandas as pd
import requests
from dotenv import load_dotenv

from . import DATA_DIR

load_dotenv()  # KRX_ID / KRX_PW 가 있으면 pykrx가 사용

KOSPI200_INDEX = "1028"
OUT_PATH = DATA_DIR / "constituents" / "kospi200.parquet"
NAVER_KPI200_URL = "https://finance.naver.com/sise/entryJongmok.naver"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_kospi200_pykrx(date: str | None = None) -> pd.DataFrame:
    """pykrx로 KOSPI 200 구성종목 조회. KRX 로그인 불가 시 빈 DataFrame."""
    from pykrx import stock

    try:
        tickers = stock.get_index_portfolio_deposit_file(KOSPI200_INDEX, date=date)
    except Exception as e:
        print(f"[constituents] pykrx failed: {e}")
        return pd.DataFrame(columns=["ticker", "name"])
    rows = [{"ticker": t, "name": stock.get_market_ticker_name(t)} for t in tickers]
    return pd.DataFrame(rows, columns=["ticker", "name"])


def parse_naver_kpi200_page(html: str) -> list[dict]:
    """네이버 KOSPI200 편입종목 페이지 1개에서 (ticker, name) 추출 (순수 함수)."""
    pattern = re.compile(r'code=(\d{6})"[^>]*>([^<]+)</a>')
    return [{"ticker": code, "name": name.strip()}
            for code, name in pattern.findall(html)]


def fetch_kospi200_naver(sleep_sec: float = 0.2) -> pd.DataFrame:
    """네이버 금융 KOSPI200 편입종목 목록 (20페이지 × 10종목)."""
    rows: list[dict] = []
    for page in range(1, 21):
        resp = requests.get(
            NAVER_KPI200_URL, params={"type": "KPI200", "page": page},
            headers=UA, timeout=15,
        )
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        rows.extend(parse_naver_kpi200_page(resp.text))
        time.sleep(sleep_sec)
    df = pd.DataFrame(rows, columns=["ticker", "name"]).drop_duplicates("ticker")
    return df.reset_index(drop=True)


def collect_constituents(date: str | None = None) -> pd.DataFrame:
    """KOSPI 200 구성종목 수집 → parquet 저장. pykrx 실패 시 네이버 폴백."""
    df = fetch_kospi200_pykrx(date)
    source = "pykrx"
    if df.empty:
        print("[constituents] pykrx returned empty (KRX login required) -> Naver fallback")
        df = fetch_kospi200_naver()
        source = "naver"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    print(f"[constituents] {len(df)} tickers (source={source}) -> {OUT_PATH}")
    return df


def load_constituents() -> pd.DataFrame:
    return pd.read_parquet(OUT_PATH)
