#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28"]
# ///
"""국회 세 호스트를 치는 HTTP 세션 — 쿠키·CSRF·레이트리밋·재시도.

도메인 모듈(bills·members·meetings)이 공유하는 유일한 관심사다. 여기 모아 둔 이유는
**세 호스트의 함정이 서로 다르고, 각 도메인 파일이 그걸 따로 기억하면 하나가 틀린다**는 것.

호스트별로 다른 것:

  likms   POST에 ``_csrf``가 필요하고, 그 토큰은 진입 페이지의 **``form``이 아니라
          ``global-hidden-form``**에 있다. 진입 GET을 한 번 해야 얻는다.
  record  POST에 ``X-Requested-With: XMLHttpRequest``가 필요하다. 없으면 **200에
          결과가 0건**으로 온다 — 에러가 아니라 빈 응답이라 알아채기 어렵다.
  www     의원 상세 GET만 쓴다. 명부 JSON API는 봇 차단으로 403이라 쓰지 않는다.

**빈 결과는 성공이 아니다.** 이 도메인의 실패는 거의 전부 200에 알맹이만 빈 형태로
오므로, 호출자가 "몇 건이 와야 하는가"를 알고 대조해야 한다. 그래서 이 모듈은
상태 코드만 보고 판정하지 않고 판정을 호출자에게 넘긴다.
"""
from __future__ import annotations

import random
import re
import sys
import time

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LIKMS = "https://likms.assembly.go.kr"
RECORD = "https://record.assembly.go.kr"
WWW = "https://www.assembly.go.kr"

#: likms 의안 검색 폼이 있는 페이지. **다른 검색 페이지들은 폼이 달라 행이 오지 않는다.**
#: 메인 페이지 원시 HTML에 이 링크가 없어서(내비게이션이 스크립트로 그려진다) 브라우저로 찾았다.
LIKMS_SEARCH_PAGE = f"{LIKMS}/bill/bi/bill/sch/detailedSchPage.do"

#: 기본 속도. 04번의 실측 절차로 정한다 — 측정 전에는 보수적으로 둔다.
DEFAULT_RATE = 4.0


class Session:
    """쿠키를 유지하는 세션 하나. 호스트별 진입과 CSRF를 알아서 처리한다."""

    def __init__(self, rate: float = DEFAULT_RATE, timeout: float = 60.0):
        self._c = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                               timeout=timeout)
        self._min_interval = 1.0 / rate if rate > 0 else 0.0
        self._last = 0.0
        self._csrf: dict[str, str] = {}
        self.requests = 0

    # ── 레이트리밋 ────────────────────────────────────────────
    def _wait(self) -> None:
        if self._min_interval <= 0:
            return
        gap = time.monotonic() - self._last
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last = time.monotonic()

    # ── 저수준 ────────────────────────────────────────────────
    def request(self, method: str, url: str, *, retries: int = 3, **kw) -> httpx.Response:
        """5xx·전송 오류만 재시도한다.

        ⚠️ 4xx는 재시도하지 않는다. 403(명부 API)이나 404(사라진 의안)는 다시 쳐도 같고,
           재시도하면 차단만 굳힌다. 그것들은 호출자가 ``gone``으로 원장에 남길 일이다.
        """
        last: Exception | None = None
        for attempt in range(retries):
            self._wait()
            self.requests += 1
            try:
                r = self._c.request(method, url, **kw)
                if r.status_code < 500:
                    return r
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request,
                                             response=r)
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
            if attempt < retries - 1:
                # 지터를 섞는다 — 여러 대상이 같은 리듬으로 재시도하면 부하가 겹친다
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        raise last  # type: ignore[misc]

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def xhr(self, url: str, data: dict, *, referer: str, **kw) -> httpx.Response:
        """XHR POST. ``X-Requested-With``가 핵심이다.

        ⚠️ record 의 ``cmit.do``는 이 헤더가 없으면 **200에 회의 id가 0개**로 온다.
           계획 세션은 원인을 세션 쿠키로 지목했지만 재현해 보니 쿠키 없이도 헤더만
           있으면 온다 — 빈 결과를 만나면 쿠키가 아니라 이 헤더를 먼저 의심하라.
        """
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
        }
        headers.update(kw.pop("headers", {}))
        return self.request("POST", url, data=data, headers=headers, **kw)

    # ── likms CSRF ────────────────────────────────────────────
    def likms_csrf(self, page: str = LIKMS_SEARCH_PAGE) -> str:
        """진입 페이지에서 ``_csrf``를 얻어 캐시한다.

        ⚠️ 토큰은 ``document.forms['form']`` 안에 **없다.** ``global-hidden-form``이라는
           다른 form에 있다. ``form``의 필드만 훑으면 토큰을 못 찾는다 —
           ``billInfo.do``의 다중 form 함정과 같은 계열이다.
        """
        if page not in self._csrf:
            m = re.search(r'name="_csrf"[^>]*value="([^"]*)"', self.get(page).text)
            if not m:
                raise RuntimeError(f"_csrf를 찾지 못했다: {page}")
            self._csrf[page] = m.group(1)
        return self._csrf[page]

    def likms_form_fields(self, page: str = LIKMS_SEARCH_PAGE) -> list[str]:
        """진입 페이지 ``form``의 name 목록(63개). 하드코딩하지 않고 페이지에서 읽는다."""
        html = self.get(page).text
        body = re.search(r'<form[^>]*\bname="form"[^>]*>(.*?)</form>', html, re.S)
        if not body:
            raise RuntimeError(f"form을 찾지 못했다: {page}")
        return list(dict.fromkeys(
            re.findall(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', body.group(1))))

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def measure_rate(session_factory, fetch, targets: list, rates=(2, 4, 6, 8)) -> None:
    """04번의 속도 실측 절차. **측정 없이 기본값을 박지 마라.**

    실패율이 오르거나 응답시간 중앙값이 뚜렷이 나빠지는 지점 **바로 아래**를 고른다.
    속도는 완결성 장치가 아니다 — 실패한 요청은 원장이 받고 다음 패스가 회수하므로
    이 실험 자체는 안전하다.
    """
    import statistics
    print(f"{'rps':>4}  {'중앙값':>8}  {'p90':>8}  {'실패':>5}  {'총시간':>7}")
    for rate in rates:
        s = session_factory(rate)
        lat, fail, t0 = [], 0, time.monotonic()
        for t in targets:
            a = time.monotonic()
            try:
                fetch(s, t)
                lat.append(time.monotonic() - a)
            except Exception:
                fail += 1
        s.close()
        if lat:
            print(f"{rate:>4}  {statistics.median(lat):>7.2f}s  "
                  f"{statistics.quantiles(lat, n=10)[8] if len(lat) > 9 else max(lat):>7.2f}s  "
                  f"{fail:>5}  {time.monotonic() - t0:>6.1f}s")


if __name__ == "__main__":
    # 세 호스트가 살아 있는지 확인한다. 파서를 고치기 전에 원천부터 의심할 때 쓴다.
    with Session() as s:
        for label, url in (("likms 검색 페이지", LIKMS_SEARCH_PAGE),
                           ("record 트리", f"{RECORD}/assembly/mnts/total/22.do?class_id_sch=2&cmit_chk=all"),
                           ("www 의원 상세", f"{WWW}/members/22nd/KIMHyun")):
            try:
                r = s.get(url)
                print(f"  {label:20s} {r.status_code} {len(r.content):>9,d}B")
            except Exception as e:
                print(f"  {label:20s} ERR {type(e).__name__}: {e}")
        try:
            print(f"  {'likms _csrf':20s} {len(s.likms_csrf())}자")
            print(f"  {'likms form 필드':20s} {len(s.likms_form_fields())}개")
        except Exception as e:
            print(f"  csrf/form ERR {e}", file=sys.stderr)
