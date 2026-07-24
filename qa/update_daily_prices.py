# -*- coding: utf-8 -*-
"""일일 시세 자동 수집 — GitHub Actions에서 매 장마감 후 실행.

추적 종목의 최근 종가·전일比를 수집해 data/live/prices_latest.csv 로 저장한다.
소스 폴백: FinanceDataReader(네이버 기반, 해외IP 안정적) → pykrx(KRX).
개인정보 없음 — 공개 시장 데이터만. 포트폴리오/평단은 이 저장소에 두지 않는다.

로컬 테스트: pip install finance-datareader pykrx && python qa/update_daily_prices.py
"""
import csv
import datetime as dt
import os
import sys

# 추적 유니버스 (코어4 + 위시리스트 + 관심) — 필요 시 여기만 수정
TICKERS = {
    "004370": "농심",
    "035720": "카카오",
    "005930": "삼성전자",
    "021240": "코웨이",
    "033780": "KT&G",
    "018260": "삼성SDS",
    "002840": "미원상사",
    "014680": "한솔케미칼",
    "003230": "삼양식품",
    "000080": "하이트진로",
    "035420": "NAVER",
    "271560": "오리온",
    "012750": "에스원",
    "030000": "제일기획",
    "067160": "SOOP",
    "007310": "오뚜기",
    "051900": "LG생활건강",
}
US_TICKERS = {"CPNG": "쿠팡", "NKE": "나이키", "DIS": "디즈니"}  # FinanceDataReader가 미국 종목도 지원

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "live", "prices_latest.csv")


def fetch_fdr(symbol, days=10):
    import FinanceDataReader as fdr
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    df = fdr.DataReader(symbol, start.isoformat(), end.isoformat())
    if df is None or len(df) < 2:
        return None
    closes = df["Close"].dropna()
    if len(closes) < 2:
        return None
    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    date = closes.index[-1].date().isoformat()
    return dict(close=last, prev=prev, chg=(last / prev - 1) * 100, date=date)


def fetch_pykrx(ticker, days=10):
    from pykrx import stock
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
    if df is None or len(df) < 2:
        return None
    closes = df["종가"].dropna()
    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    date = str(closes.index[-1].date())
    return dict(close=last, prev=prev, chg=(last / prev - 1) * 100, date=date)


def main():
    rows, failed = [], []
    for tk, nm in {**TICKERS, **US_TICKERS}.items():
        rec = None
        for fetcher, label in ((fetch_fdr, "fdr"), (fetch_pykrx, "pykrx")):
            if tk in US_TICKERS and label == "pykrx":
                continue  # pykrx는 국내 전용
            try:
                rec = fetcher(tk)
                if rec:
                    rec["source"] = label
                    break
            except Exception as e:
                print(f"[warn] {tk} {label}: {type(e).__name__}: {e}", file=sys.stderr)
        if rec:
            rows.append(dict(ticker=tk, name=nm, **rec))
            print(f"[ok] {nm}({tk}) {rec['close']:,.2f} ({rec['chg']:+.2f}%) as of {rec['date']} via {rec['source']}")
        else:
            failed.append(tk)
            print(f"[fail] {nm}({tk}) 모든 소스 실패")

    if not rows:
        print("[error] 전 종목 수집 실패 — 파일 미갱신", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "name", "date", "close", "prev", "chg", "source"])
        w.writeheader()
        for r in rows:
            r["close"] = round(r["close"], 2)
            r["prev"] = round(r["prev"], 2)
            r["chg"] = round(r["chg"], 2)
            w.writerow(r)
    with open(OUT.replace("prices_latest.csv", "updated_at.txt"), "w") as f:
        f.write(dt.datetime.utcnow().isoformat() + "Z\n")
    print(f"\n[done] {len(rows)}종목 저장 ({len(failed)}종목 실패: {failed})")
    return 0 if len(failed) < len(TICKERS) // 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
