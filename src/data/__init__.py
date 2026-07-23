"""데이터부 수집 패키지: KOSPI 200 구성종목, 시세(pykrx), 재무제표(DART)."""

from pathlib import Path

# 저장 루트 (저장소 루트 기준 data/)
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
