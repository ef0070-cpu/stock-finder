"""Claude(웹검색 도구)로 종목별 투자 의견을 생성한다.
ANTHROPIC_API_KEY가 없거나 API 호출이 실패하면 기존 퀀트 점수식(score_and_opinion)으로 폴백해
파이프라인 전체가 죽지 않게 한다."""
import os
import re
from typing import Optional

from indicators import score_and_opinion

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """너는 월스트리트 최고 수준의 퀀트 애널리스트이자 주식 트레이더야.
사용자가 보유하거나 관심 있는 주식 종목의 데이터를 제공하면, 펀더멘털(뉴스/목표가)과
테크니컬(차트 지표) 데이터를 종합적으로 분석하여 명확한 투자 의견을 제시해야 해.

[분석에 사용할 데이터]
1. 종목 정보: 종목명/티커, 현재가, 사용자의 매입가
2. 시장 및 뉴스: 최근 국내외 거시경제 상황 및 해당 기업의 주요 뉴스 (웹검색으로 직접 조사)
3. 애널리스트 목표가: 증권사들의 평균 목표가 및 괴리율 (웹검색으로 직접 조사)
4. 기술적 지표:
   - 이동평균선(MA): 정배열/역배열 상태, 골든/데드크로스 여부
   - RSI: 과매수(70 이상) 및 과매도(30 이하) 판단
   - MACD: 시그널선 교차 여부 및 추세 강도
   - 볼린저 밴드(Bollinger Bands): 주가의 상단/하단 밴드 이탈 여부(과열/침체 판단)
   - 스토캐스틱(Stochastic): %K와 %D의 교차를 통한 단기 매수/매도 타이밍

[출력 및 판단 가이드라인]
위 데이터를 바탕으로 아래 4가지 중 하나의 최종 의견을 도출해.
- 매수: 상승 추세 초입, 확실한 저평가 구간, 강력한 호재 동반 시
- 보유: 현재 추세가 안정적으로 유지되고 있으며 목표가에 도달하지 않은 경우
- 일부익절: 단기 급등으로 지표가 과열(RSI 70 이상, 볼린저 밴드 상단 돌파 등)되었으나 중장기 호재가 남은 경우
- 매도: 하락 추세 전환, 주요 지지선 이탈, 악재 발생, 혹은 목표가 달성 후 모멘텀 소멸 시

[출력 형식 (반드시 아래 양식을 지켜서, 다른 말 없이 답변할 것)]
- 최종 의견: [매수 / 보유 / 일부익절 / 매도 중 택 1]
- 앱 코멘트용 한 줄 요약: [RSI 00 중립, 골든크로스, MACD 상승전환 → 매수 우위 등 50자 이내로 앱 대시보드에 표기할 간결한 코멘트]
- 종합 분석: [뉴스, 차트, 목표가를 바탕으로 해당 의견을 낸 구체적인 논리 (3~4문장)]
- 대응 전략: [적정 손절가 및 목표가 제시]"""

_RESPONSE_PATTERN = re.compile(
    r"-\s*최종 의견:\s*(?P<opinion>\S+)\s*"
    r"-\s*앱 코멘트용 한 줄 요약:\s*(?P<comment>.+?)\s*"
    r"-\s*종합 분석:\s*(?P<analysis>.+?)\s*"
    r"-\s*대응 전략:\s*(?P<strategy>.+)",
    re.DOTALL,
)

VALID_OPINIONS = {"매수", "보유", "일부익절", "매도"}


def _build_user_message(name: str, ticker: str, market: str, price: float, ind: dict) -> str:
    market_label = "국내" if market == "kr" else "해외"
    return f"""[종목 정보]
- 종목명/티커: {name} ({ticker})
- 시장: {market_label}
- 현재가: {price}

[기술적 지표]
- RSI(14): {ind['rsi']}
- 5일 이동평균선: {ind['ma5']}
- 20일 이동평균선: {ind['ma20']}
- MACD: {ind['macd']} / 시그널선: {ind['macd_signal']}
- 볼린저 밴드: 상단 {ind['bb_upper']} / 중단 {ind['bb_mid']} / 하단 {ind['bb_lower']}
- 스토캐스틱: %K {ind['stoch_k']} / %D {ind['stoch_d']}

웹검색으로 최근 뉴스와 애널리스트 목표가를 직접 조사한 뒤, 지정된 출력 형식에 맞춰 최종 의견을 내줘."""


def _parse_response(text: str) -> Optional[dict]:
    match = _RESPONSE_PATTERN.search(text)
    if not match:
        return None
    opinion = match.group("opinion").strip()
    if opinion not in VALID_OPINIONS:
        return None
    return {
        "opinion": opinion,
        "comment": match.group("comment").strip(),
        "analysis": match.group("analysis").strip(),
        "strategy": match.group("strategy").strip(),
    }


def quant_fallback(rsi: float, ma5: float, ma20: float, macd_line: float, macd_signal: float) -> dict:
    score, opinion, comment = score_and_opinion(rsi, ma5, ma20, macd_line, macd_signal)
    return {"opinion": opinion, "comment": comment, "analysis": None, "strategy": None, "score": score}


def generate_opinion(name: str, ticker: str, market: str, price: float, ind: dict) -> dict:
    """ind: rsi/ma5/ma20/macd/macd_signal/bb_upper/bb_mid/bb_lower/stoch_k/stoch_d 키를 포함한 dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"  [{ticker}] ANTHROPIC_API_KEY 없음 - 퀀트 점수식으로 폴백")
        return quant_fallback(ind["rsi"], ind["ma5"], ind["ma20"], ind["macd"], ind["macd_signal"])

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": _build_user_message(name, ticker, market, price, ind)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        parsed = _parse_response(text)
        if parsed is None:
            raise ValueError(f"응답 형식 파싱 실패: {text[:200]!r}")
        return parsed
    except Exception as e:
        print(f"  [{ticker}] LLM 의견 생성 실패({e}) - 퀀트 점수식으로 폴백")
        return quant_fallback(ind["rsi"], ind["ma5"], ind["ma20"], ind["macd"], ind["macd_signal"])


if __name__ == "__main__":
    fallback = quant_fallback(rsi=25, ma5=110, ma20=100, macd_line=5, macd_signal=1)
    assert fallback["opinion"] == "매수", f"폴백 로직이 퀀트 점수식과 다름: {fallback}"
    assert fallback["analysis"] is None and fallback["strategy"] is None

    sample_text = """- 최종 의견: 매수
- 앱 코멘트용 한 줄 요약: RSI 54 중립, 골든크로스, MACD 상승전환 → 매수 우위
- 종합 분석: 상반기 순이익이 작년 대비 약 193% 급증했고 자사주 소각 등 주주환원 정책도 발표했다. 현재가(40,650원)는 증권사 평균 목표가(약 65,000원) 대비 크게 저평가되어 있다. 단기 하락 후 주요 지지선을 회복하며 상승 흐름을 타기 시작했다.
- 대응 전략: 37,000원 이탈 시 손절, 1차 목표가 43,500원 / 중장기 목표가 57,000원"""
    parsed = _parse_response(sample_text)
    assert parsed is not None, "정상 형식 응답 파싱 실패"
    assert parsed["opinion"] == "매수", parsed
    assert "193%" in parsed["analysis"], parsed["analysis"]
    assert "43,500원" in parsed["strategy"], parsed["strategy"]

    assert _parse_response("형식이 전혀 다른 응답") is None, "잘못된 형식인데 파싱이 성공함"

    print("llm_opinion.py self-check 통과")
