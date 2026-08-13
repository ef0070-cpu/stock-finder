"""discover.py의 순수 로직(네트워크 없는 부분)만 검증한다."""
from discover import classify_tags, filter_common_stocks, interleave, select_candidates


def test_filter_common_stocks():
    codes = ["005930", "005935", "000660", "402340", "005945"]
    assert filter_common_stocks(codes) == ["005930", "000660", "402340"]


def test_select_candidates():
    analyzed = [
        {"market": "kr", "ticker": "A", "opinion": "매수", "score": 2},
        {"market": "kr", "ticker": "B", "opinion": "보유", "score": 0},
        {"market": "kr", "ticker": "C", "opinion": "매수", "score": 3},
        {"market": "us", "ticker": "D", "opinion": "매도", "score": -3},
        {"market": "us", "ticker": "E", "opinion": "매수", "score": 1},
    ]
    # 시장별로 매수 의견 중 점수가 가장 높은 종목 1개씩만 선택
    result = select_candidates(analyzed)
    assert [c["ticker"] for c in result] == ["C", "E"]

    # 해당 시장에 매수 의견이 없으면 그 시장은 결과에서 빠진다
    no_us_buy = [
        {"market": "kr", "ticker": "A", "opinion": "매수", "score": 2},
        {"market": "us", "ticker": "D", "opinion": "매도", "score": -3},
    ]
    assert [c["ticker"] for c in select_candidates(no_us_buy)] == ["A"]


def test_interleave():
    assert interleave(["k1", "k2", "k3"], ["u1"]) == ["k1", "u1", "k2", "k3"]
    assert interleave(["k1"], ["u1", "u2", "u3"]) == ["k1", "u1", "u2", "u3"]
    assert interleave([], ["u1", "u2"]) == ["u1", "u2"]
    assert interleave(["k1", "k2"], []) == ["k1", "k2"]


def test_classify_tags():
    assert classify_tags(per=10, dividend_yield=4.0) == ["고배당주"]
    assert classify_tags(per=30, dividend_yield=1.0) == ["성장주"]
    assert classify_tags(per=30, dividend_yield=4.0) == ["고배당주", "성장주"]
    assert classify_tags(per=10, dividend_yield=1.0) == []
    assert classify_tags(per=None, dividend_yield=None) == []


if __name__ == "__main__":
    test_filter_common_stocks()
    test_select_candidates()
    test_interleave()
    test_classify_tags()
    print("test_discover.py 통과")
