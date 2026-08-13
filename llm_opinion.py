"""Claude(웹검색 도구)로 종목별 투자 의견을 생성한다.
ANTHROPIC_API_KEY가 없거나 API 호출이 실패하면 기존 퀀트 점수식(score_and_opinion)으로 폴백해
파이프라인 전체가 죽지 않게 한다."""
import os
import re
from typing import Optional

from indicators import score_and_opinion

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """너는 주식 초보자들에게 복잡한 금융 데이터를 아주 쉽고 친절하게 설명해 주는 AI 주식 멘토야.
종목에 대한 펀더멘털(실적, 뉴스)과 기술적 지표(차트) 데이터를 종합하여 분석 결과를 출력할 때,
절대 길고 복잡한 줄글(Wall of text)로 작성하지 마. 전체적인 맥락은 빠짐없이 포함하되, 무조건
아래의 규칙과 양식을 엄격하게 지켜서 출력해.

[작성 규칙]
1. 가독성 극대화: 단락을 길게 쓰지 말고, 글머리 기호(-, *, 💡 등)를 적극 사용하여 시각적으로 분리해.
2. 초보자 눈높이: PER, PBR, MACD, RSI 같은 전문 용어를 사용할 때는 초보자가 이해할 수 있는 쉬운
   의미를 함께 적어줘. (예: "RSI 30 이하 (현재 주가가 많이 떨어져서 싸진 상태)")
3. 핵심 먼저: 부연 설명보다는 결론과 이유를 직관적으로 먼저 배치해.

[분석에 사용할 데이터]
1. 종목 정보: 종목명/티커, 현재가
2. 시장 및 뉴스: 최근 국내외 거시경제 상황 및 해당 기업의 주요 뉴스 (웹검색으로 직접 조사)
3. 애널리스트 목표가: 증권사들의 평균 목표가 및 괴리율 (웹검색으로 직접 조사)
4. 기술적 지표: 이동평균선(MA), RSI, MACD, 볼린저 밴드, 스토캐스틱

[출력 양식] (반드시 아래 구조를 그대로, 다른 말 없이 따를 것)

■ 최종 의견: [매수 / 보유 / 일부익절 / 매도 중 택 1]
■ 한 줄 요약: [초보자도 단번에 이해할 수 있는 매매 핵심 이유 1문장]

💡 왜 이런 의견을 냈나요? (분석 요약)
- 🏢 기업 실적/이슈: [매출 상태나 주요 호재/악재를 쉬운 말로 1~2줄 요약]
- 📈 현재 차트 상태: [현재 주가 흐름과 기술적 지표가 의미하는 바를 쉬운 말로 1~2줄 요약]
- 🎯 전문가 전망: [증권사 목표가와 현재 주가의 차이(괴리율) 등을 간단히 언급]

💰 실전 대응 전략
- 진입(살 가격): [00,000원 ~ 00,000원 부근]
- 목표(팔 가격): [1차: 00,000원 / 2차: 00,000원]
- 손절(위험 가격): [00,000원 이탈 시 손절 (이유 짧게 덧붙임)]"""

_RESPONSE_PATTERN = re.compile(
    r"■\s*최종 의견:\s*(?P<opinion>\S+)\s*"
    r"■\s*한 줄 요약:\s*(?P<comment>.+?)\s*"
    r"💡.*?\n"
    r"-\s*🏢\s*기업 실적/이슈:\s*(?P<biz>.+?)\s*"
    r"-\s*📈\s*현재 차트 상태:\s*(?P<chart>.+?)\s*"
    r"-\s*🎯\s*전문가 전망:\s*(?P<analyst>.+?)\s*"
    r"💰.*?\n"
    r"-\s*진입\(살 가격\):\s*(?P<entry>.+?)\s*"
    r"-\s*목표\(팔 가격\):\s*(?P<target>.+?)\s*"
    r"-\s*손절\(위험 가격\):\s*(?P<stop>.+)",
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
    analysis = (
        f"🏢 기업 실적/이슈: {match.group('biz').strip()}\n"
        f"📈 현재 차트 상태: {match.group('chart').strip()}\n"
        f"🎯 전문가 전망: {match.group('analyst').strip()}"
    )
    strategy = (
        f"진입(살 가격): {match.group('entry').strip()}\n"
        f"목표(팔 가격): {match.group('target').strip()}\n"
        f"손절(위험 가격): {match.group('stop').strip()}"
    )
    return {
        "opinion": opinion,
        "comment": match.group("comment").strip(),
        "analysis": analysis,
        "strategy": strategy,
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

    sample_text = """■ 최종 의견: 일부익절
■ 한 줄 요약: 단기간 많이 올라서 잠깐 쉬어갈 수 있으니 일부는 이익을 챙겨두는 게 좋아요.

💡 왜 이런 의견을 냈나요? (분석 요약)
- 🏢 기업 실적/이슈: 최근 실적 발표가 예상보다 좋아서 주가가 급등했어요.
- 📈 현재 차트 상태: RSI 78 (많이 올라서 비싸진 상태) 이고 볼린저 밴드 상단을 뚫었어요.
- 🎯 전문가 전망: 증권사 평균 목표가와 거의 비슷한 수준까지 올라왔어요.

💰 실전 대응 전략
- 진입(살 가격): 250,000원 ~ 255,000원 부근
- 목표(팔 가격): 1차: 270,000원 / 2차: 285,000원
- 손절(위험 가격): 235,000원 이탈 시 손절 (20일선 붕괴 신호)"""
    parsed = _parse_response(sample_text)
    assert parsed is not None, "정상 형식 응답 파싱 실패"
    assert parsed["opinion"] == "일부익절", parsed
    assert "RSI 78" in parsed["analysis"], parsed["analysis"]
    assert "270,000원" in parsed["strategy"], parsed["strategy"]

    assert _parse_response("형식이 전혀 다른 응답") is None, "잘못된 형식인데 파싱이 성공함"

    print("llm_opinion.py self-check 통과")
