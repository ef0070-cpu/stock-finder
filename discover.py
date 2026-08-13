"""종목 발굴 파이프라인(1~4단계) 자동화 스크립트 — index.html의 "종목 발굴 시작" 버튼이
server.py의 /discover 엔드포인트를 통해 이 스크립트를 실행한다.
run_pipeline.bat/서버 업데이트 버튼과는 완전히 독립적으로, 필요할 때 직접 실행한다.

1단계: 국내 시총 100 + 미국 S&P500 시총 100 스크리닝
2단계: 저렴한 퀀트 점수식(RSI/MA/MACD)으로 기술적 필터링
3~4단계: 시장(국내/미국)별로 사용자가 지정한 개수만큼(기본 1개씩)의 최고 후보만
         골라 Claude(웹검색)로 워런 버핏 체크리스트 심층 리뷰 — 전수(최대 200종목)가
         아니라 소수 종목에만 LLM을 호출해 비용·시간을 억제한다. 개수는
         DISCOVER_KR_COUNT/DISCOVER_US_COUNT 환경변수로 조절한다.
"""
import json
import os
import re
from datetime import datetime, timedelta
from html import escape
from itertools import zip_longest
from typing import Optional

import FinanceDataReader as fdr
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


def _write_progress(state: dict) -> None:
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.flush()


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
    caps = []
    for symbol in get_sp500_symbols():
        try:
            cap = yf.Ticker(symbol).fast_info["marketCap"]
        except Exception as e:
            print(f"  {symbol} 시총 조회 실패: {e}")
            continue
        if cap is None:
            print(f"  {symbol} 시총 없음, 제외")
            continue
        caps.append((symbol, cap))
    caps.sort(key=lambda pair: pair[1], reverse=True)
    return [("us", symbol) for symbol, _ in caps[:limit]]


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
        per = float(row["PER"]) if row["PER"] else None
        div = float(row["DIV"]) if row["DIV"] else None
        return per, div

    info = yf.Ticker(ticker).info
    per = info.get("trailingPE")
    raw_yield = info.get("dividendYield")
    div = round(raw_yield * 100, 2) if isinstance(raw_yield, (int, float)) and raw_yield < 1 else raw_yield
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
        "**파이프라인**: 1단계 종목발굴(국내 시총 100 + 미국 S&P500 시총 100) → "
        "2단계 기술적지표 필터 → 3~4단계 워런버핏 검토(시장별 지정 개수만큼 최고 후보)"
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


def main() -> None:
    progress = {
        "stage": 1,
        "stage_label": "1단계: 종목 발굴 (시총 상위 스크리닝)",
        "kr_done": False,
        "us_done": False,
        "candidates_found": 0,
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

    print("1단계: 종목 발굴 (시총 상위 스크리닝)...")
    kr_candidates = get_kr_candidates()
    progress["kr_done"] = True
    _write_progress(progress)

    us_candidates = get_us_candidates()
    progress["us_done"] = True
    pairs = interleave(kr_candidates, us_candidates)
    progress["candidates_found"] = len(pairs)
    print(f"  후보 {len(pairs)}개 확보")

    print("2단계: 기술적 지표 분석...")
    progress["stage"] = 2
    progress["stage_label"] = "2단계: 기술적 지표 분석"
    progress["total_to_analyze"] = len(pairs)
    _write_progress(progress)

    analyzed = []
    for market, ticker in pairs:
        progress["current"] = {"market": market, "ticker": ticker}
        try:
            # 200개 안팎을 스크리닝하는 단계라 LLM 호출은 비용/시간이 너무 크다 —
            # 여기선 저렴한 퀀트 점수식으로만 1차 필터링하고, 상위 종목의 정밀 분석은
            # 별도로(3~4단계, 워런버핏 검토) 처리한다.
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
        }
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

    def _read_count(env_name: str) -> int:
        try:
            return max(0, int(os.environ.get(env_name, FINAL_LIMIT_PER_MARKET)))
        except ValueError:
            return FINAL_LIMIT_PER_MARKET

    kr_limit = _read_count("DISCOVER_KR_COUNT")
    us_limit = _read_count("DISCOVER_US_COUNT")
    candidates = select_candidates(analyzed, kr_limit=kr_limit, us_limit=us_limit)

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

    print(f"3~4단계: 워런 버핏 체크리스트 심층 리뷰 (국내 {kr_limit}개 · 미국 {us_limit}개)...")
    progress["stage"] = 3
    progress["stage_label"] = "3~4단계: 워런 버핏 체크리스트 심층 리뷰"
    progress["review_total"] = len(candidates)
    _write_progress(progress)

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

    progress["stage"] = 4
    progress["stage_label"] = "4단계: 최종 결과 저장"
    progress["review_current"] = None
    _write_progress(progress)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": candidates,
    }
    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open("candidates.js", "w", encoding="utf-8") as f:
        f.write("window.CANDIDATES_DATA = ")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    report_date = datetime.now().strftime("%Y-%m-%d")
    write_report(candidates, report_date)

    progress["stage"] = "done"
    progress["stage_label"] = "완료"
    progress["current"] = None
    progress["done"] = True
    progress["final_passed"] = len(candidates)
    _write_progress(progress)

    pass_count = sum(1 for c in candidates if c.get("warren_score", {}).get("verdict") == "PASS")
    print(f"완료: 최고 후보 {len(candidates)}개 검토 (PASS {pass_count}개) → {CANDIDATES_FILE}, reports/{report_date}-발굴보고서.html 저장")


if __name__ == "__main__":
    main()
