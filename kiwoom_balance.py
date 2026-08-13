"""키움증권 REST API로 예수금(현금 잔고)을 조회한다.
kiwoom_config.json(git에 안 올라감 — .gitignore 대상)에 앱키/시크릿을 넣어야 동작하고,
없으면 모든 함수가 조용히 None을 반환해 나머지 파이프라인이 죽지 않는다.

주의(왜 조회만 구현했나): 이 모듈은 조회 전용이다. 매수/매도 주문 API는 의도적으로
구현하지 않았다 — 엔드포인트/필드명을 잘못 짚으면 조회는 그냥 에러로 끝나지만 주문은
실제 계좌에 영향을 준다. 아래 엔드포인트·TR 코드(kt00001)·도메인은 공식 문서
(https://openapi.kiwoom.com/guide/apiguide)와 커뮤니티 레퍼런스로 교차 확인했지만,
계좌 조회 TR의 정확한 요청/응답 필드명까지 100% 확정하진 못했다 — 실제 앱키로 첫 호출을
해보고 값이 안 맞으면 kiwoom_config.json은 그대로 두고 이 파일의 _call_account_api
바디/파싱 부분만 공식 명세서(API 사용신청 후 발급됨)와 대조해서 고치면 된다.
"""
import json
from typing import Optional

import requests

CONFIG_FILE = "kiwoom_config.json"


def _valid(cfg: Optional[dict]) -> bool:
    return bool(cfg and cfg.get("app_key") and cfg.get("app_secret"))


def _load_config() -> Optional[dict]:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _base_url(cfg: dict) -> str:
    return "https://mockapi.kiwoom.com" if cfg.get("mock") else "https://api.kiwoom.com"


def _get_token(cfg: dict) -> str:
    resp = requests.post(
        f"{_base_url(cfg)}/oauth2/token",
        headers={"Content-Type": "application/json;charset=UTF-8"},
        json={"grant_type": "client_credentials", "appkey": cfg["app_key"], "secretkey": cfg["app_secret"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("token"):
        raise ValueError(f"토큰 발급 실패: {data}")
    return data["token"]


def _call_account_api(cfg: dict, token: str, tr_id: str, body: dict) -> dict:
    """계좌 관련 TR은 공식 가이드 기준 /api/dostk/acnt 엔드포인트를 api-id 헤더로 구분해서 부른다."""
    resp = requests.post(
        f"{_base_url(cfg)}/api/dostk/acnt",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": tr_id,
        },
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_deposit_krw(cfg: Optional[dict] = None) -> Optional[float]:
    """예수금(현금 잔고, 원) 조회. 설정이 없거나 조회 실패하면 None."""
    cfg = cfg if cfg is not None else _load_config()
    if not _valid(cfg):
        return None
    try:
        token = _get_token(cfg)
        # kt00001(예수금상세현황요청) — qry_tp "3"은 추정현금자산 포함 조회.
        data = _call_account_api(cfg, token, "kt00001", {"qry_tp": "3"})
        raw = data.get("entr") or data.get("prsm_dpst_aset_amt")
        return float(raw) if raw is not None else None
    except Exception as e:
        print(f"키움 예수금 조회 실패: {e}")
        return None


if __name__ == "__main__":
    assert _base_url({"mock": True}) == "https://mockapi.kiwoom.com"
    assert _base_url({"mock": False}) == "https://api.kiwoom.com"
    assert _base_url({}) == "https://api.kiwoom.com", "mock 플래그 없으면 실전투자 서버가 기본이어야 함"

    assert _valid(None) is False
    assert _valid({}) is False
    assert _valid({"app_key": "a"}) is False, "시크릿 없이는 유효하지 않아야 함"
    assert _valid({"app_key": "a", "app_secret": "b"}) is True

    assert get_deposit_krw({"app_key": "", "app_secret": ""}) is None, "앱키 없이는 네트워크 호출 없이 None"
    assert get_deposit_krw({}) is None

    print("kiwoom_balance.py self-check 통과")
