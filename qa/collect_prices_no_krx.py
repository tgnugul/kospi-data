"""KRX 계정 없이 Phase 2 전제 데이터 수집 (일봉 + point-in-time 주식수).

경로:
- 일봉 OHLCV: pykrx 수정주가 경로 (로그인 불필요 — src/data/README.md 확인됨)
- 상장주식수(연도별): DART 주식총수현황 API(stockTotqySttus) — .env의 DART_API_KEY 사용
  (KOSPI200 × 10개년 ≈ 2,000건, 일일 한도 20,000건 내. 다른 대량 작업과 같은 날 실행 금지)
- 시가총액: 종가 × 해당 시점 적용 주식수 (사업연도 Y의 주식수를 Y+1년 4월~Y+2년 3월에 적용
  — 재무제표 매핑과 동일한 point-in-time 규약)
- KOSPI 지수: pykrx 시도, 실패 시 경고 후 생략 (벤치마크 H4는 미판정 처리)

사용법 (로컬, .env에 DART_API_KEY 필요 / KRX_ID·KRX_PW 불필요):
    python qa/collect_prices_no_krx.py            # KOSPI200 전 종목
    python qa/collect_prices_no_krx.py --sample 20

한계 (Phase 2 설계서 수정사항으로 기록): 주식수가 연 단위 계단함수라 연중 소각·증자가
다음 매핑 구간부터 반영된다. 상수 주식수(look-ahead)보다는 훨씬 정확하며 설계 취지
(과거 시총·PBR의 체계적 왜곡 방지)는 충족한다.
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import requests

from src.data import DATA_DIR
from src.data.constituents import collect_constituents
from src.data.financials import DART_BASE, get_api_key, load_corp_codes, parse_amount
from src.data.prices import OHLCV_RENAME, default_date_range

SLEEP = 0.15
PRICES_DIR = DATA_DIR / "prices"
SHARES_DIR = DATA_DIR / "shares"


def fetch_shares_year(corp_code: str, year: int, api_key: str) -> float | None:
    """DART 주식총수현황 -> 보통주 발행주식총수 (istc_totqy). 없으면 None."""
    try:
        resp = requests.get(
            f"{DART_BASE}/stockTotqySttus.json",
            params={"crtfc_key": api_key, "corp_code": corp_code,
                    "bsns_year": str(year), "reprt_code": "11011"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return None
        common, fallback = None, None
        for item in data.get("list", []):
            se = (item.get("se") or "").replace(" ", "")
            qty = parse_amount(item.get("istc_totqy"))
            if qty is None or qty <= 0:
                continue
            if "보통" in se:
                common = qty
            elif se in ("합계", "계") and fallback is None:
                fallback = qty
        return common if common is not None else fallback
    except Exception:
        return None


def collect_shares(tickers: list[str], years: list[int]) -> dict[str, dict[int, float]]:
    """종목별 연도별 보통주 발행주식총수. data/shares/shares_by_year.parquet 저장."""
    api_key = get_api_key()
    corp_map = load_corp_codes().set_index("stock_code")["corp_code"].to_dict()
    SHARES_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict[int, float]] = {}
    rows = []
    for i, t in enumerate(tickers, 1):
        corp = corp_map.get(t)
        if corp is None:
            print(f"[shares] {t}: corp_code 없음")
            continue
        by_year: dict[int, float] = {}
        for y in years:
            v = fetch_shares_year(corp, y, api_key)
            if v is not None:
                by_year[y] = v
                rows.append(dict(ticker=t, year=y, shares=v))
            time.sleep(SLEEP)
        out[t] = by_year
        print(f"[shares] ({i}/{len(tickers)}) {t}: {len(by_year)}/{len(years)}개 연도")
    pd.DataFrame(rows).to_parquet(SHARES_DIR / "shares_by_year.parquet", index=False)
    return out


def build_prices(ticker: str, fromdate: str, todate: str,
                 shares_by_year: dict[int, float]) -> pd.DataFrame:
    """일봉(무로그인) + 연도별 주식수 결합 -> 표준 prices 스키마."""
    from pykrx import stock
    ohlcv = stock.get_market_ohlcv(fromdate, todate, ticker)
    if ohlcv.empty:
        return pd.DataFrame()
    ohlcv = ohlcv.rename(columns=OHLCV_RENAME)[list(OHLCV_RENAME.values())]
    ohlcv.index.name = "date"
    df = ohlcv.reset_index()
    df["date"] = pd.to_datetime(df["date"])
    # 사업연도 Y 주식수 -> Y+1.04 ~ Y+2.03 적용 (재무 매핑과 동일 규약)
    fiscal = df["date"].dt.year - (df["date"].dt.month < 4).astype(int) - 1
    df["shares_outstanding"] = fiscal.map(shares_by_year)
    # 이력 시작 이전 구간은 가장 오래된 값으로 채움 (보수적 근사, 결측 방지)
    if shares_by_year:
        oldest = shares_by_year[min(shares_by_year)]
        df["shares_outstanding"] = df["shares_outstanding"].fillna(oldest)
    df["market_cap"] = df["close"] * df["shares_outstanding"]
    df["ticker"] = ticker
    return df


def collect_index(fromdate: str, todate: str) -> bool:
    try:
        from pykrx import stock
        idx = stock.get_index_ohlcv(fromdate, todate, "1001")
        if idx.empty:
            raise RuntimeError("empty")
        idx = idx.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                                  "종가": "close", "거래량": "volume"})
        idx.index.name = "date"
        idx = idx.reset_index()[["date", "open", "high", "low", "close", "volume"]]
        out = DATA_DIR / "index"
        out.mkdir(parents=True, exist_ok=True)
        idx.to_parquet(out / "kospi.parquet", index=False)
        print(f"[index] KOSPI {len(idx)} rows")
        return True
    except Exception as e:
        print(f"[index] KOSPI 지수 수집 실패 ({e}) — H4(벤치마크) 미판정으로 진행")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--tickers", type=str, default=None,
                    help="쉼표구분 티커 목록 (지정 시 KOSPI200 대신 사용 — KOSDAQ 등)")
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip().zfill(6) for t in args.tickers.split(",") if t.strip()]
    else:
        cons = collect_constituents()
        tickers = cons["ticker"].tolist()
    if args.sample:
        tickers = tickers[: args.sample]
    fromdate, todate = default_date_range(args.years)
    fy_last = date.today().year - 1
    years = list(range(fy_last - args.years + 1, fy_last + 1))

    print(f"[1/3] DART 주식총수 수집: {len(tickers)}종목 × {len(years)}개년 "
          f"(약 {len(tickers)*len(years)}건, DART 한도 주의)")
    shares = collect_shares(tickers, years)

    print(f"[2/3] 일봉 수집 (pykrx, 무로그인)")
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for i, t in enumerate(tickers, 1):
        try:
            df = build_prices(t, fromdate, todate, shares.get(t, {}))
        except Exception as e:
            print(f"[prices] {t} FAILED: {e}")
            continue
        if df.empty or df["shares_outstanding"].isna().all():
            print(f"[prices] {t}: 데이터 없음/주식수 결측")
            continue
        df.to_parquet(PRICES_DIR / f"{t}.parquet", index=False)
        n_ok += 1
        if i % 20 == 0:
            print(f"[prices] {i}/{len(tickers)} 진행")
        time.sleep(0.3)
    print(f"[prices] 완료: {n_ok}/{len(tickers)}종목")

    print("[3/3] KOSPI 지수")
    collect_index(fromdate, todate)

    # point-in-time 검사: 표본에서 주식수가 시점별로 변하는지
    varying = 0
    for t in tickers[:10]:
        p = PRICES_DIR / f"{t}.parquet"
        if p.exists():
            if pd.read_parquet(p, columns=["shares_outstanding"])["shares_outstanding"].nunique() > 1:
                varying += 1
    print(f"[check] 주식수 시점 변화 감지: 표본 10종목 중 {varying}종목 "
          f"({'OK' if varying > 0 else 'FAIL — 전 종목 상수면 문제'})")
    return 0 if n_ok >= len(tickers) * 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
