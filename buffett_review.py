"""워런 버핏 스타일 가치투자 체크리스트 심층 리뷰(3~4단계) — 종목 발굴 파이프라인의
2단계를 통과한 소수 종목(시장별 최고 후보 1개씩)에 대해서만 호출한다.
ANTHROPIC_API_KEY가 없거나 API 호출/파싱이 실패하면 None을 반환해 해당 종목은
buffett_review 없이 후보 목록에만 남도록 한다(discover.py가 처리)."""
import os
import re
from typing import Optional

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """너는 워런 버핏 스타일의 가치투자 심사역이야. 종목 하나를 깊이 있게 검토해서
①좋은 기업인가(경쟁우위·해자, 꾸준한 수익, 재무 건전성, 주주 친화적 경영, 이해하기 쉬운 사업)
②지금 가격이 적당한가(밸류에이션)를 판정하고, 초보 투자자도 이해할 수 있는 실행 조언까지 제시해.

뉴스와 재무 데이터는 웹검색으로 직접 조사해서 근거로 삼아.

[출력 형식 (반드시 아래 양식을 그대로 지켜서, 다른 말 없이 답변할 것)]
3단계 판정: [PASS 또는 WAIT 또는 REJECT]
워런점수: 재무 [0~10 숫자], 기술 [0~10 숫자], 타이밍 [0~10 숫자], 종합 [0~10 숫자]
체크리스트:
- 해자: [✅ 또는 ⚠️ 또는 ❌] [한 문장 설명]
- 꾸준한수익: [✅ 또는 ⚠️ 또는 ❌] [한 문장 설명]
- 재무건전성: [✅ 또는 ⚠️ 또는 ❌] [한 문장 설명]
- 주주친화: [✅ 또는 ⚠️ 또는 ❌] [한 문장 설명]
- 이해용이성: [✅ 또는 ⚠️ 또는 ❌] [한 문장 설명]
밸류에이션: [PER/PBR 등 근거를 포함한 설명]
4단계점수: PER [0~10 숫자]/10, PBR [0~10 숫자]/10, ROE [0~10 숫자]/10, 재무안정성 [0~10 숫자]/10
매수이유:
1. [이유]
2. [이유]
3. [이유]
리스크이유:
1. [이유]
2. [이유]
3. [이유]
초보자조언: [비중, 추가매수 신호, 손절 조건을 담은 실행 조언]
한줄총평: [전체 결론 한 문장]"""

_CHECKLIST_LABELS = {
    "해자": "moat",
    "꾸준한수익": "consistent_earnings",
    "재무건전성": "financial_health",
    "주주친화": "shareholder_friendly",
    "이해용이성": "understandable",
}
VALID_VERDICTS = {"PASS", "WAIT", "REJECT"}


def _build_user_message(name: str, ticker: str, market: str, price: float, per, dividend_yield: Optional[float]) -> str:
    market_label = "국내" if market == "kr" else "해외"
    per_text = f"{per}배" if per is not None else "조회 안됨"
    div_text = f"{dividend_yield}%" if dividend_yield is not None else "조회 안됨"
    return f"""[종목 정보]
- 종목명/티커: {name} ({ticker})
- 시장: {market_label}
- 현재가: {price}
- PER: {per_text}
- 배당수익률: {div_text}

웹검색으로 최근 실적·재무·뉴스를 직접 조사한 뒤, 지정된 출력 형식에 맞춰 검토 결과를 내줘."""


def _extract(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None


def _extract_numbered_list(text: str, label: str) -> list:
    m = re.search(rf"{label}:\s*((?:\d+\..+\n?)+)", text)
    if not m:
        return []
    return [line.split(".", 1)[1].strip() for line in m.group(1).strip().splitlines() if "." in line]


def _extract_checklist(text: str) -> Optional[dict]:
    m = re.search(r"체크리스트:\s*\n((?:- .+\n?)+)", text)
    if not m:
        return None
    checklist = {}
    for line in m.group(1).strip().splitlines():
        line = line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        label, desc = line.split(":", 1)
        key = _CHECKLIST_LABELS.get(label.strip())
        if key:
            checklist[key] = desc.strip()
    return checklist if len(checklist) == len(_CHECKLIST_LABELS) else None


def _parse_response(text: str, rsi: float) -> Optional[dict]:
    verdict = _extract(r"3단계 판정:\s*(\S+)", text)
    if verdict not in VALID_VERDICTS:
        return None

    warren_m = re.search(
        r"워런점수:\s*재무\s*([\d.]+),?\s*기술\s*([\d.]+),?\s*타이밍\s*([\d.]+),?\s*종합\s*([\d.]+)", text
    )
    if not warren_m:
        return None

    checklist = _extract_checklist(text)
    valuation = _extract(r"밸류에이션:\s*(.+)", text)
    score_m = re.search(
        r"4단계점수:\s*PER\s*([\d.]+)/10,?\s*PBR\s*([\d.]+)/10,?\s*ROE\s*([\d.]+)/10,?\s*재무안정성\s*([\d.]+)/10", text
    )
    buy_reasons = _extract_numbered_list(text, "매수이유")
    risk_reasons = _extract_numbered_list(text, "리스크이유")
    beginner_advice = _extract(r"초보자조언:\s*(.+)", text)
    summary = _extract(r"한줄총평:\s*(.+)", text)

    if not all([checklist, valuation, score_m, buy_reasons, risk_reasons, beginner_advice, summary]):
        return None

    return {
        "warren_score": {
            "financial": float(warren_m.group(1)),
            "technical": float(warren_m.group(2)),
            "timing": float(warren_m.group(3)),
            "total": float(warren_m.group(4)),
            "verdict": verdict,
            "rsi": rsi,
        },
        "buffett_review": {
            "stage3_verdict": verdict,
            "stage3_conclusion": summary,
            "checklist": checklist,
            "valuation": valuation,
            "stage4": {
                "score": {
                    "per": float(score_m.group(1)),
                    "pbr": float(score_m.group(2)),
                    "roe": float(score_m.group(3)),
                    "financial_health": float(score_m.group(4)),
                },
                "buy_reasons": buy_reasons,
                "risk_reasons": risk_reasons,
                "beginner_advice": beginner_advice,
                "summary": summary,
            },
        },
    }


def generate_review(name: str, ticker: str, market: str, price: float, rsi: float, per, dividend_yield: Optional[float]) -> Optional[dict]:
    """성공 시 {"warren_score": {...}, "buffett_review": {...}} 반환, 실패 시 None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"  [{ticker}] ANTHROPIC_API_KEY 없음 - 버핏 리뷰 생략")
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=3072,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": _build_user_message(name, ticker, market, price, per, dividend_yield)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        parsed = _parse_response(text, rsi)
        if parsed is None:
            raise ValueError(f"응답 형식 파싱 실패: {text[:300]!r}")
        return parsed
    except Exception as e:
        print(f"  [{ticker}] 버핏 리뷰 생성 실패({e}) - 생략")
        return None


if __name__ == "__main__":
    sample = """3단계 판정: PASS
워런점수: 재무 6.2, 기술 8.8, 타이밍 7.7, 종합 7.6
체크리스트:
- 해자: ✅ 검색 90%+ 점유율
- 꾸준한수익: ✅ 매출 5년 연속 성장
- 재무건전성: ✅ 순이익 대비 부채 미미
- 주주친화: ✅ 배당 개시 + 자사주 매입
- 이해용이성: ✅ 광고·클라우드 구독 수익
밸류에이션: PER 17.6~19배로 5년 평균 대비 할인
4단계점수: PER 8/10, PBR 5/10, ROE 9/10, 재무안정성 8/10
매수이유:
1. PER 할인
2. ROE 우수
3. 현금 여력
리스크이유:
1. PBR 급등
2. 반독점 이슈
3. AI 경쟁
초보자조언: 비중 5~10%, 분할매수 권장
한줄총평: 사업은 견고하나 분할매수가 적절하다"""
    parsed = _parse_response(sample, rsi=54.2)
    assert parsed is not None, "정상 형식 응답 파싱 실패"
    assert parsed["warren_score"]["verdict"] == "PASS"
    assert parsed["warren_score"]["total"] == 7.6
    assert len(parsed["buffett_review"]["checklist"]) == 5
    assert len(parsed["buffett_review"]["stage4"]["buy_reasons"]) == 3
    assert parsed["buffett_review"]["stage4"]["score"]["per"] == 8.0

    assert _parse_response("형식이 전혀 다른 응답", rsi=50) is None, "잘못된 형식인데 파싱이 성공함"

    print("buffett_review.py self-check 통과")
