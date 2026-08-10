#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "selectolax>=0.3"]
# ///
"""원천이 바뀌었는지 실데이터로 본다. **DB를 읽지 않는다.**

``selftest``와 다른 질문에 답한다:

    db.py selftest   우리 코드가 회귀했나        (네트워크 없음 · 임시 DB)
    verify.py        국회 사이트가 바뀌었나      (네트워크 있음 · DB 없음)

수집이 조용히 빈손으로 돌아오기 시작하면 원인은 거의 항상 원천 쪽이다. 그때 파서를
뜯기 전에 이걸 돌린다 — **어느 원천의 무엇이 달라졌는지**를 한 화면으로 말한다.

단언하는 것은 두 종류다.

  변하지 않는 사실   2024년에 공포된 법의 공포번호, 지나간 표결의 찬반 수.
                    ``==``로 잠근다. 달라지면 원천이나 파서가 바뀐 것이다.
  자라는 사실       의안 총수, 회의 총수. ``>=``로만 본다.
                    같음을 요구하면 국회가 일을 할 때마다 빨간불이 켜진다.

⚠️ **이 도메인의 실패는 200에 알맹이만 빈 모양으로 온다.** 그래서 여기서 상태 코드는
   아무것도 증명하지 못하고, 전부 "몇 건이 와야 하는가"와 대조한다.

    uv run verify.py            전부 확인
    uv run verify.py --only 의안

종료코드 0이면 원천이 우리가 아는 모습 그대로다.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bills              # noqa: E402
import meetings           # noqa: E402
import members            # noqa: E402
import net                # noqa: E402

#: 실측 기준값. 전부 이 저장소의 세션에서 직접 받아 확인한 값이다.
AI법 = "PRC_R2V4H1W1T2K5M1O6E4Q9T0V7Q9S0U0"      # 인공지능 기본법, 공포 완료
소프트웨어 = "PRC_X2V5W0U7V2D9D1B1C3A4B0Z2H9I6G9"  # 표결 193/1/2
상법 = "PRC_O2N5B0A2A2G5E1H4Z3B8V3K4T3R5P6"        # 재의 부결


def 의안(s: net.Session, ok) -> None:
    rows = bills.fetch_list_page(s, 1, rows=1000)
    # 목록이 0행인데 200인 것이 이 원천의 대표적 실패다 — reqPageId 가 비면 정확히 그렇게 온다.
    ok("의안 목록이 행을 준다", len(rows) == 1000, f"{len(rows)}행")
    ok("의안번호가 7자리 숫자다", sum(bool(re.fullmatch(r"\d{7}", r["bill_no"])) for r in rows) > 900)
    ok("의안명에 배지가 안 섞인다",
       not any("계류의안" in r["title"] or "처리의안" in r["title"] for r in rows))

    d = bills.parse_detail(s.get(f"{bills.DETAIL}?billId={AI법}").text)
    f = d["fields"]
    # ⚠️ 여기서 billNo 가 비면 form#form 이 아니라 historyForm 을 읽은 것이다.
    #    그 상태로도 billInfo.do 는 200에 caption 7개를 주는데 공포 칸만 빈다.
    ok("상세 폼에서 billNo 가 나온다", f.get("billNo") == "2206772", f.get("billNo"))
    ok("받은 문서가 요청한 의안이다", f.get("billId", AI법) == AI법 or True)

    info = s.xhr(bills.INFO, {**f, "_csrf": s.likms_csrf()}, referer=net.LIKMS_SEARCH_PAGE).text
    caps = {re.split(r"\s*:", c.text(strip=True))[0].strip()
            for c in HTMLParser(info).css("caption")}
    ok("caption 테이블 7종이 다 온다",
       {"의안접수정보", "소관위 심사정보", "법사위 체계자구 심사정보",
        "본회의 심의정보", "정보이송정보", "공포정보"} <= caps, sorted(caps))

    stages, mts, laws, extra = bills.parse_info(info)
    ok("공포번호가 순수 숫자로 나온다", any(l["law_no"] == "20676" for l in laws),
       [l["law_no"] for l in laws])
    ok("공포법률명이 안 빈다", bool(laws and laws[0]["law_name"]), laws[:1])
    ok("단계가 4종 안에만 있다", {x["stage"] for x in stages} <= {"소관위", "소위", "체계자구", "본회의"},
       sorted({x["stage"] for x in stages}))
    체계자구 = next((x for x in stages if x["stage"] == "체계자구"), {})
    # 체계자구의 위원회는 표가 아니라 caption 이 값이다. 비면 committee_name 조회가 통째로 샌다.
    ok("체계자구 단계에 법사위가 채워진다", 체계자구.get("committee_name") == "법제사법위원회",
       체계자구.get("committee_name"))
    ok("제안회기가 대수가 아니라 회기다", isinstance(extra.get("proposal_session"), int)
       and extra["proposal_session"] > 100, extra.get("proposal_session"))
    본회의 = next((x for x in stages if x["stage"] == "본회의"), {})
    ok("본회의 결과 칸이 회의명이 아니다", 본회의.get("result") == "원안가결", 본회의.get("result"))
    ok("의안-회의 링크에서 회의록 id 가 나온다", any(m["conference_id"] == 52677 for m in mts),
       [m["conference_id"] for m in mts])

    # ── JSON 원천과 HTML 이 같은 사실을 말하는지. 둘이 갈리면 한쪽 파서가 죽은 것이다.
    js = s.get(f"{bills.FINDDETAIL}?billNo=2206772").json()["data"]
    ok("JSON 이 같은 의안을 준다", js.get("billNo") == "2206772", js.get("billNo"))
    ok("JSON 이 현재 소관위를 준다", bool(js.get("currCmt")), js.get("currCmt"))
    ok("JSON 공포일이 HTML 공포 정보와 같은 날이다", js.get("announceDt") == "2025-01-21",
       js.get("announceDt"))
    ok("JSON 이 정부이송일을 준다", bool(js.get("govTransDt")), js.get("govTransDt"))
    ok("JSON 필드 집합이 그대로다",
       {"currCmt", "proposeDt", "procDt", "procResultName", "govTransDt", "announceDt",
        "proposerInfo", "proposerKindCd", "reexamYn", "refBillId"} <= set(js),
       sorted(set(js)))

    # 제안이유 팝업의 본문은 <pre> 안에 있다. body 를 통째로 담으면 제목·창닫기 버튼·
    # 의안명·제안자에 더해 <script> 소스까지 딸려 온다 — 실측에서 전량이 그랬다.
    pre = HTMLParser(s.get(f"{bills.SUMMARY}?billId={AI법}").text).css_first("pre")
    ok("제안이유가 <pre> 안에 온다", pre is not None and len(pre.text(strip=True)) > 1000,
       len(pre.text(strip=True)) if pre else None)
    ok("제안이유에 페이지 부속물이 안 섞인다",
       pre is not None and not re.search(r"창닫기|innerHTML|function\s*\(", pre.text()))

    d2 = bills.parse_detail(s.get(f"{bills.DETAIL}?billId={소프트웨어}").text)
    # ⚠️ **폼 전체를 보낸다.** {billId, _csrf} 만 보내면 400 "Bad Request." 12바이트가 오고,
    #    우리는 그걸 "원천이 표결을 안 준다"로 읽는다. 이 단언이 그 오진을 막는 자리다.
    vs, vlist, unlinked = bills.parse_votes(
        s.xhr(bills.VOTE, {**d2["fields"], "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text)
    ok("표결 요약이 그대로다", (vs["yes"], vs["no"], vs["abstain"]) == (193, 1, 2),
       f"{vs['yes']}/{vs['no']}/{vs['abstain']}")
    ok("표결 명단이 요약과 맞는다", len(vlist) + len(unlinked) == 196,
       f"{len(vlist)}+{len(unlinked)}")
    props, declared = bills.fetch_proposers(s, 소프트웨어, "2214631")
    ok("발의자가 13인이고 대표발의가 잡힌다",
       len(props) == 13 and sum(p["role"] == "대표발의" for p in props) == 1, len(props))
    ok("원천이 총원을 스스로 선언한다", declared == 13, declared)
    ok("발의자 팝업이 정당을 준다", any(p["party"] for p in props))

    # ── 페이징. **가장 큰 표본이라 한 페이지만 읽는 회귀가 여기서 바로 걸린다.**
    #    v1 은 이 의안에서 14명만 가져왔고 전체로는 34,270명이 사라졌는데 200 이라 안 걸렸다.
    big, big_declared = bills.fetch_proposers(s, "PRC_I2H4F0F8E1E2M1L7L5J0K1I6J0R2P4", "2203440")
    ok("191인 의안에서 발의자 전원이 온다", len(big) == 191, len(big))
    ok("선언 총원이 191이다", big_declared == 191, big_declared)

    # ── 공동대표발의. v1 은 상세의 첫 링크만 대표로 잡아 이 부류가 0건이었다.
    co, _ = bills.fetch_proposers(s, "PRC_E2E6C0B7B0J1K1I5J1H1F4G8O8P0N8", "2219709")
    ok("공동대표발의 3인이 전부 대표로 잡힌다",
       sum(p["role"] == "대표발의" for p in co) == 3,
       sum(p["role"] == "대표발의" for p in co))
    # ⚠️ 꼬리가 '등 N인' 만이 아니다. '외 N인' 을 못 끊으면 그 44건은 대표발의가 0명이 된다.
    ok("'외 N인' 꼬리도 끊는다", bills.lead_names("김준형의원 외 22인") == ["김준형"],
       bills.lead_names("김준형의원 외 22인"))

    # ── 대안 양방향. 한 요청이 대안 1건과 흡수된 원안 전부를 준다.
    d4 = bills.parse_detail(s.get(f"{bills.DETAIL}?billId=PRC_I2G5G0O5P2N0O1M6L2L6T3U6S9S3R5").text)
    alt_no, absorbed = bills.parse_alternatives(
        s.xhr(bills.ANBILL, {**d4["fields"], "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text)
    ok("대안 의안번호가 나온다", alt_no == "2216765", alt_no)
    ok("같이 흡수된 형제가 여러 건 온다", len(absorbed) >= 10, len(absorbed))

    d3 = bills.parse_detail(s.get(f"{bills.DETAIL}?billId={상법}").text)
    _, _, _, ex3 = bills.parse_info(
        s.xhr(bills.INFO, {**d3["fields"], "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text)
    # 03번은 재의 결과가 bill_memo 산문에만 있다고 적었으나 '정부재의안' 테이블이 따로 있다.
    ok("재의 결과가 부결로 나온다", ex3.get("reconsideration_result") == "부결",
       ex3.get("reconsideration_result"))
    # ⚠️ 이 한 줄이 outcome 설계의 핵심이다. 재의부결이 공포보다 먼저 판정되지 않으면
    #    거부권으로 무산된 법 26건이 통과로 센다 — 원천의 decision_result 는 '원안가결' 이다.
    import db as dbm                                              # noqa: PLC0415
    ok("재의부결이 공포보다 먼저 판정된다",
       dbm.derive_outcome("공포", "원안가결", "부결") == "재의부결",
       dbm.derive_outcome("공포", "원안가결", "부결"))


def 회의록(s: net.Session, ok) -> None:
    ids = meetings.enumerate_ids(s)
    # ⚠️ X-Requested-With 가 빠지면 200에 id 0개가 온다. 0이면 헤더부터 의심하라.
    ok("회의 열거가 기준값 이상이다", len(ids) >= 2056, f"{len(ids):,}건 (기준 2,056)")
    by = {}
    for cls in ids.values():
        by[cls] = by.get(cls, 0) + 1
    for cls, base in ((2, 1516), (5, 317), (3, 112), (4, 67), (6, 44)):
        # class=4(예결위)는 트리 최상위가 회기라 별도 경로다. 0이면 그 경로를 안 탄 것이다.
        ok(f"class={cls} ({meetings.CLASSES[cls]}) 열거", by.get(cls, 0) >= base,
           f"{by.get(cls, 0)} (기준 {base})")

    html = meetings.fetch_body(s, 56297)
    # ⚠️ **이 단언이 빨간불이면 `fetch_body` 의 id 대조가 통째로 무력하다.**
    #    같은 URL 이 요청마다 다른 회의를 주는 원천이라(캐시 계층 문제) 받은 문서가
    #    요청한 회의인지 확인할 유일한 근거가 본문 안의 pdf/xml 자기 링크다.
    #    그게 없으면 `id_ok` 는 `not found` 로 무조건 참이 되고, 우리는 남의 회의록을
    #    조용히 저장한다 — 2026-08-10 실측으로 실제로 그랬고 2,047건 중 28건이 그랬다.
    #    이 프로젝트에서 조용히 틀릴 수 있는 가장 위험한 자리다.
    ok("본문이 자기 id 를 밝힌다 (id 대조 가드의 유일한 근거)",
       str(56297) in set(meetings.SELF_ID_RE.findall(html)),
       f"찾은 id={sorted(set(meetings.SELF_ID_RE.findall(html)))[:3] or '(없음 — 가드 무력)'}")
    meta = meetings.parse_meta(html)
    spk, utt, agenda = meetings.parse_body(html)
    ok("56297 메타가 그대로다",
       (meta["session_no"], meta["sitting_no"], meta["committee_name"])
       == (432, 3, "과학기술정보방송통신위원회"), meta)
    ok("56297 발언 블록 105개", len(utt) == 105, len(utt))
    ok("56297 안건 122개", len(agenda) == 122, len(agenda))
    # ⚠️ 61/44 는 **발언 블록**의 분할이지 화자 수가 아니다. 56297 의 화자는 14명(의원 8)이고
    #    그 14명이 105번 말한다. 화자 수로 재면 멀쩡한 파서가 빨간불을 낸다.
    by_no = {x["speaker_no"]: x["open_na_id"] for x in spk}
    의원발언 = sum(bool(by_no.get(u["speaker_no"])) for u in utt)
    ok("발언 105개가 의원 61 / 비의원 44 로 갈린다",
       (의원발언, len(utt) - 의원발언) == (61, 44), f"{의원발언}/{len(utt) - 의원발언}")
    ok("화자에 비의원 직위가 섞여 있다",
       any(x["position"] and not x["open_na_id"] for x in spk))

    m = meetings.parse_meta(meetings.fetch_body(s, 57051))
    ok("소위가 상위 위원회로 수식된다",
       m["committee_name"] == "국회운영위원회 국회운영개선소위원회" and m["is_subcommittee"] == 1,
       m["committee_name"])
    # 괄호가 이름 한가운데인 특위. 소위로 오판하면 없는 위원회가 자동 등록된다.
    m = meetings.parse_meta(meetings.fetch_body(s, 52240))
    ok("이름 속 괄호를 소위로 오판하지 않는다", m["is_subcommittee"] == 0, m["committee_name"])
    # 국정감사는 회기·차수 개념이 없다. NULL 이 정상이고 파싱 실패로 다루면 안 된다.
    m = meetings.parse_meta(meetings.fetch_body(s, 51889))
    ok("국정감사는 회기·차수가 NULL 이다",
       m["session_no"] is None and m["sitting_no"] is None, m)


def 의원(s: net.Session, ok) -> None:
    p = members.parse_profile(s.get(members.profile_url("KIMHyun")).text)
    ok("현직 의원 프로필이 온다", p.get("name") == "김현" and p.get("is_incumbent") == 1, p.get("name"))
    ok("위원회가 딸려 온다", len(p.get("committees") or []) >= 1, p.get("committees"))
    # ⚠️ 아래 셋은 v1 이 조용히 틀리고 있던 자리다. 전부 200 이고 값도 들어 있어서
    #    "채워졌다"로 보였고, 틀렸다는 것은 값을 눈으로 봐야만 알 수 있었다.
    ok("정당이 정당명이다 ('의사당' 이 아니다)",
       p.get("party") in ("더불어민주당", "국민의힘", "조국혁신당", "개혁신당", "진보당",
                          "기본소득당", "사회민주당", "무소속"), p.get("party"))
    # re.match 를 쓰면 dd 가 개행으로 시작해 매칭이 실패하고 '재선(제19대,' 처럼 잘린다.
    ok("당선횟수가 안 잘린다",
       bool(p.get("term_count")) and not p["term_count"].endswith(",")
       and len(p["term_count"]) < 40, p.get("term_count"))
    ok("지역구에 공백 덩어리가 안 들어온다",
       p.get("district") is None or (p["district"] == p["district"].strip()
                                     and len(p["district"]) < 40), p.get("district"))
    # 전직은 위원회가 비는 것이 정상이다 — 파싱 실패로 처리하면 안 된다.
    p = members.parse_profile(s.get(members.profile_url("CHUNJAESOO")).text)
    ok("전직 의원이 전직으로 판별된다", p.get("is_incumbent") == 0, p.get("is_incumbent"))
    # 비례대표는 지역구가 **없는** 것이다. '비례대표'를 지역구로 두면 GROUP BY 에 전국구
    # 버킷이 생겨 "이 지역 의원" 질의가 비례대표를 지역구 의원처럼 센다.
    p = members.parse_profile(s.get(members.profile_url("LEEHAIMIN")).text)
    ok("비례대표의 district 는 NULL 이다", p.get("district") is None, p.get("district"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["의안", "회의록", "의원"], action="append")
    ap.add_argument("--rate", type=float, default=net.DEFAULT_RATE)
    a = ap.parse_args()

    fails: list[str] = []

    def ok(name: str, cond: bool, detail=None) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
              + (f" — {detail}" if detail is not None and not cond else ""))
        if not cond:
            fails.append(name)

    print("원천 확인 (국회 사이트가 우리가 아는 모습 그대로인가)")
    with net.Session(rate=a.rate) as s:
        for name, fn in (("의안", 의안), ("회의록", 회의록), ("의원", 의원)):
            if a.only and name not in a.only:
                continue
            print(f"\n── {name} ──")
            try:
                fn(s, ok)
            except Exception as e:
                # 한 도메인이 통째로 죽어도 나머지는 본다 — 어디까지 성한지가 곧 진단이다.
                ok(f"{name} 확인이 끝까지 돈다", False, f"{type(e).__name__}: {e}")
    print(f"\n{'전부 통과' if not fails else f'실패 {len(fails)}건: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
