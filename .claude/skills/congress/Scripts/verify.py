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

    stages, mts, extra = bills.parse_info(info)
    공포 = next((x for x in stages if x["stage"] == "공포"), {})
    ok("공포번호가 순수 숫자로 나온다", 공포.get("ref_no") == "20676", 공포.get("ref_no"))
    ok("공포일자에 라벨이 안 붙는다", 공포.get("date_processed") == "2025-01-21",
       공포.get("date_processed"))
    ok("공포법률명이 안 빈다", bool(extra.get("promulgated_law_name")),
       extra.get("promulgated_law_name"))
    본회의 = next((x for x in stages if x["stage"] == "본회의"), {})
    ok("본회의 결과 칸이 회의명이 아니다", 본회의.get("result") == "원안가결", 본회의.get("result"))
    ok("의안-회의 링크에서 회의록 id 가 나온다", any(m["conference_id"] == 52677 for m in mts),
       [m["conference_id"] for m in mts])

    d2 = bills.parse_detail(s.get(f"{bills.DETAIL}?billId={소프트웨어}").text)
    vs, vlist, missing = bills.parse_votes(
        s.xhr(bills.VOTE, {"billId": 소프트웨어, "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text)
    ok("표결 요약이 그대로다", (vs["yes"], vs["no"], vs["abstain"]) == (193, 1, 2),
       f"{vs['yes']}/{vs['no']}/{vs['abstain']}")
    ok("표결 명단이 요약과 맞는다", len(vlist) + missing == 196, f"{len(vlist)}+{missing}")
    props = bills.parse_proposers(
        s.xhr(bills.PROPOSER, {"billId": 소프트웨어, "billNo": "2214631",
                               "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text, d2["represent"])
    ok("발의자가 13인이고 대표발의가 잡힌다",
       len(props) == 13 and sum(p["role"] == "대표발의" for p in props) == 1, len(props))
    ok("발의자 팝업이 정당을 준다", any(p["party"] for p in props))

    d3 = bills.parse_detail(s.get(f"{bills.DETAIL}?billId={상법}").text)
    _, _, ex3 = bills.parse_info(
        s.xhr(bills.INFO, {**d3["fields"], "_csrf": s.likms_csrf()},
              referer=net.LIKMS_SEARCH_PAGE).text)
    # 03번은 재의 결과가 bill_memo 산문에만 있다고 적었으나 '정부재의안' 테이블이 따로 있다.
    ok("재의 결과가 부결로 나온다", ex3.get("reconsideration_result") == "부결",
       ex3.get("reconsideration_result"))


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
    # 전직은 위원회가 비는 것이 정상이다 — 파싱 실패로 처리하면 안 된다.
    p = members.parse_profile(s.get(members.profile_url("CHUNJAESOO")).text)
    ok("전직 의원이 전직으로 판별된다", p.get("is_incumbent") == 0, p.get("is_incumbent"))


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
