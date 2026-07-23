# -*- coding: utf-8 -*-
"""KOSDAQ 유니버스 수집 (GitHub Actions용) — 자격 게이트 → DART 재무 10개년 → 시세.

자격 게이트 (discovery_agent_spec 0단계):
- 시가총액 >= 3,000억 (FDR 상장목록 Marcap으로 1차, 없으면 pykrx)
- 최근 60일 일평균 거래대금 >= 10억 (게이트 통과 후보만 pykrx 조회 — 호출 절약)
- 스팩·리츠·우선주·관리종목 제외

환경변수 DART_API_KEY 필요 (Actions secret).
사용법: python qa/collect_kosdaq.py [--list-only] [--sample N] [--resume]
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MCAP_MIN = 300_000_000_000
TURNOVER_MIN = 1_000_000_000


def eligible_tickers(verbose=True):
    import FinanceDataReader as fdr
    import pandas as pd
    from pykrx import stock

    lst = fdr.StockListing("KOSDAQ")
    name_col = "Name" if "Name" in lst.columns else lst.columns[1]
    code_col = "Code" if "Code" in lst.columns else lst.columns[0]
    lst = lst[~lst[name_col].astype(str).str.contains("스팩|리츠", na=False)]
    if verbose:
        print(f"[gate] KOSDAQ 상장 {len(lst)}종목, 컬럼: {list(lst.columns)[:8]}")

    # 1차: 시총 (목록에 Marcap 있으면 무료로 필터)
    if "Marcap" in lst.columns:
        big = lst[pd.to_numeric(lst["Marcap"], errors="coerce") >= MCAP_MIN]
        if verbose:
            print(f"[gate] 시총 3,000억+ (목록 기준): {len(big)}종목")
    else:
        big = lst  # 시총 컬럼 없으면 2차에서 pykrx로
        if verbose:
            print("[gate] 목록에 Marcap 없음 — pykrx로 개별 확인")

    end = dt.date.today()
    start = end - dt.timedelta(days=100)
    out = []
    for i, (_, row) in enumerate(big.iterrows(), 1):
        tk, nm = str(row[code_col]).zfill(6), str(row[name_col])
        try:
            df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), tk)
            if df is None or len(df) < 40:
                continue
            turnover = float((df["종가"] * df["거래량"]).tail(60).mean())
            if turnover < TURNOVER_MIN:
                continue
            mcap = None
            if "Marcap" in big.columns:
                mcap = float(row["Marcap"])
            else:
                cap = stock.get_market_cap_by_date(end.strftime("%Y%m%d"), end.strftime("%Y%m%d"), tk)
                mcap = float(cap["시가총액"].iloc[-1]) if cap is not None and len(cap) else None
                if mcap is None or mcap < MCAP_MIN:
                    continue
            out.append(dict(ticker=tk, name=nm, mcap=mcap, turnover=turnover))
            if verbose and len(out) % 25 == 0:
                print(f"[gate] ...{len(out)}종목 통과 (검사 {i}/{len(big)})")
        except Exception as e:
            print(f"[warn] {nm}({tk}): {type(e).__name__}", file=sys.stderr)
    import pandas as pd
    return pd.DataFrame(out).sort_values("mcap", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--sample", type=int, default=None)
    args = ap.parse_args()

    from src.data import DATA_DIR
    cand_path = DATA_DIR / "constituents" / "kosdaq_eligible.csv"
    cand_path.parent.mkdir(parents=True, exist_ok=True)

    df = eligible_tickers()
    df.to_csv(cand_path, index=False)
    print(f"[gate] 최종 통과 {len(df)}종목 -> {cand_path}")
    print(df.head(30).to_string(index=False))
    if args.list_only or len(df) == 0:
        return 0

    tickers = df["ticker"].tolist()
    if args.sample:
        tickers = tickers[: args.sample]
    if args.resume:
        done = {p.stem for p in (DATA_DIR / "financials").glob("*.parquet")}
        tickers = [t for t in tickers if t not in done]

    print(f"[financials] {len(tickers)}종목 × 10개년 — 예상 DART 약 {len(tickers)*15:,}건")
    from src.data.financials import collect_financials
    fin = collect_financials(tickers)
    n_ok = sum(1 for v in fin.values() if v > 0)
    print(f"[financials] {n_ok}/{len(tickers)}종목 완료")
    return 0 if n_ok >= max(1, len(tickers)) * 0.7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
