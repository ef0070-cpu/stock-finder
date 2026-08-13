"""run_pipeline.bat이 띄우는 로컬 서버. index.html의 '업데이트' 버튼이 이 서버의
/run 엔드포인트를 호출하면 파이프라인을 다시 실행해 data.js를 갱신한다.
이 창을 열어둔 동안만 업데이트 버튼이 동작한다."""
import http.server
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import kiwoom_balance
import run_pipeline

discover_process = None

PORT = 8765


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        try:
            with open(filename, "rb") as f:
                body = f.read()
            status = 200
        except FileNotFoundError:
            body = b"{}" if filename.endswith(".json") else b""
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/discover_progress.json":
            self._serve_file(
                "discover_progress.json", "application/json; charset=utf-8"
            )
            return

        if path in ("/discover_progress", "/discover_progress.html"):
            self._serve_file("discover_progress.html", "text/html; charset=utf-8")
            return

        if path == "/discover":
            # index.html의 "종목 발굴 시작" 버튼이 호출한다. discover.py(1~4단계)를
            # 백그라운드 서브프로세스로 띄우고 즉시 응답한다 — 진행상황은
            # discover_progress.json/.html이 별도로 폴링해 보여준다.
            global discover_process
            if discover_process is not None and discover_process.poll() is None:
                self._json(200, {"status": "already_running"})
                return
            kr_count = query.get("kr", ["1"])[0]
            us_count = query.get("us", ["1"])[0]
            env = dict(os.environ, DISCOVER_KR_COUNT=kr_count, DISCOVER_US_COUNT=us_count)
            discover_process = subprocess.Popen([sys.executable, "discover.py"], env=env)
            self._json(200, {"status": "started"})
            return

        if path == "/report":
            date = query.get("date", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                date = datetime.now().strftime("%Y-%m-%d")
            self._serve_file(
                f"reports/{date}-발굴보고서.html", "text/html; charset=utf-8"
            )
            return

        if path == "/quote":
            # 브라우저가 Yahoo Finance를 직접 fetch하면 CORS로 막힌다(어떤 origin이든
            # 마찬가지). 이 서버는 로컬 실행이라 CORS 제약이 없으므로 대신 조회해준다 —
            # tickers.json에 아직 없는 해외 신규 티커를 보유종목에 추가했을 때 사용.
            ticker = query.get("ticker", [""])[0]
            try:
                self._json(200, {"status": "ok", "result": run_pipeline.analyze_ticker("us", ticker)})
            except Exception as e:
                self._json(500, {"status": "error", "message": str(e)})
            return

        if path == "/kiwoom/deposit":
            # 보유종목 탭 "키움 잔고 불러오기" 버튼이 호출한다. kiwoom_config.json이
            # 없거나 앱키가 비어 있으면 deposit이 null로 돌아오고, 화면에서 그 경우를
            # "설정 필요"로 안내한다.
            deposit = kiwoom_balance.get_deposit_krw()
            self._json(200, {"status": "ok", "deposit": deposit})
            return

        if path == "/fx":
            # 해외 종목 가격을 원화로 환산해 표시할 때 쓰는 참고 환율.
            try:
                self._json(200, {"status": "ok", "rate": run_pipeline.get_usd_krw_rate()})
            except Exception as e:
                self._json(500, {"status": "error", "message": str(e)})
            return

        if path != "/run":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        try:
            run_pipeline.main()
            self._json(200, {"status": "ok"})
        except SystemExit:
            self._json(500, {"status": "error", "message": "tickers.json에 등록된 티커가 없음"})
        except Exception as e:
            self._json(500, {"status": "error", "message": str(e)})


def main():
    print("초기 데이터 갱신 중...")
    try:
        run_pipeline.main()
    except SystemExit:
        pass

    print(f"\n서버 시작 (포트 {PORT}) - index.html의 '업데이트' 버튼이 이 서버를 호출합니다.")
    print("이 창을 닫으면 서버가 멈추고 업데이트 버튼도 동작하지 않습니다.")
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
