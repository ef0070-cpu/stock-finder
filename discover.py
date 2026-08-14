"""종목 발굴 파이프라인(1~4단계) 자동화 스크립트 — index.html의 "종목 발굴 시작" 버튼이
server.py의 /discover 엔드포인트를 통해 이 스크립트를 실행한다.
run_pipeline.bat/서버 업데이트 버튼과는 완전히 독립적으로, 필요할 때 직접 실행한다.

1단계: 국내 시총 100 + 미국 S&P500 시총 100 스크리닝 → PER/PBR/ROE 재무적 분석으로 필터링
2단계: 저렴한 퀀트 점수식(RSI/MA/MACD)으로 기술적 필터링
3단계: 시장(국내/미국)별로 사용자가 지정한 개수만큼(기본 1개씩)의 최고 후보만
       골라 Claude(웹검색)로 워런 버핏 체크리스트 심층 리뷰 — 전수(최대 200종목)가
       아니라 소수 종목에만 LLM을 호출해 비용·시간을 억제한다. 개수는
       DISCOVER_KR_COUNT/DISCOVER_US_COUNT 환경변수로 조절한다.
4단계: 3단계에서 PASS 판정을 받은 종목만 최종 채택 — WAIT/REJECT는 결과에 남기지 않는다
       (보고서·앱 카드 모두 "지금 시점 매수 추천" 종목만 보이게 하기 위함).
"""
import concurrent.futures
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from html import escape
from itertools import zip_longest
from typing import Optional

import FinanceDataReader as fdr
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from pykrx import stock as pykrx_stock

from buffett_review import generate_review
from run_pipeline import analyze_ticker

CANDIDATES_FILE = "candidates.json"
PROGRESS_FILE = "discover_progress.json"
REPORTS_DIR = "reports"
KR_LIMIT = 100
US_LIMIT = 100
FINAL_LIMIT_PER_MARKET = 1

# ponytail: 1단계 재무 필터는 "명백한 적신호"만 거른다(적자·자본잠식) — PER/PBR
# 고평가 여부는 걸러내지 않는다(그건 3단계 버핏 심사역이 밸류에이션으로 따로 판단).
# 조회 실패(데이터 없음)는 판단 보류로 통과시킨다 — KRX 재무 API가 간헐적으로
# 빈 응답을 주는 걸 이미 확인했음(discover_run.log 참고), 데이터 없다고 종목을
# 무작정 걸러내면 그 원인이 KRX 쪽 일시 장애일 때 후보가 텅 빌 수 있다.
MIN_ROE_PCT = 0.0  # 이 값 미만이면 제외(자본 까먹는 중)


def passes_financial_filter(per: Optional[float], roe: Optional[float]) -> bool:
    if per is not None and per <= 0:  # 적자(주당순이익 마이너스)
        return False
    if roe is not None and roe < MIN_ROE_PCT:
        return False
    return True


def _selftest() -> None:
    assert passes_financial_filter(10, 15) is True
    assert passes_financial_filter(-5, 15) is False, "PER 적자(음수)인데 통과함"
    assert passes_financial_filter(10, -3) is False, "ROE 음수(자본잠식)인데 통과함"
    assert passes_financial_filter(None, None) is True, "재무데이터 없으면 판단보류로 통과해야 함"


def _write_progress(state: dict) -> None:
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.flush()


def _commit_progress_checkpoint(label: str) -> None:
    """GitHub Actions 안에서만: discover_progress.json을 커밋/푸시해 웹호스팅 버전 앱이
    원격에서 폴링하며 단계별 진행상황을 실시간에 가깝게 보여줄 수 있게 한다.
    로컬 실행(server.py)에서는 파일을 직접 폴링하므로 이 함수가 아무것도 하지 않는다.
    커밋/푸시 실패는 진행표시 기능일 뿐이므로 무시하고 파이프라인을 계속 진행한다."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "add", PROGRESS_FILE], check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "commit", "-m", f"progress: {label}"], capture_output=True, text=True
        )
        if commit.returncode != 0:
            return  # 이전 커밋과 상태가 같음 등 — 조용히 넘어간다
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"], check=True, capture_output=True
        )
        subprocess.run(["git", "push"], check=True, capture_output=True)
    except Exception as e:
        print(f"  진행상황 커밋 실패(무시하고 계속): {e}")


def filter_common_stocks(codes: list[str]) -> list[str]:
    return [c for c in codes if c.endswith("0")]


def get_kr_candidates(limit: int = KR_LIMIT) -> list[tuple[str, str]]:
    df = fdr.StockListing("KRX")
    common_codes = set(filter_common_stocks(df["Code"].tolist()))
    df = df[df["Code"].isin(common_codes)].sort_values("Marcap", ascending=False)
    return [("kr", code) for code in df["Code"].head(limit)]


SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def get_sp500_symbols() -> list[str]:
    resp = requests.get(
        SP500_WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "constituents"})
    rows = table.find_all("tr")[1:]
    return [row.find("td").text.strip().replace(".", "-") for row in rows]


def get_us_candidates(limit: int = US_LIMIT) -> list[tuple[str, str]]:
    symbols = get_sp500_symbols()

    def _market_cap(symbol: str):
        try:
            cap = yf.Ticker(symbol).fast_info["marketCap"]
        except Exception as e:
            print(f"  {symbol} 시총 조회 실패: {e}")
            return None
        if cap is None:
            print(f"  {symbol} 시총 없음, 제외")
        return cap

    caps = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for symbol, cap in zip(symbols, executor.map(_market_cap, symbols)):
            if cap is not None:
                caps.append((symbol, cap))
    caps.sort(key=lambda pair: pair[1], reverse=True)
    return [("us", symbol) for symbol, _ in caps[:limit]]


def fetch_kr_financials(tickers: list[str]) -> dict[str, dict]:
    """전종목 PER/PBR/ROE(근사)을 한 번에 조회한다(최근 영업일을 최대 5일 거슬러 시도).
    ROE는 EPS/BPS*100으로 근사한다(배당·자사주매입은 반영 안 되는 단순 추정치).
    KRX 재무 API가 응답을 못 주면(관측된 바 있음) 빈 dict를 반환 — 호출부가 종목별로
    "데이터 없음 → 판단 보류로 통과"로 처리한다."""
    end = datetime.now()
    for i in range(6):
        date_str = (end - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = pykrx_stock.get_market_fundamental_by_ticker(date_str, market="ALL")
        except Exception as e:
            print(f"  KR 재무데이터 조회 실패({date_str}): {e}")
            continue
        if df.empty:
            continue
        result = {}
        for ticker in tickers:
            if ticker not in df.index:
                continue
            row = df.loc[ticker]
            per = float(row["PER"]) if pd.notna(row["PER"]) else None
            pbr = float(row["PBR"]) if pd.notna(row["PBR"]) else None
            eps = float(row["EPS"]) if pd.notna(row["EPS"]) else None
            bps = float(row["BPS"]) if pd.notna(row["BPS"]) else None
            roe = round(eps / bps * 100, 2) if eps is not None and bps else None
            result[ticker] = {"per": per, "pbr": pbr, "roe": roe}
        return result
    print("  KR 재무데이터 전체 조회 실패 — 1단계 재무 필터를 전종목 판단보류로 통과시킵니다.")
    return {}


def fetch_us_financials(symbols: list[str]) -> dict[str, dict]:
    def _fetch(symbol: str):
        try:
            info = yf.Ticker(symbol).info
        except Exception as e:
            print(f"  {symbol} 재무데이터 조회 실패: {e}")
            return symbol, None
        per = info.get("trailingPE")
        pbr = info.get("priceToBook")
        roe_raw = info.get("returnOnEquity")
        roe = round(roe_raw * 100, 2) if isinstance(roe_raw, (int, float)) else None
        return symbol, {"per": per, "pbr": pbr, "roe": roe}

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for symbol, data in executor.map(_fetch, symbols):
            if data is not None:
                result[symbol] = data
    return result


def classify_tags(per: Optional[float], dividend_yield: Optional[float]) -> list[str]:
    # ponytail: PER>=25 as a growth-stock proxy (no earnings-growth data in this
    # pipeline) — upgrade to real revenue/EPS growth if precision matters later.
    tags = []
    if dividend_yield is not None and dividend_yield >= 3.0:
        tags.append("고배당주")
    if per is not None and per >= 25:
        tags.append("성장주")
    return tags


def fetch_fundamentals(market: str, ticker: str) -> tuple[Optional[float], Optional[float]]:
    """반환: (PER, 배당수익률%) — 조회 실패 시 (None, None)."""
    if market == "kr":
        end = datetime.now()
        start = end - timedelta(days=7)
        df = pykrx_stock.get_market_fundamental(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        if df.empty:
            return None, None
        row = df.iloc[-1]
        per = float(row["PER"]) if pd.notna(row["PER"]) else None
        div = float(row["DIV"]) if pd.notna(row["DIV"]) else None
        return per, div

    info = yf.Ticker(ticker).info
    per = info.get("trailingPE")
    raw_yield = info.get("dividendYield")
    # yfinance는 dividendYield를 이미 %단위 숫자로 준다(예: 0.44는 0.44%) — 예전엔 0~1
    # 소수(0.0044)였어서 *100 보정이 있었는데, 지금은 그 보정이 NVDA 배당수익률을
    # 45%로 만드는 등 값을 열 배 넘게 부풀리는 버그였다. 그대로 반환한다.
    div = round(raw_yield, 2) if isinstance(raw_yield, (int, float)) else None
    return per, div


def select_candidates(
    analyzed: list[dict], kr_limit: int = FINAL_LIMIT_PER_MARKET, us_limit: int = FINAL_LIMIT_PER_MARKET
) -> list[dict]:
    """시장(국내/미국)별로 매수 의견 중 점수가 가장 높은 종목만, 시장별로 지정된 개수만큼
    골라 최고 후보를 압축한다. 매수 의견이 그 개수보다 적으면 있는 만큼만 담는다."""
    limits = {"kr": kr_limit, "us": us_limit}
    result = []
    for market in ("kr", "us"):
        buys = [a for a in analyzed if a["market"] == market and a.get("opinion") == "매수"]
        buys.sort(key=lambda a: a["score"], reverse=True)
        result.extend(buys[: limits[market]])
    return result


_CHECKLIST_DISPLAY = [
    ("moat", "① 경쟁 해자"),
    ("consistent_earnings", "② 꾸준한 수익"),
    ("financial_health", "③ 재무 건전성"),
    ("shareholder_friendly", "④ 주주 친화적 경영"),
    ("understandable", "⑤ 이해하기 쉬운 사업"),
]


def _numbered(items: list) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


def write_report(candidates: list[dict], date: str) -> None:
    """PASS 판정 종목만 상세 수록한 발굴 보고서를 .md/.html로 저장한다."""
    passed = [c for c in candidates if c.get("buffett_review", {}).get("stage3_verdict") == "PASS"]

    md = [f"# 종목 발굴 보고서 ({date})", ""]
    md.append(
        "**파이프라인**: 1단계 재무적 분석(시총 상위 스크리닝 → PER/ROE 필터) → "
        "2단계 기술적 분석(RSI/이동평균/MACD) → 3단계 워런버핏 검토(시장별 지정 개수만큼 최고 후보) → "
        "4단계 최종 종목 결정(PASS 판정만 채택)"
    )
    md.append("")
    if not passed:
        md.append("이번 회차는 PASS 판정 종목이 없습니다.")
    else:
        md.append("## 한줄 요약")
        md.append("")
        md.append("| 종목 | 시장 | 한줄총평|")
        md.append("|---|---|---|")
        for c in passed:
            market_label = "국내" if c["market"] == "kr" else "미국"
            summary = c["buffett_review"]["stage3_conclusion"]
            md.append(f"| **{c['ticker']}** {c['name']} | {market_label} | {summary} |")
        md.append("")
        for c in passed:
            market_label = "국내" if c["market"] == "kr" else "미국"
            review = c["buffett_review"]
            checklist = review["checklist"]
            score = review["stage4"]["score"]
            md.append("---")
            md.append("")
            md.append(f"## {c['ticker']} · {c['name']} ({market_label})")
            md.append("")
            md.append("**3단계 체크리스트**")
            for key, label in _CHECKLIST_DISPLAY:
                md.append(f"- {label} {checklist[key]}")
            md.append("")
            md.append(f"**밸류에이션**: {review['valuation']}")
            md.append("")
            md.append(
                f"**4단계 점수**: PER {score['per']}/10, PBR {score['pbr']}/10, "
                f"ROE {score['roe']}/10, 재무안정성 {score['financial_health']}/10"
            )
            md.append("")
            md.append("**매수 이유**")
            md.append(_numbered(review["stage4"]["buy_reasons"]))
            md.append("")
            md.append("**리스크 이유**")
            md.append(_numbered(review["stage4"]["risk_reasons"]))
            md.append("")
            md.append(f"**초보자 조언**: {review['stage4']['beginner_advice']}")
            md.append("")
    md_text = "\n".join(md)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(f"{REPORTS_DIR}/{date}-발굴보고서.md", "w", encoding="utf-8") as f:
        f.write(md_text)

    def _inline(text: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))

    html_body = []
    for line in md_text.splitlines():
        if line.startswith("## "):
            html_body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            html_body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line == "---":
            html_body.append("<hr>")
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(line.replace("|", "").strip()) <= {"-"}:
                continue
            tag = "th" if "종목" in cells else "td"
            html_body.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
        elif line.strip() == "":
            html_body.append("")
        else:
            html_body.append(f"<p>{_inline(line)}</p>")
    # 표가 아닌 부분까지 <table> 안에 들어가면 무효 마크업이라 렌더가 깨진다 —
    # 실제로는 문단/표를 분리해서 감싸야 하므로, 표 라인만 <table>로 감싸고 나머진 그대로 이어붙인다.
    html_parts = []
    in_table = False
    for line in html_body:
        is_row = line.startswith("<tr>")
        if is_row and not in_table:
            html_parts.append("<table>")
            in_table = True
        elif not is_row and in_table:
            html_parts.append("</table>")
            in_table = False
        html_parts.append(line)
    if in_table:
        html_parts.append("</table>")
    html_text = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><title>종목 발굴 보고서 ({date})</title>
<style>
body {{ font-family: "Malgun Gothic", sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #1e293b; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 18px; margin-top: 32px; color: #4f46e5; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 14px; }}
th {{ background: #f1f5f9; }}
hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 24px 0; }}
p {{ font-size: 14px; margin: 6px 0; }}
</style></head><body>
{"".join(html_parts)}
</body></html>"""
    with open(f"{REPORTS_DIR}/{date}-발굴보고서.html", "w", encoding="utf-8") as f:
        f.write(html_text)


def interleave(kr: list, us: list) -> list:
    return [
        p
        for pair in zip_longest(kr, us)
        for p in pair
        if p is not None
    ]


def _read_count(env_name: str) -> int:
    try:
        return max(0, int(os.environ.get(env_name, FINAL_LIMIT_PER_MARKET)))
    except ValueError:
        return FINAL_LIMIT_PER_MARKET


def main() -> None:
    _selftest()
    kr_limit = _read_count("DISCOVER_KR_COUNT")
    us_limit = _read_count("DISCOVER_US_COUNT")
    if kr_limit == 0 and us_limit == 0:
        print("국내/해외 종목수량이 모두 0 — 발굴할 대상이 없어 기존 candidates.json을 그대로 두고 종료합니다.")
        _write_progress({
            "stage": "done",
            "stage_label": "완료 (발굴 대상 없음)",
            "done": True,
            "error": "국내/해외 종목수량이 모두 0으로 설정되어 발굴을 건너뛰었습니다. 기존 candidates.json은 변경되지 않았습니다.",
            "final_passed": None,
        })
        return

    progress = {
        "stage": 1,
        "stage_label": "1단계: 재무적 분석 (시총 스크리닝 → PER/ROE 필터)",
        "kr_done": False,
        "us_done": False,
        "candidates_found": 0,
        "financial_total": 0,
        "financial_checked": 0,
        "financial_passed": 0,
        "analyzed": 0,
        "total_to_analyze": 0,
        "buy_found": 0,
        "final_passed": None,
        "current": None,
        "recent": [],
        "review_total": 0,
        "review_done": 0,
        "review_current": None,
        "done": False,
    }
    _write_progress(progress)

    print("1단계: 시총 상위 스크리닝...")
    kr_candidates = get_kr_candidates()
    progress["kr_done"] = True
    _write_progress(progress)

    us_candidates = get_us_candidates()
    progress["us_done"] = True
    screened = interleave(kr_candidates, us_candidates)
    progress["candidates_found"] = len(screened)
    print(f"  시총 상위 후보 {len(screened)}개 확보")
    _write_progress(progress)

    print("1단계: 재무적 분석 (PER/ROE 필터)...")
    kr_financials = fetch_kr_financials([t for m, t in screened if m == "kr"])
    us_financials = fetch_us_financials([t for m, t in screened if m == "us"])
    financials = {**kr_financials, **us_financials}

    pairs = []
    progress["financial_total"] = len(screened)
    for market, ticker in screened:
        fin = financials.get(ticker)
        progress["financial_checked"] += 1
        if fin is None:
            pairs.append((market, ticker))  # 데이터 없음 — 판단 보류로 통과
            continue
        if passes_financial_filter(fin["per"], fin["roe"]):
            pairs.append((market, ticker))
            progress["financial_passed"] += 1
    _write_progress(progress)
    print(f"  재무 필터 통과 {len(pairs)} / {len(screened)}개 (데이터 없어 판단보류 포함)")
    _commit_progress_checkpoint("1단계 완료")

    print("2단계: 기술적 지표 분석...")
    progress["stage"] = 2
    progress["stage_label"] = "2단계: 기술적 분석 (RSI/이동평균/MACD)"
    progress["total_to_analyze"] = len(pairs)
    _write_progress(progress)

    analyzed = []
    for market, ticker in pairs:
        progress["current"] = {"market": market, "ticker": ticker}
        try:
            # 200개 안팎을 스크리닝하는 단계라 LLM 호출은 비용/시간이 너무 크다 —
            # 여기선 저렴한 퀀트 점수식으로만 1차 필터링하고, 상위 종목의 정밀 분석은
            # 별도로(3단계, 워런버핏 검토) 처리한다.
            result = analyze_ticker(market, ticker, use_llm=False)
        except Exception as e:
            print(f"  [{market}] {ticker} 분석 실패: {e}")
            progress["analyzed"] += 1
            progress["recent"] = (
                [{"market": market, "ticker": ticker, "ok": False}] + progress["recent"]
            )[:8]
            _write_progress(progress)
            continue
        entry = {
            "market": result["market"],
            "ticker": ticker,
            "name": result.get("name", ticker),
            "price": result["price"],
            "change_pct": result.get("change_pct"),
            "score": result["score"],
            "opinion": result["opinion"],
            "comment": result["comment"],
            "rsi": result["rsi"],
            "ma5": result["ma5"],
            "ma20": result["ma20"],
        }
        if "supply_demand" in result:
            entry["supply_demand"] = result["supply_demand"]
        analyzed.append(entry)
        progress["analyzed"] += 1
        if entry["opinion"] == "매수":
            progress["buy_found"] += 1
        progress["recent"] = (
            [
                {
                    "market": market,
                    "ticker": ticker,
                    "ok": True,
                    "opinion": entry["opinion"],
                }
            ]
            + progress["recent"]
        )[:8]
        _write_progress(progress)

    candidates = select_candidates(analyzed, kr_limit=kr_limit, us_limit=us_limit)
    _commit_progress_checkpoint("2단계 완료")

    print("PER/배당수익률 조회 및 태그 분류...")
    for c in candidates:
        try:
            per, div = fetch_fundamentals(c["market"], c["ticker"])
        except Exception as e:
            print(f"  [{c['market']}] {c['ticker']} 재무지표 조회 실패: {e}")
            per, div = None, None
        c["per"] = round(per, 1) if per is not None else None
        c["dividend_yield"] = round(div, 2) if div is not None else None
        c["tags"] = classify_tags(per, div)

    print(f"3단계: 워런 버핏 체크리스트 심층 리뷰 (국내 {kr_limit}개 · 미국 {us_limit}개)...")
    progress["stage"] = 3
    progress["stage_label"] = "3단계: 워런 버핏 검토"
    progress["review_total"] = len(candidates)
    _write_progress(progress)
    _commit_progress_checkpoint("3단계 시작")

    for c in candidates:
        progress["review_current"] = {"market": c["market"], "ticker": c["ticker"], "name": c["name"]}
        _write_progress(progress)
        review = generate_review(
            c["name"], c["ticker"], c["market"], c["price"], c["rsi"], c["per"], c["dividend_yield"]
        )
        if review:
            c["warren_score"] = review["warren_score"]
            c["buffett_review"] = review["buffett_review"]
        progress["review_done"] += 1
        _write_progress(progress)
        _commit_progress_checkpoint(f"3단계 리뷰 {progress['review_done']}/{progress['review_total']}")

    progress["stage"] = 4
    progress["stage_label"] = "4단계: 최종 종목 결정 (PASS 판정만 채택)"
    progress["review_current"] = None
    _write_progress(progress)

    # 4단계: PASS 판정을 받은 종목만 "지금 시점 매수 추천"으로 최종 채택한다.
    # WAIT/REJECT는 기술적으로는 매수 신호였지만 버핏 심사역이 보류·기각한
    # 종목이므로 결과에 남기지 않는다(보고서·앱 카드 노출 기준을 일치시키기 위함).
    wait_count = sum(1 for c in candidates if c.get("warren_score", {}).get("verdict") == "WAIT")
    reject_count = sum(1 for c in candidates if c.get("warren_score", {}).get("verdict") == "REJECT")
    unreviewed_count = sum(1 for c in candidates if "warren_score" not in c)
    final_candidates = [c for c in candidates if c.get("warren_score", {}).get("verdict") == "PASS"]
    print(
        f"  4단계 판정 결과 — PASS {len(final_candidates)} / WAIT {wait_count} / "
        f"REJECT {reject_count} / 리뷰실패(미판정) {unreviewed_count}"
    )

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": final_candidates,
    }
    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open("candidates.js", "w", encoding="utf-8") as f:
        f.write("window.CANDIDATES_DATA = ")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    report_date = datetime.now().strftime("%Y-%m-%d")
    write_report(final_candidates, report_date)

    progress["stage"] = "done"
    progress["stage_label"] = "완료"
    progress["current"] = None
    progress["done"] = True
    progress["final_passed"] = len(final_candidates)
    _write_progress(progress)
    _commit_progress_checkpoint("완료")

    print(f"완료: 최종 매수 추천 {len(final_candidates)}개 (검토 {len(candidates)}개 중 PASS) → {CANDIDATES_FILE}, reports/{report_date}-발굴보고서.html 저장")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        state["error"] = str(e)
        state["done"] = True
        _write_progress(state)
        raise
