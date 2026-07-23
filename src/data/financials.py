"""DART OpenAPI 연간 사업보고서 재무제표 수집.

- corpCode.xml 로 ticker(종목코드) -> corp_code 매핑
- fnlttSinglAcntAll 로 재무상태표(BS)·손익계산서(IS/CIS)·현금흐름표(CF) 주요 계정 추출
"""

import io
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date

import pandas as pd
import requests
from dotenv import load_dotenv

from . import DATA_DIR

DART_BASE = "https://opendart.fss.or.kr/api"
CORP_CODES_PATH = DATA_DIR / "dart" / "corp_codes.parquet"
FINANCIALS_DIR = DATA_DIR / "financials"
DEFAULT_YEARS = 10  # fnlttSinglAcntAll 은 2015 사업연도부터 지원 (그 이전은 결측 허용)
REPRT_ANNUAL = "11011"  # 사업보고서
SLEEP_SEC = 0.2

# 표준계정코드(account_id) -> 지표명
KEY_ACCOUNT_IDS = {
    # 재무상태표
    "ifrs-full_Assets": "assets",
    "ifrs-full_CurrentAssets": "current_assets",
    "ifrs-full_Liabilities": "liabilities",
    "ifrs-full_CurrentLiabilities": "current_liabilities",
    "ifrs-full_Equity": "equity",
    "ifrs-full_EquityAttributableToOwnersOfParent": "equity_owners",
    "ifrs-full_CashAndCashEquivalents": "cash",
    # 손익계산서
    "ifrs-full_Revenue": "revenue",
    "ifrs-full_CostOfSales": "cost_of_sales",
    "ifrs-full_GrossProfit": "gross_profit",
    "dart_OperatingIncomeLoss": "operating_income",
    "ifrs-full_ProfitLoss": "net_income",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent": "net_income_owners",
    "ifrs-full_FinanceCosts": "finance_costs",
    "ifrs-full_InterestExpense": "finance_costs",  # 금융사(예: KB금융)는 이자비용으로 공시
    # 현금흐름표
    "ifrs-full_CashFlowsFromUsedInOperatingActivities": "cf_operating",
    "ifrs-full_CashFlowsFromUsedInInvestingActivities": "cf_investing",
    "ifrs-full_CashFlowsFromUsedInFinancingActivities": "cf_financing",
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "capex",
    "ifrs-full_DividendsPaidClassifiedAsFinancingActivities": "dividends_paid",
}

# 표준코드 미사용 기업 대비: 계정명 폴백 (공백 제거 후 비교)
KEY_ACCOUNT_NAMES = {
    "자산총계": "assets",
    "유동자산": "current_assets",
    "부채총계": "liabilities",
    "유동부채": "current_liabilities",
    "자본총계": "equity",
    "현금및현금성자산": "cash",
    "매출액": "revenue",
    "수익(매출액)": "revenue",
    "영업수익": "revenue",
    "매출원가": "cost_of_sales",
    "매출총이익": "gross_profit",
    "매출총이익(손실)": "gross_profit",
    "금융비용": "finance_costs",
    "이자비용": "finance_costs",
    "영업이익": "operating_income",
    "영업이익(손실)": "operating_income",
    "당기순이익": "net_income",
    "당기순이익(손실)": "net_income",
    "당기순손익": "net_income",
    "연결당기순이익": "net_income",
    "연결당기순이익(손실)": "net_income",
    "영업활동현금흐름": "cf_operating",
    "영업활동으로인한현금흐름": "cf_operating",
    "투자활동현금흐름": "cf_investing",
    "투자활동으로인한현금흐름": "cf_investing",
    "재무활동현금흐름": "cf_financing",
    "재무활동으로인한현금흐름": "cf_financing",
    "유형자산의취득": "capex",
    "배당금의지급": "dividends_paid",
    "배당금지급": "dividends_paid",
}

# 지표별 소속 재무제표 그룹 (BS / IS(=IS·CIS) / CF).
# 예: 현금흐름표의 '당기순이익'(조정 출발점)이 net_income으로 중복 추출되는 것을 방지.
METRIC_GROUP = {
    "assets": "BS",
    "current_assets": "BS",
    "liabilities": "BS",
    "current_liabilities": "BS",
    "equity": "BS",
    "equity_owners": "BS",
    "cash": "BS",
    "revenue": "IS",
    "cost_of_sales": "IS",
    "gross_profit": "IS",
    "operating_income": "IS",
    "net_income": "IS",
    "net_income_owners": "IS",
    "finance_costs": "IS",
    "cf_operating": "CF",
    "cf_investing": "CF",
    "cf_financing": "CF",
    "capex": "CF",
    "dividends_paid": "CF",
}


def get_api_key() -> str:
    load_dotenv()
    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY not set (.env)")
    return key


# ---------- corp_code 매핑 ----------

def parse_corp_codes(xml_bytes: bytes) -> pd.DataFrame:
    """CORPCODE.xml 파싱 -> [corp_code, corp_name, stock_code] (상장사만)."""
    root = ET.fromstring(xml_bytes)
    rows = []
    for el in root.iter("list"):
        stock_code = (el.findtext("stock_code") or "").strip()
        if not stock_code:
            continue
        rows.append(
            {
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "stock_code": stock_code,
                "modify_date": (el.findtext("modify_date") or "").strip(),
            }
        )
    df = pd.DataFrame(rows, columns=["corp_code", "corp_name", "stock_code", "modify_date"])
    # 동일 종목코드 중복 시 최신 modify_date 유지
    df = df.sort_values("modify_date").drop_duplicates("stock_code", keep="last")
    return df.drop(columns=["modify_date"]).reset_index(drop=True)


def collect_corp_codes() -> pd.DataFrame:
    """DART corpCode.xml(zip) 다운로드 -> 파싱 -> parquet 저장."""
    resp = requests.get(
        f"{DART_BASE}/corpCode.xml", params={"crtfc_key": get_api_key()}, timeout=60
    )
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")
    df = parse_corp_codes(xml_bytes)
    CORP_CODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CORP_CODES_PATH, index=False)
    print(f"[financials] corp codes: {len(df)} listed corps -> {CORP_CODES_PATH}")
    return df


def load_corp_codes() -> pd.DataFrame:
    if CORP_CODES_PATH.exists():
        return pd.read_parquet(CORP_CODES_PATH)
    return collect_corp_codes()


# ---------- 재무제표 파싱 (순수 함수) ----------

def parse_amount(value: str | None) -> float | None:
    """DART 금액 문자열('1,234,567', '-', '') -> float 또는 None."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _match_metric(item: dict) -> str | None:
    account_id = (item.get("account_id") or "").strip()
    # 2018년 이전 공시는 'ifrs-full_' 대신 'ifrs_' 접두어 사용 (예: 삼성전자 2015)
    if account_id.startswith("ifrs_"):
        account_id = "ifrs-full_" + account_id[len("ifrs_"):]
    if account_id in KEY_ACCOUNT_IDS:
        return KEY_ACCOUNT_IDS[account_id]
    name = (item.get("account_nm") or "").replace(" ", "").strip()
    # 항목 번호 접두어 제거 (예: 'VIII.당기순이익', '1.매출액', 'Ⅴ.영업이익')
    name = re.sub(r"^[0-9IVXⅠ-ⅿ]+\.", "", name)
    return KEY_ACCOUNT_NAMES.get(name)


def extract_key_accounts(items: list[dict]) -> list[dict]:
    """fnlttSinglAcntAll 응답 list에서 주요 계정만 추출.

    반환 레코드: {sj_div, metric, account_id, account_nm, amount}
    동일 (sj_div 그룹, metric)은 최초 항목만 사용 (연결 본표 우선 순서 가정).
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        sj_div = (item.get("sj_div") or "").strip()
        if sj_div not in ("BS", "IS", "CIS", "CF"):
            continue
        metric = _match_metric(item)
        if metric is None:
            continue
        # IS와 CIS 는 같은 손익 지표 그룹으로 취급 (중복 방지)
        group = "IS" if sj_div in ("IS", "CIS") else sj_div
        # 지표가 소속 재무제표에서 나온 경우만 인정 (예: CF의 당기순이익 제외)
        if METRIC_GROUP[metric] != group:
            continue
        key = (group, metric)
        if key in seen:
            continue
        amount = parse_amount(item.get("thstrm_amount"))
        if amount is None:
            continue
        seen.add(key)
        out.append(
            {
                "sj_div": sj_div,
                "metric": metric,
                "account_id": (item.get("account_id") or "").strip(),
                "account_nm": (item.get("account_nm") or "").strip(),
                "amount": amount,
            }
        )
    return out


# ---------- 수집 ----------

def fetch_annual_statement(
    corp_code: str, year: int, api_key: str, fs_div: str = "CFS"
) -> list[dict] | None:
    """단일 기업·연도 사업보고서 전체 재무제표 조회. 데이터 없으면 None."""
    resp = requests.get(
        f"{DART_BASE}/fnlttSinglAcntAll.json",
        params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": REPRT_ANNUAL,
            "fs_div": fs_div,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "000":
        return None
    return data.get("list", [])


def default_year_range(years: int = DEFAULT_YEARS) -> list[int]:
    """최근 N개 사업연도 (직전 연도부터 역순 아님, 오름차순)."""
    last = date.today().year - 1
    return list(range(last - years + 1, last + 1))


def collect_financials(
    tickers: list[str],
    years: list[int] | None = None,
    sleep_sec: float = SLEEP_SEC,
) -> dict[str, int]:
    """종목별 연간 재무제표 주요 계정을 수집해 data/financials/{ticker}.parquet 저장.

    연결(CFS) 우선, 없으면 별도(OFS) 폴백. 반환: {ticker: 행 수}.
    """
    if years is None:
        years = default_year_range()
    api_key = get_api_key()
    corp_map = load_corp_codes().set_index("stock_code")["corp_code"].to_dict()
    FINANCIALS_DIR.mkdir(parents=True, exist_ok=True)

    result: dict[str, int] = {}
    for i, ticker in enumerate(tickers, 1):
        corp_code = corp_map.get(ticker)
        if corp_code is None:
            print(f"[financials] {ticker}: corp_code not found")
            result[ticker] = 0
            continue
        rows: list[dict] = []
        for year in years:
            try:
                items = fetch_annual_statement(corp_code, year, api_key, fs_div="CFS")
                fs_div = "CFS"
                if not items:
                    items = fetch_annual_statement(corp_code, year, api_key, fs_div="OFS")
                    fs_div = "OFS"
            except Exception as e:
                print(f"[financials] {ticker} {year} FAILED: {e}")
                items = None
            if items:
                for rec in extract_key_accounts(items):
                    rows.append({"ticker": ticker, "corp_code": corp_code,
                                 "year": year, "fs_div": fs_div, **rec})
            time.sleep(sleep_sec)
        if rows:
            df = pd.DataFrame(rows)
            df.to_parquet(FINANCIALS_DIR / f"{ticker}.parquet", index=False)
            result[ticker] = len(df)
            print(f"[financials] ({i}/{len(tickers)}) {ticker}: {len(df)} rows")
        else:
            print(f"[financials] {ticker}: no data")
            result[ticker] = 0
    return result
