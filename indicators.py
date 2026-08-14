"""RSI / 이동평균 / MACD 계산과 매수-매도-보유 의견 산출."""
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
from pykrx import stock as pykrx_stock


def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def compute_ma(closes: pd.Series, window: int) -> float:
    return float(closes.rolling(window).mean().iloc[-1])


def compute_macd(
    closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[float, float]:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1])


def compute_bollinger(
    closes: pd.Series, window: int = 20, num_std: float = 2.0
) -> Tuple[float, float, float]:
    mid = closes.rolling(window).mean()
    std = closes.rolling(window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])


def compute_stochastic(
    highs: pd.Series, lows: pd.Series, closes: pd.Series,
    k_period: int = 14, d_period: int = 3,
) -> Tuple[float, float]:
    lowest_low = lows.rolling(k_period).min()
    highest_high = highs.rolling(k_period).max()
    percent_k = (closes - lowest_low) / (highest_high - lowest_low) * 100
    percent_d = percent_k.rolling(d_period).mean()
    return float(percent_k.iloc[-1]), float(percent_d.iloc[-1])


def score_and_opinion(
    rsi: float, ma5: float, ma20: float, macd_line: float, macd_signal: float
) -> Tuple[int, str, str]:
    score = 0
    reasons = []

    if rsi < 30:
        score += 1
        reasons.append(f"RSI {rsi:.1f} 과매도")
    elif rsi > 70:
        score -= 1
        reasons.append(f"RSI {rsi:.1f} 과매수")
    else:
        reasons.append(f"RSI {rsi:.1f} 중립")

    if ma5 > ma20:
        score += 1
        reasons.append("골든크로스")
    else:
        score -= 1
        reasons.append("데드크로스")

    if macd_line > macd_signal:
        score += 1
        reasons.append("MACD 상승전환")
    else:
        score -= 1
        reasons.append("MACD 하락전환")

    if score >= 2:
        opinion = "매수"
    elif score <= -2:
        opinion = "매도"
    else:
        opinion = "보유"

    comment = ", ".join(reasons) + f" → {opinion} 우위"
    return score, opinion, comment


def _foreign_streak(foreign_net: pd.Series) -> int:
    """외국인 순매수대금 시계열(과거→최근)에서, 가장 최근 거래일부터 거꾸로 부호가 이어지는
    연속일수를 센다. 양수면 연속 순매수 일수, 음수면 -연속 순매도 일수, 0이면 스트릭 없음(중립).
    부호가 바뀌거나 값이 0인 날을 만나면 그 지점에서 스트릭이 끊긴 것으로 본다."""
    values = list(foreign_net.iloc[::-1])  # 최근 → 과거
    if not values or values[0] == 0:
        return 0
    sign = 1 if values[0] > 0 else -1
    streak = 0
    for v in values:
        if (v > 0) == (sign > 0) and v != 0:
            streak += 1
        else:
            break
    return streak * sign


def _institution_direction(institution_net: pd.Series) -> str:
    """기관 비중 확대/축소/유지 판단 규칙(단순화된 기준): 최근 조회 구간 동안의 기관
    순매수대금 합계 부호로 판단한다 — 합계가 양수면 '확대', 음수면 '축소', 정확히 0이면 '유지'."""
    total = institution_net.sum()
    if total > 0:
        return "확대"
    if total < 0:
        return "축소"
    return "유지"


def _foreign_comment(streak: int) -> str:
    if streak > 0:
        return f"외국인이 {streak}거래일 연속으로 사고 있습니다."
    if streak < 0:
        return f"외국인이 {abs(streak)}거래일 연속으로 팔고 있습니다."
    return "외국인 매매가 뚜렷한 방향 없이 중립적입니다."


def _institution_comment(state: str) -> str:
    return {
        "확대": "기관이 비중을 늘리고 있습니다.",
        "축소": "기관이 비중을 줄이고 있습니다.",
        "유지": "기관이 비중을 큰 변화 없이 유지하고 있습니다.",
    }[state]


_SUPPLY_DEMAND_SUMMARY = {
    ("buy", "확대"): "외국인과 기관이 동시에 사들이고 있어 수급이 우호적인 편입니다. 다만 매수 판단은 실적 등 다른 정보와 함께 참고하세요.",
    ("buy", "축소"): "외국인은 사고 기관은 팔고 있어 수급 신호가 엇갈립니다. 단기 방향성은 좀 더 지켜보는 게 좋습니다.",
    ("buy", "유지"): "외국인 매수세가 이어지고 있어 수급은 우호적인 편입니다.",
    ("sell", "확대"): "외국인은 팔고 기관은 사고 있어 수급 신호가 엇갈립니다. 단기 방향성은 좀 더 지켜보는 게 좋습니다.",
    ("sell", "축소"): "외국인과 기관이 동시에 팔고 있어 수급이 부담스러운 상황입니다. 신규 매수는 신중하게 접근하세요.",
    ("sell", "유지"): "외국인 매도세가 이어지고 있어 수급이 다소 부담스러운 상황입니다.",
    ("neutral", "확대"): "외국인 수급은 중립적이지만 기관이 비중을 늘리고 있어 매수 우위 신호로 볼 수 있습니다.",
    ("neutral", "축소"): "외국인 수급은 중립적이지만 기관이 비중을 줄이고 있어 매도 우위 신호로 볼 수 있습니다.",
    ("neutral", "유지"): "외국인과 기관 모두 뚜렷한 방향 없이 관망하는 모습입니다.",
}


def analyze_supply_demand(ticker: str, lookback_days: int = 20) -> Optional[dict]:
    """국내 종목의 최근 기관/외국인 수급(순매수대금) 흐름을 초보자용 코멘트로 요약한다.
    해외 종목은 pykrx 데이터가 없어 이 함수를 호출하지 않는다(호출부에서 market == "kr"일 때만 사용).

    판단 기준(둘 다 최근 lookback_days 거래일 기준):
    - 외국인 연속 순매수/순매도 일수: _foreign_streak 참고 — 최근일부터 거꾸로 부호가
      이어지는 일수.
    - 기관 비중 확대/축소/유지: _institution_direction 참고 — 순매수대금 합계의 부호.

    데이터가 없으면(신규상장 등) None을 반환한다.
    """
    end = datetime.now()
    start = end - timedelta(days=lookback_days * 2)  # 주말/휴장 감안 여유
    df = pykrx_stock.get_market_trading_value_by_date(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
    )
    if df.empty:
        return None
    df = df.tail(lookback_days)

    streak = _foreign_streak(df["외국인합계"])
    institution_state = _institution_direction(df["기관합계"])
    foreign_bucket = "buy" if streak > 0 else "sell" if streak < 0 else "neutral"

    return {
        "foreign_comment": _foreign_comment(streak),
        "institution_comment": _institution_comment(institution_state),
        "summary": _SUPPLY_DEMAND_SUMMARY[(foreign_bucket, institution_state)],
    }


if __name__ == "__main__":
    uptrend = pd.Series([100 + i for i in range(40)], dtype=float)
    downtrend = pd.Series([140 - i for i in range(40)], dtype=float)

    assert compute_rsi(uptrend) > 70, "꾸준한 상승 추세인데 RSI가 과매수 영역이 아님"
    assert compute_rsi(downtrend) < 30, "꾸준한 하락 추세인데 RSI가 과매도 영역이 아님"

    ma5_up = compute_ma(uptrend, 5)
    ma20_up = compute_ma(uptrend, 20)
    assert ma5_up > ma20_up, "상승 추세인데 5일선이 20일선보다 낮음(골든크로스 아님)"

    macd_line_up, macd_signal_up = compute_macd(uptrend)
    assert macd_line_up > macd_signal_up, "상승 추세인데 MACD선이 시그널선보다 낮음"

    score, opinion, _ = score_and_opinion(rsi=25, ma5=110, ma20=100, macd_line=5, macd_signal=1)
    assert score == 3 and opinion == "매수", f"과매도+골든크로스+MACD상승인데 매수가 아님: {score}, {opinion}"

    score, opinion, _ = score_and_opinion(rsi=75, ma5=90, ma20=100, macd_line=1, macd_signal=5)
    assert score == -3 and opinion == "매도", f"과매수+데드크로스+MACD하락인데 매도가 아님: {score}, {opinion}"

    score, opinion, _ = score_and_opinion(rsi=50, ma5=110, ma20=100, macd_line=1, macd_signal=5)
    assert score == 0 and opinion == "보유", f"중립+골든크로스+MACD하락인데 보유가 아님: {score}, {opinion}"

    bb_upper, bb_mid, bb_lower = compute_bollinger(uptrend)
    assert bb_upper > bb_mid > bb_lower, "볼린저 밴드 상단/중단/하단 순서가 어긋남"

    highs = uptrend + 1
    lows = uptrend - 1
    stoch_k, stoch_d = compute_stochastic(highs, lows, uptrend)
    assert stoch_k > 50, "꾸준한 상승 추세인데 스토캐스틱 %K가 낮음"

    buy_streak = _foreign_streak(pd.Series([100, -50, 30, 20, 10]))  # 과거→최근
    assert buy_streak == 3, f"연속 순매수일수 계산이 어긋남: {buy_streak}"
    sell_streak = _foreign_streak(pd.Series([100, 50, -30, -20, -10]))
    assert sell_streak == -3, f"연속 순매도일수 계산이 어긋남: {sell_streak}"
    assert _foreign_streak(pd.Series([10, 20, 0])) == 0, "최근일 순매매가 0인데 중립으로 안 나옴"

    assert _institution_direction(pd.Series([10, -5, 20])) == "확대", "순매수 합계가 양수인데 비중 확대가 아님"
    assert _institution_direction(pd.Series([-10, 5, -20])) == "축소", "순매수 합계가 음수인데 비중 축소가 아님"
    assert _institution_direction(pd.Series([10, -10])) == "유지", "순매수 합계가 0인데 유지가 아님"

    assert "3거래일 연속으로 사고" in _foreign_comment(3)
    assert "3거래일 연속으로 팔고" in _foreign_comment(-3)
    assert "중립" in _foreign_comment(0)
    for foreign_bucket in ("buy", "sell", "neutral"):
        for institution_state in ("확대", "축소", "유지"):
            assert (foreign_bucket, institution_state) in _SUPPLY_DEMAND_SUMMARY, (
                f"수급 종합의견 표에 조합이 빠짐: {foreign_bucket}, {institution_state}"
            )

    print("indicators.py self-check 통과")
