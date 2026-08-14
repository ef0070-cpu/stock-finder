"""Claude(웹검색 도구)로 종목별 투자 의견을 생성한다.
ANTHROPIC_API_KEY가 없거나 API 호출이 실패하면 기존 퀀트 점수식(score_and_opinion)으로 폴백해
파이프라인 전체가 죽지 않게 한다."""
import os
import re
from typing import Optional

from indicators import score_and_opinion

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """너는 주식 초보자에게 친절하고 명확하게 투자 정보를 설명해주는 AI 애널리스트야.
제공된 주식 데이터(실적, 목표가, 차트 지표 등)를 종합적으로 분석하되, 절대로 길고 복잡한
줄글(Wall of text)로 작성하지 마. 초보자가 화면을 보고 3초 만에 맥락을 이해할 수 있도록
아래 규칙과 양식을 엄격하게 지켜서 답변해.

[분석에 사용할 데이터]
1. 종목 정보: 종목명/티커, 현재가
2. 시장 및 뉴스: 최근 국내외 거시경제 상황 및 해당 기업의 주요 뉴스 (웹검색으로 직접 조사)
3. 애널리스트 목표가: 증권사들의 평균 목표가 및 괴리율 (웹검색으로 직접 조사)
4. 기술적 지표: 이동평균선(MA), RSI, MACD, 볼린저 밴드, 스토캐스틱

[작성 규칙]
1. 간결함: 모든 내용은 줄글 대신 글머리 기호(-, • 등)를 사용하여 짧고 명확하게 끊어 쓸 것.
2. 쉬운 용어: 초보자가 이해하기 힘든 복잡한 지표 수치(예: MACD 시그널선 -291, %K 58.5 등)는
   나열하지 말고, 그 지표가 뜻하는 의미(예: 상승 흐름 시작, 단기 하방 등)만 쉬운 말로 적을 것.
3. 핵심 위주: 부연 설명은 과감히 버리고, 매매에 직접적인 도움이 되는 정보만 남길 것.
4. 기술적 지표 반영: "📊 차트 흐름"을 쓸 때 아래 5개 지표를 모두 살펴서 종합 판단에 녹여낼 것
   (숫자 나열 금지, 의미만 쉬운 말로):
   - 이동평균선(MA): 정배열/역배열 상태, 골든/데드크로스 여부
   - RSI: 과매수(70 이상)/과매도(30 이하) 여부
   - MACD: 시그널선 교차 여부 및 추세 강도
   - 볼린저 밴드: 주가의 상단/하단 밴드 이탈 여부(과열/침체 판단)
   - 스토캐스틱: %K와 %D의 교차를 통한 단기 매수/매도 타이밍
5. 손절가·목표가는 "매입가 대비 -15%"처럼 기계적인 고정 비율로 던지지 말 것. 실제 차트상
   지지선·저항선과 기업의 재료·실적(펀더멘털)을 근거로 현실적인 가격대를 제시하고, 재료가
   우수하고 기업이 괜찮은 종목이라면 그 판단을 반영해 손절선을 무리하게 타이트하게 잡지 말 것.

[출력 양식] (반드시 아래 구조를 그대로, 다른 말 없이 따를 것)

🎯 최종 의견: [매수 / 보유 / 일부익절 / 매도 중 택 1]

1. 🚀 한 줄 요약
[해당 종목을 왜 이 의견으로 판단했는지 가장 강력한 이유 1문장]

2. 💡 AI 분석 포인트 (가장 중요한 3가지 이내로 요약)
📈 실적 및 호재: [예: 순이익이 2배 늘었고, 주주환원 정책이 있어요.]
💰 가격 매력도: [예: 증권사 목표가 대비 현재 가격이 아주 쌉니다.]
📊 차트 흐름: [이동평균/RSI/MACD/볼린저밴드/스토캐스틱을 종합한 현재 흐름을 쉬운 말로 1~2줄 요약]

3. 📝 실전 매매 가이드
🛒 매수가: [구체적인 매수 추천 가격대]
🛑 손절가: [지지선·펀더멘털 근거로 판단한 현실적인 손절 기준가 + 이유 짧게]
🎯 목표가: [저항선·목표주가 등 근거 기반 1차/2차 또는 중장기 목표 가격]"""

_RESPONSE_PATTERN = re.compile(
    r"🎯\s*최종 의견:\s*(?P<opinion>\S+)\s*"
    r".*?🚀\s*한 줄 요약\s*\n"
    r"(?P<comment>.+?)\s*"
    r".*?📈\s*실적 및 호재:\s*(?P<biz>.+?)\s*"
    r"💰\s*가격 매력도:\s*(?P<price>.+?)\s*"
    r"📊\s*차트 흐름:\s*(?P<chart>.+?)\s*"
    r".*?🛒\s*매수가:\s*(?P<entry>.+?)\s*"
    r"🛑\s*손절가:\s*(?P<stop>.+?)\s*"
    r"🎯\s*목표가:\s*(?P<target>.+)",
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
        f"📈 실적 및 호재: {match.group('biz').strip()}\n"
        f"💰 가격 매력도: {match.group('price').strip()}\n"
        f"📊 차트 흐름: {match.group('chart').strip()}"
    )
    strategy = (
        f"🛒 매수가: {match.group('entry').strip()}\n"
        f"🛑 손절가: {match.group('stop').strip()}\n"
        f"🎯 목표가: {match.group('target').strip()}"
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

    sample_text = """🎯 최종 의견: 매수

1. 🚀 한 줄 요약
순이익이 급증하고 강력한 주주환원 정책(자사주 소각)을 발표하여 앞으로의 주가 상승이 기대됩니다.

2. 💡 AI 분석 포인트
📈 실적 및 호재: 상반기 순이익이 작년 대비 약 193% 급증했으며, 향후 3년간 보유 주식의 22%를 없앨 계획입니다.
💰 가격 매력도: 현재가(40,650원)는 증권사 평균 목표가(약 65,000원)에 비해 크게 저평가되어 있어요.
📊 차트 흐름: 단기 하락 후 주요 지지선을 회복하며 상승 흐름을 타기 시작했습니다.

3. 📝 실전 매매 가이드
🛒 매수가: 39,500원 ~ 41,000원 부근 (나눠서 매수 추천)
🛑 손절가: 37,000원 이탈 시 (위험 관리를 위해 매도)
🎯 목표가: 단기 43,500원 / 중장기 57,000원"""
    parsed = _parse_response(sample_text)
    assert parsed is not None, "정상 형식 응답 파싱 실패"
    assert parsed["opinion"] == "매수", parsed
    assert "193%" in parsed["analysis"], parsed["analysis"]
    assert "43,500원" in parsed["strategy"], parsed["strategy"]

    assert _parse_response("형식이 전혀 다른 응답") is None, "잘못된 형식인데 파싱이 성공함"

    print("llm_opinion.py self-check 통과")
