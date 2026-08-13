"""tickers.json의 종목을 조회해 지표를 계산하고 data.js를 생성한다."""
import json
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from pykrx import stock as pykrx_stock

from indicators import compute_bollinger, compute_macd, compute_ma, compute_rsi, compute_stochastic
from llm_opinion import generate_opinion

TICKERS_FILE = "tickers.json"
OUTPUT_FILE = "data.js"
HISTORY_JSON_FILE = "history.json"
HISTORY_JS_FILE = "history.js"


def fetch_kr_ohlc(ticker: str) -> pd.DataFrame:
    end = datetime.now()
    start = end - timedelta(days=120)
    df = pykrx_stock.get_market_ohlcv(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
    )
    if df.empty:
        raise ValueError("가격 데이터 없음")
    return df.rename(columns={"종가": "close", "고가": "high", "저가": "low"})[
        ["close", "high", "low"]
    ].astype(float)


def fetch_us_ohlc(ticker: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period="6mo", interval="1d")
    if df.empty:
        raise ValueError("가격 데이터 없음")
    return df.rename(columns={"Close": "close", "High": "high", "Low": "low"})[
        ["close", "high", "low"]
    ].astype(float)


def get_usd_krw_rate() -> float:
    """해외 종목 가격을 원화로 환산해 보여줄 때 쓰는 참고 환율(투자 조언 아님)."""
    return float(yf.Ticker("KRW=X").fast_info["lastPrice"])


def analyze_ticker(market: str, ticker: str) -> dict:
    ohlc = fetch_kr_ohlc(ticker) if market == "kr" else fetch_us_ohlc(ticker)
    closes = ohlc["close"]
    if len(closes) < 30:
        raise ValueError(f"지표 계산에 필요한 데이터가 부족함 ({len(closes)}일)")

    rsi = compute_rsi(closes)
    ma5 = compute_ma(closes, 5)
    ma20 = compute_ma(closes, 20)
    macd_line, macd_signal = compute_macd(closes)
    bb_upper, bb_mid, bb_lower = compute_bollinger(closes)
    stoch_k, stoch_d = compute_stochastic(ohlc["high"], ohlc["low"], closes)

    values = [rsi, ma5, ma20, macd_line, macd_signal, bb_upper, bb_mid, bb_lower, stoch_k, stoch_d]
    if any(pd.isna(v) or v in (float("inf"), float("-inf")) for v in values):
        raise ValueError("지표 계산 결과가 유효하지 않음(NaN/Inf)")

    price = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])
    change_pct = round((price - prev_close) / prev_close * 100, 2)

    result = {
        "market": market,
        "price": round(price, 2),
        "change_pct": change_pct,
        "rsi": round(rsi, 1),
        "ma5": round(ma5, 1),
        "ma20": round(ma20, 1),
        "macd": round(macd_line, 2),
        "macd_signal": round(macd_signal, 2),
        "bb_upper": round(bb_upper, 2),
        "bb_mid": round(bb_mid, 2),
        "bb_lower": round(bb_lower, 2),
        "stoch_k": round(stoch_k, 1),
        "stoch_d": round(stoch_d, 1),
    }

    name = ticker
    if market == "kr":
        try:
            name = pykrx_stock.get_market_ticker_name(ticker)
            result["name"] = name
        except Exception:
            pass
    else:
        try:
            info = yf.Ticker(ticker).info
            yf_name = info.get("shortName") or info.get("longName")
            if yf_name:
                name = yf_name
                result["name"] = name
        except Exception:
            pass

    llm_result = generate_opinion(name, ticker, market, result["price"], result)
    result["opinion"] = llm_result["opinion"]
    result["comment"] = llm_result["comment"]
    if llm_result.get("analysis"):
        result["llm_analysis"] = llm_result["analysis"]
    if llm_result.get("strategy"):
        result["llm_strategy"] = llm_result["strategy"]
    if "score" in llm_result:
        result["score"] = llm_result["score"]

    return result


def load_tickers() -> list[tuple[str, str]]:
    with open(TICKERS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    pairs = []
    for market in ("kr", "us"):
        for ticker in raw.get(market, []):
            pairs.append((market, ticker))
    return pairs


def _load_history() -> dict:
    try:
        with open(HISTORY_JSON_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_history(history: dict) -> None:
    with open(HISTORY_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    with open(HISTORY_JS_FILE, "w", encoding="utf-8") as f:
        f.write("window.STOCK_HISTORY = ")
        json.dump(history, f, ensure_ascii=False, indent=2)
        f.write(";\n")


def main() -> None:
    pairs = load_tickers()
    if not pairs:
        print(f"{TICKERS_FILE}에 등록된 티커가 없습니다.")
        sys.exit(1)

    results: dict[str, dict] = {}
    for market, ticker in pairs:
        print(f"[{market}] {ticker} 분석 중...")
        try:
            results[ticker] = analyze_ticker(market, ticker)
        except Exception as e:
            print(f"  실패: {e}")
            results[ticker] = {"market": market, "error": str(e)}

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("window.STOCK_DATA = ")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    today = datetime.now().strftime("%Y-%m-%d")
    history = _load_history()
    history[today] = {"results": results}
    _save_history(history)

    ok_count = sum(1 for r in results.values() if "error" not in r)
    print(f"완료: {ok_count}/{len(results)}개 종목 분석 성공 → {OUTPUT_FILE} 저장")


if __name__ == "__main__":
    main()
