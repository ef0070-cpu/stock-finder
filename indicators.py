"""RSI / 이동평균 / MACD 계산과 매수-매도-보유 의견 산출."""
from typing import Tuple
import pandas as pd


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

    print("indicators.py self-check 통과")

    print("indicators.py self-check 통과")
