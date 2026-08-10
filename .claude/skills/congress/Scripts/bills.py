#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "selectolax>=0.3"]
# ///
"""의안 — 목록(전체 의안)과 상세(법률안만).

목록은 싸다(21요청·1분). **비싼 것은 상세이고 그건 법률안만 받는다.**
그런데도 목록은 전체 의안을 받는다 — 의안번호가 종류 구분 없이 연속이라
전체를 담아야 "번호에 구멍이 없다 = 누락이 없다"가 성립하기 때문이다.
법률안만 담으면 예산안·결의안 자리에 구멍이 생기고, 그 구멍이 비법률안인지
수집 실패인지 영원히 구분할 수 없다.

    uv run bills.py --db /tmp/t.db --list-only
    uv run bills.py --db /tmp/t.db --bill-id PRC_R2V4H1W1T2K5M1O6E4Q9T0V7Q9S0U0
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as dbm          # noqa: E402
import net                # noqa: E402

LIST = f"{net.LIKMS}/bill/bi/bill/sch/findSchPaging.do"
DETAIL = f"{net.LIKMS}/bill/bi/billDetailPage.do"
INFO = f"{net.LIKMS}/bill/bi/bill/detail/billInfo.do"
VOTE = f"{net.LIKMS}/bill/bi/bill/detail/voteInfo.do"
PROPOSER = f"{net.LIKMS}/bill/bi/popup/billProposer.do"
SUMMARY = f"{net.LIKMS}/bill/bi/popup/billSummary.do"
ANBILL = f"{net.LIKMS}/bill/bi/bill/detail/anBillInfo.do"

#: 의안 마일스톤 JSON. **HTML 표에서 유도하지 말고 여기서 받는다.**
#:
#: ⚠️ 경로 앞의 ``/bill`` 을 빼면 안 된다. 상세 페이지의 JS 는 ``/bi/common/findBillDetail.do``
#:    라고 적어 두었지만(헬퍼가 접두어를 붙인다), 그 경로를 그대로 부르면 **200 에 euc-kr
#:    레거시 에러 페이지**가 온다 — 404 가 아니라 200 이라 성공으로 읽힌다.
#: ⚠️ 응답은 ``{"data": {...}}`` 로 한 겹 싸여 있다.
#: 폼도 _csrf 도 필요 없다. billNo 하나로 GET 이고 550바이트다.
FINDDETAIL = f"{net.LIKMS}/bill/bi/common/findBillDetail.do"

#: caption 앞머리 → bill_stages.stage.
#:
#: ⚠️ caption 텍스트로 판별한다. 클래스나 순서에 의존하지 마라 — 의안마다 있는 테이블이 다르다.
#: '관련위 심사정보'·'정보이송정보'·'공포정보'는 여기 없다. 앞의 둘은 담을 사실이 없어
#: 버렸고(db.py 의 bill_stages.stage 주석), 공포정보는 단계가 아니라
#: bill_promulgated_laws 로 간다 — 아래 PROMULGATION_CAPTION.
STAGE_BY_CAPTION = {
    "소관위 심사정보": "소관위", "소위 심사정보": "소위",
    "법사위 체계자구 심사정보": "체계자구", "본회의 심의정보": "본회의",
}
#: 공포된 법률 목록. **단계가 아니라 별도 표다** — 일괄개정은 의안 하나가 법률 여러 개를 공포한다.
PROMULGATION_CAPTION = "공포정보"

#: 회의록 id 가 나오는 테이블들 → bill_meetings.stage
MEETING_CAPTIONS = {"소관위 회의정보": "소관위", "법사위 회의정보": "체계자구",
                    "본회의 심의정보": "본회의"}

#: ``<td class>`` → bill_stages 컬럼. **열 위치가 아니라 class 로 읽는다.**
#:
#: ⚠️ 열 위치로 읽으면 테이블마다 열 구성이 달라 조용히 어긋난다. 실측한 두 사고:
#:    '본회의 심의정보'는 상정일·의결일·**회의명**·회의결과 순이라 셋째 칸을 처리결과로
#:    읽으면 의결결과 자리에 회의명이 들어가고, '관련위 심사정보'는 처리결과 칸이 아예
#:    없어(의견서제시일이 끝이다) 넷째 칸을 결과로 읽으면 '문서'가 들어간다.
#:    원천은 값 칸마다 class 를 달아 두었고 그게 열 순서보다 안정적이다.
#: 상정일(presentDt)은 받지 않는다 — 소관위·소위 모두 98% 가 첫 회의일과 같아
#: bill_meetings 가 답한다(db.py 의 bill_stages 주석).
STAGE_CELLS = {
    "committeeName": "committee_name", "submitDt": "date_referred",
    "inscResultCd": "result", "procDt": "date_processed",
}

#: 체계자구 단계의 위원회는 **표에 칸이 없고 caption 이 곧 값이다**('법사위 체계자구 심사정보').
#: v1 은 그래서 1,501행 전부 NULL 이었는데, 그러면 `committee_name='법제사법위원회'` 조회가
#: 그 1,501건을 조용히 놓친다 — 스키마 주석은 "체계자구는 언제나 법사위"라고 말하는데
#: 데이터는 그렇지 않은 상태다. 채워서 둘을 맞춘다.
LEGISLATION_COMMITTEE = "법제사법위원회"

#: 상세를 동시에 받는 워커 수. **실측으로 정했고 근거가 collect_details 주석에 있다.**
DEFAULT_WORKERS = 6


def fetch_list_page(session: net.Session, page: int, rows: int = 1000) -> list[dict]:
    """의안 목록 한 페이지.

    ⚠️ **``reqPageId=billSrch``가 진짜 게이트다.** 폼 63필드를 다 보내도 이 값이 비면
       200에 총건수(20,598)와 페이지네이터는 정확히 오는데 **목록 행만 0개**다.
       필터를 잘못 걸었다고 오판하기 딱 좋다.
    ⚠️ ``_csrf``는 ``form``이 아니라 ``global-hidden-form``에 있다(net.likms_csrf 가 처리).
    """
    fields = {n: "" for n in session.likms_form_fields()}
    fields.update({
        "_csrf": session.likms_csrf(), "reqPageId": "billSrch", "detailedTab": "billDtl",
        "representKindCd": "대표발의", "isPopSelect": "N", "schSorting": "score", "ordCd": "DESC",
        "ageFrom": "22", "ageTo": "22", "page": str(page), "rows": str(rows),
        **{k: "전체" for k in ("billKind", "proposerKind", "procGbnCd", "jntPrpslYn",
                               "cmtResultCd", "mainResultCd", "mainUpdateYn", "expAddiYn",
                               "budgetSubbillCd", "reexamYn", "lawStatus")},
    })
    html = session.xhr(LIST, fields, referer=net.LIKMS_SEARCH_PAGE).text
    tree = HTMLParser(html)
    out = []
    for tr in tree.css("tr"):
        a = tr.css_first("a[data-bill_no]") or tr.css_first("a[data-bill-id]")
        if a is None:
            continue
        bill_no = a.attributes.get("data-bill_no")
        if not bill_no:
            continue
        # ⚠️ 의안명 셀 앞에 '계류의안'/'처리의안' 배지가 <i class="ico_kye"> 로 붙어 있다.
        #    a.text() 를 그대로 쓰면 **전량에 배지가 섞인다** — 실측으로 20,598건 전부 그랬다.
        #    배지 노드를 지우고 남은 텍스트를 쓴다.
        for i_tag in a.css("i"):
            i_tag.decompose()
        title = re.sub(r"\s+", " ", a.text(strip=True))
        # 의안명 꼬리의 '(홍길동의원 등 13인)' 은 발의자 **기대 건수**다.
        # replace_children 이 부분 실패를 가리는 근거로 쓴다 — 원천이 스스로 말한 숫자다.
        summary = None
        if pm := re.search(r"\(([^()]*?의원(?:\s*등\s*(\d+)\s*인)?)\)\s*$", title):
            summary = pm.group(1)
        cells = [td.text(separator=" ", strip=True) for td in tr.css("td")]
        out.append({
            "bill_no": bill_no, "bill_id": a.attributes.get("data-bill-id"),
            "assembly_unit": 22, "title": title, "proposer_summary": summary,
            "proposer_kind": cells[2] if len(cells) > 2 else None,
            "date_proposed": cells[3] if len(cells) > 3 else None,
            "date_decided": cells[4] if len(cells) > 4 else None,
            "decision_result": cells[5] if len(cells) > 5 else None,
            "review_status": cells[-1] if cells else None,
        })
    # ⚠️ data-bill-id 는 행마다 두 번 나온다. bill_no 기준으로 중복 제거한다.
    return list({r["bill_no"]: r for r in out}.values())


def collect_list(db, session: net.Session) -> int:
    total = 0
    page = 1
    while True:
        rows = fetch_list_page(session, page)
        if not rows:
            break
        for r in rows:
            r["bill_kind"] = r.get("bill_kind") or "미상"   # 상세에서 billKindCd 로 덮인다
            dbm.upsert_bill_list(db, r)
        total += len(rows)
        print(f"  page {page:>2d}  {len(rows):>5,d}행  누적 {total:>6,d}")
        if len(rows) < 1000:
            break
        page += 1
    return total


def parse_detail(html: str) -> dict:
    """상세 GET에서 숨은 필드와 대표발의자를 뽑는다.

    ⚠️ **숨은 input 전부를 이름→값 딕셔너리로 접으면 안 된다.** 페이지의 hidden input 은
       ``form``·``historyForm`` 두 form 에 흩어져 있고 ``billNo``·``reexamYn``이 **양쪽에
       중복**된다 — ``historyForm`` 쪽은 검색조건 echo 용 **빈 문자열**이라 뒤에 오는 것이
       이겨 ``billNo=''``가 된다. 그러면 billInfo.do 가 200에 caption 7개를 다 주는 것처럼
       보이는데 **공포번호·공포법률명 칸만 빈다.**
       그래서 ``<form id="form">`` 안의 필드만 뽑는다 (실측 정확히 12개).
    ⚠️ 대표발의자는 이 카드가 **유일하게 확실한 근거**다. 발의자 팝업은 대표발의를 전혀
       표시하지 않고 그냥 목록 맨 앞에 있을 뿐이라 순서에 기대면 안 된다.
       위원장 대안과 정부제출에는 이 카드가 아예 없다 — 그게 정상이다.
    """
    m = re.search(r'<form[^>]*\bid="form"[^>]*>(.*?)</form>', html, re.S)
    if not m:
        raise ValueError("form#form 을 찾지 못했다")
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', m.group(1)))
    if not fields.get("billNo"):
        raise ValueError("billNo 가 비었다 — 잘못된 form 을 읽었다")
    slugs = re.findall(r"/members/22nd/(\w+)", html)
    return {"fields": fields, "represent": slugs[0] if slugs else None}


def cell_values(tr) -> dict[str, str]:
    """한 행을 ``td class → 값``으로 접는다.

    ⚠️ **``td.text()``를 그대로 쓰면 안 된다.** 값 칸이 전부
       ``<td class="announceNo"><i>공포번호</i><span>20676</span></td>`` 모양이고
       ``<i>``는 좁은 화면용 라벨이다. 그냥 읽으면 공포번호가 ``'공포번호 20676'``이
       된다 — 실측으로 여섯 예제 전부 그렇게 들어갔다. 목록의 ``<i class="ico_kye">``
       배지와 같은 계열이니, 이 사이트에서 ``<i>``는 일단 지우고 보는 게 맞다.
    ⚠️ 지우는 것은 **직계 자식 ``<i>`` 뿐이다.** 통째로 지우면 문서 칸 안의 링크까지
       날아갈 수 있고, 그 칸에 회의록 id 가 들어 있다.
    ⚠️ 값은 ``<span>``에 있다. 라벨만 지우고 나머지를 다 읽으면 **UI 버튼 글자가 값에
       붙는다** — 철회된 의안의 회의결과 칸이 ``<span>철회</span><a>철회자 목록</a>``이라
       ``'철회 철회자 목록'``으로 들어갔고, 그러면 ``result = '철회'`` 조회가 그 의안을
       놓친다. 노이즈를 하나씩 지우는 대신 **값 노드를 고른다** — 원천에 새 버튼이
       붙어도 안 샌다. 직계 span 이 없는 칸(공포법률은 ``<a>``다)만 전체 텍스트로 떨어진다.
    """
    out: dict[str, str] = {}
    for td in tr.css("td"):
        cls = (td.attributes.get("class") or "").split()
        if not cls:
            continue
        for label in [n for n in td.iter() if n.tag == "i"]:
            label.decompose()
        spans = [n for n in td.iter() if n.tag == "span"]
        text = (" ".join(n.text(separator=" ", strip=True) for n in spans) if spans
                else td.text(separator=" ", strip=True))
        # ⚠️ 원천이 줄바꿈을 **이중 이스케이프**해서 보내는 칸이 있다 — `&lt;br&gt;` 가
        #    텍스트 노드에 그대로 남아 값이 '수정안반영폐기<br>예산부수법률안 …' 이 된다.
        #    `<br>` 뒤는 부연이고 앞이 값이다. 잘라 두지 않으면 result 가 열거형이기를
        #    멈춰서 `result='수정안반영폐기'` 조회가 그 39건을 놓친다.
        text = re.split(r"<br\s*/?>", text, maxsplit=1, flags=re.I)[0]
        out[cls[0]] = re.sub(r"\s+", " ", text).strip() or None
    return out


def parse_info(html: str) -> tuple[list[dict], list[dict], list[dict], dict]:
    """caption 테이블 → 심사 단계 · 회의 · 공포법률 · bills 보강값."""
    tree = HTMLParser(html)
    stages, meetings, laws, extra = [], [], [], {}

    for table in tree.css("table"):
        cap = table.css_first("caption")
        if cap is None:
            continue
        head = re.split(r"\s*:", cap.text(strip=True))[0].strip()
        rows = [(tr, c) for tr in table.css("tr") if (c := cell_values(tr))]
        if not rows:
            continue

        if head == "의안접수정보":
            # ⚠️ **'제N회'를 집어야 한다. 첫 숫자가 아니다.** 원천은
            #    '제22대(2024~2028)제417회' 로 주므로 맨 앞 숫자를 잡으면 대수(22)가
            #    회기 자리에 들어간다 — 전 행이 22가 되고 그래도 정수라 아무도 못 알아챈다.
            #    문자열로 두면 회기 범위 질의가 사전순 비교로 조용히 틀린다.
            if m := re.search(r"제\s*(\d+)\s*회", rows[0][1].get("sessionTitle") or ""):
                extra["proposal_session"] = int(m.group(1))
        elif head == "정부재의안":
            # 재의(대통령 거부권) 결과의 **구조화된 원천**이다. 03번은 이 값이 오직
            # bill_memo 산문에만 있다고 적었는데, 실측해 보니 caption 테이블이 따로 있고
            # procResultName 칸이 '부결'을 그대로 준다(상법 2208496). 산문 정규식은
            # 아래에 대비책으로만 남긴다.
            extra["reconsideration_result"] = rows[0][1].get("procResultName")

        if head in MEETING_CAPTIONS:
            for tr, c in rows:
                # 회의록 칸의 pdf.do?id= 가 회의록 본문 URL의 그 id다
                link = tr.css_first('a[href*="pdf.do?id="]')
                if link is None:
                    continue
                meetings.append({
                    "conference_id": int(re.search(r"id=(\d+)",
                                                   link.attributes["href"]).group(1)),
                    "stage": MEETING_CAPTIONS[head],
                    "meeting_name": c.get("mtngName"),
                    # 본회의 심의정보에는 회의일(mtngDt) 칸이 없다 — 의결일이 그날이다
                    "date_meeting": c.get("mtngDt") or c.get("procDt"),
                    "result": c.get("procResultName") or c.get("inscResultCd"),
                })

        if head == PROMULGATION_CAPTION:
            for _tr, c in rows:
                # ⚠️ 공포법률명은 의안명과 다르다 — '…기본법안(대안)' → '…기본법'.
                #    바깥 자료와 이름으로 맞출 때 쓰는 것은 이쪽이다.
                if name := c.get("lawName"):
                    laws.append({"law_name": name, "law_no": c.get("announceNo")})
            continue

        if head not in STAGE_BY_CAPTION:
            continue
        stage = STAGE_BY_CAPTION[head]
        for _tr, c in rows:
            row = {"stage": stage, "committee_name": None, "date_referred": None,
                   "date_processed": None, "result": None, "seq": 1}
            for cls, field in STAGE_CELLS.items():
                if c.get(cls) is not None:
                    row[field] = c[cls]
            # 체계자구는 표에 위원회 칸이 없고 caption 이 곧 값이다. 비워 두면
            # `committee_name='법제사법위원회'` 조회가 이 단계를 통째로 놓친다.
            if stage == "체계자구" and not row["committee_name"]:
                row["committee_name"] = LEGISLATION_COMMITTEE
            stages.append(row)

    # bill_memo — caption 테이블 **밖**이라 caption 매칭 파서로는 절대 못 잡는다.
    if memo := tree.css_first("pre.bill_memo"):
        extra["status_memo"] = memo.text(strip=True)
        if not extra.get("reconsideration_result"):
            if r := re.search(r"재의(?:를 부친 결과|결과)[^.]*?(가결|부결)", extra["status_memo"]):
                extra["reconsideration_result"] = r.group(1)

    # 소위 단계의 위원회 이름을 소관위로 수식한다 — meetings.parse_meta 와 같은 규칙이다.
    # ⚠️ 원천은 소위 심사정보 칸에 소위 이름만 주는데 그 이름은 상위 위원회를 넘어 중복된다
    #    ('법안심사제1소위원회'는 법사위·행안위·복지위·정무위에 다 있다). 맨 이름으로 넣으면
    #    회의록 쪽이 만든 수식된 이름과 갈라져 같은 소위가 committees 에 두 행이 되고,
    #    "이 소위를 거친 법안"과 "이 소위의 회의"가 서로 다른 위원회를 가리키게 된다.
    #    같은 문서 안의 소관위가 곧 그 소위의 상위다.
    if owner := next((s["committee_name"] for s in stages if s["stage"] == "소관위"), None):
        for s in stages:
            if s["stage"] == "소위" and s["committee_name"]:
                s["parent"] = owner
                s["committee_name"] = f"{owner} {s['committee_name']}"

    # seq — 정렬 순위로 매긴다. 다만 **여기에 멱등성을 걸지는 않는다.**
    # 소위는 절반이 committee_name 이 비어 정렬 타이가 남고, 타이가 뒤집히면 같은 사건이
    # 다른 seq 를 받는다. 그래서 쓰기 쪽이 UPSERT 가 아니라 DELETE+INSERT 다
    # (db.py 의 replace_children_txn). 이 정렬은 사람이 읽을 때의 순서일 뿐이다.
    for stage in {s["stage"] for s in stages}:
        group = sorted((s for s in stages if s["stage"] == stage),
                       key=lambda s: (s["date_referred"] or s["date_processed"] or "",
                                      s["committee_name"] or ""))
        for i, s in enumerate(group, 1):
            s["seq"] = i
    return stages, meetings, laws, extra


def parse_votes(html: str) -> tuple[dict, list[dict], list[dict]]:
    """표결 요약과 의원별 표. 돌려주는 셋째 값은 **slug 없는 표들**이다.

    ⚠️ 명단은 찬성·반대·기권 셋뿐이고 **불참 명단은 없다.** 행이 없는 것은 불참이 아니라
       "그 의원의 표를 우리가 모른다"이다. 불참을 세려면 재적·재석 숫자로 계산하라.
    ⚠️ 이름은 있는데 ``/members/`` 링크가 없는 li 가 있다(실측: 찬성 193 중 8).
       PK 가 slug 라 그대로는 저장할 수 없다. **이름을 들고 돌려주면** 호출자가
       members 로 이름을 맞춰 회수할 수 있다 — 조용히 버리면 "표결 행 수 ==
       찬성+반대+기권"이 영영 안 맞는데 이유를 알 수 없게 된다.
    """
    tree = HTMLParser(html)
    text = re.sub(r"\s+", " ", tree.body.text(separator=" ") if tree.body else "")
    num = lambda pat: (int(m.group(1).replace(",", "")) if (m := re.search(pat, text)) else None)
    summary = {"total_seats": num(r"재적\s*([\d,]+)"), "present": num(r"재석\s*([\d,]+)"),
               "yes": num(r"찬성\s*([\d,]+)"), "no": num(r"반대\s*([\d,]+)"),
               "abstain": num(r"기권\s*([\d,]+)")}
    if d := re.search(r"(\d{4}-\d{2}-\d{2})", text):
        summary["date_voted"] = d.group(1)

    votes, unlinked = [], []
    for uid, label in (("voteAgreeList", "찬성"), ("voteDisAgreeList", "반대"),
                       ("voteAbsList", "기권")):
        ul = tree.css_first(f"#{uid}")
        if ul is None:
            continue
        for li in ul.css("li"):
            if not (txt := dbm.norm_text(li.text(strip=True))):
                continue
            a = li.css_first('a[href*="/members/"]')
            if a is None:
                unlinked.append({"name": txt, "vote": label})
                continue
            votes.append({"open_na_id": a.attributes["href"].rstrip("/").split("/")[-1],
                          "vote": label})
    return summary, list({v["open_na_id"]: v for v in votes}.values()), unlinked


def parse_proposer_page(html: str) -> tuple[list[dict], dict]:
    """발의자 팝업 한 페이지 → (명단, 숨은 필드).

    ⚠️ **앵커 *뒤*의 텍스트를 읽으면 안 된다.** 한 명이 사진 카드로 오고 값은 앵커 *안*에 있다:

        <a href=".../members/22nd/LEEHAIMIN"><div><img></div>
           <p>이해민</p><p>李海珉</p><p class="jdang">조국혁신당</p></a>

       앵커 다음 텍스트를 잡는 정규식은 ``<div>`` 앞의 공백만 집어 **이름·한자·정당이
       전부 None 으로 들어간다.** 200 이고 발의자 수도 맞아서 어디서도 안 걸린다 —
       실측에서 의원 332명 **전원**의 party 가 NULL 이었던 원인이 이것이다.
    """
    tree = HTMLParser(html)
    hidden = {}
    for h in tree.css("input[type=hidden]"):
        key = h.attributes.get("id") or h.attributes.get("name")
        if key:
            hidden[key] = h.attributes.get("value") or ""
    out = []
    for a in tree.css('a[href*="/members/22nd/"]'):
        slug = a.attributes["href"].rstrip("/").split("/")[-1]
        party = next((p.text(strip=True) for p in a.css("p.jdang")), None)
        plain = [t for p in a.css("p") if (t := p.text(strip=True)) and t != party]
        out.append({"open_na_id": slug,
                    "name": dbm.norm_text(plain[0]) if plain else None,
                    "name_hanja": dbm.norm_text(plain[1]) if len(plain) > 1 else None,
                    "party": dbm.norm_text(party)})
    return out, hidden


def lead_names(info: str | None) -> list[str]:
    """팝업의 ``info`` 에서 **대표발의자 이름들**을 뽑는다.

    ``'최형두의원ㆍ이준석의원ㆍ황정아의원 등 11인'`` → ``['최형두', '이준석', '황정아']``
    ``'김석기의원 등 191인'``                        → ``['김석기']``
    ``'행정안전위원장'``                             → ``[]``

    ⚠️ **대표발의가 한 명이라는 가정이 v1 의 결함이었다.** 상세 HTML 의 첫 ``/members/``
       링크만 대표로 잡아서 ``role='대표발의'`` 가 2명 이상인 의안이 **0건**이었다.
       원천은 여기서 전원을 직접 말해 준다 — 순서나 위치가 아니라 선언된 값이다.
    """
    if not info:
        return []
    head = dbm.norm_text(re.split(r"\s*등\s*\d+\s*인", info)[0]) or ""
    return [n for part in head.split("·") if (n := re.sub(r"의원\s*$", "", part).strip())
            and part.strip().endswith("의원")]


def fetch_proposers(session: net.Session, bill_id: str, bill_no: str) -> tuple[list[dict], int]:
    """발의자 **전원**과 원천이 선언한 총원.

    ⚠️ **원천이 14명씩 페이징한다.** v1 은 1페이지만 읽어 191명짜리 의안에서 14명만
       가져왔고, 전체로는 34,270명이 사라졌다. 그런데 200 이고 파싱도 성공해서
       어디서도 안 걸렸다.
    ⚠️ **빈 응답을 종료 조건으로 쓰지 마라 — 무한 루프다.** ``page`` 가 마지막을 넘으면
       에러가 아니라 **마지막 페이지를 다시 준다.** 그래서 원천이 선언한
       ``pager_pages_text``(``' (1/14 페이지)'``)로 페이지 수를 먼저 정하고 그만큼만 돈다.
    """
    def one(page: int) -> tuple[list[dict], dict]:
        r = session.xhr(PROPOSER, {"billId": bill_id, "billNo": bill_no, "page": str(page),
                                   "_csrf": session.likms_csrf()},
                        referer=net.LIKMS_SEARCH_PAGE)
        return parse_proposer_page(r.text)

    rows, hidden = one(1)
    # 총원은 같은 응답 안의 **독립적인 출처**다. 제목의 '등 N인' 을 쓰지 마라 —
    # '외 N인' 44건과 위원장·정부 발의 1,654건에서 깨진다.
    declared = int(re.sub(r"[^\d]", "", hidden.get("pager_count_text") or "0") or 0)
    pages = 1
    if m := re.search(r"/\s*(\d+)\s*페이지", hidden.get("pager_pages_text") or ""):
        pages = int(m.group(1))
    leads = set(lead_names(hidden.get("info")))
    for p in range(2, pages + 1):
        more, _ = one(p)
        rows.extend(more)

    seen: dict[str, dict] = {}
    for r in rows:
        r["role"] = "대표발의" if r["name"] in leads else "공동발의"
        seen.setdefault(r["open_na_id"], r)
    return list(seen.values()), declared


def parse_alternatives(html: str) -> tuple[str | None, list[str]]:
    """대안 탭 → ``(흡수한 대안의 의안번호, 흡수된 원안 의안번호들)``.

    **한 번의 요청이 양방향을 다 준다.** 원안을 물어도 대안을 물어도 같은 표가 오고,
    의미가 방향에 상관없이 같다 — caption ``'대안반영'`` 한 행이 대안이고,
    caption ``'대안반영폐기'`` 의 N행이 그 대안에 흡수된 원안 전부다(자기 자신 포함).
    그래서 의안 하나를 받으면 형제 전부의 관계까지 한꺼번에 만들어진다.

    ⚠️ **원천이 같은 표를 두 벌 낸다**(실측: caption 이 네 개인데 내용은 두 개다).
       그대로 읽으면 행이 두 배가 된다. caption 첫 등장만 쓴다.
    """
    tree = HTMLParser(html)
    alt: str | None = None
    absorbed: list[str] = []
    seen: set[str] = set()
    for table in tree.css("table"):
        cap = table.css_first("caption")
        if cap is None:
            continue
        head = re.split(r"\s*:", cap.text(strip=True))[0].strip()
        if head not in ("대안반영", "대안반영폐기") or head in seen:
            continue
        seen.add(head)
        nos = []
        for tr in table.css("tr"):
            c = cell_values(tr)
            no = c.get("billNo")
            if not no and (m := re.search(r"\b(2\d{6})\b", tr.text(separator=" "))):
                no = m.group(1)
            if no:
                nos.append(no)
        if head == "대안반영":
            alt = nos[0] if nos else None
        else:
            absorbed = nos
    return alt, absorbed


#: 계류 의안의 상세를 다시 받는 주기(일). 심사단계는 계류 중에도 계속 움직이므로
#: 한 번 받고 두면 **14,000건이 영원히 낡는다** — v2 가 존재하는 이유의 절반이 이것이다.
DEFAULT_REFRESH_DAYS = 30


def todo_sql(refresh_days: int = DEFAULT_REFRESH_DAYS) -> tuple[str, list]:
    """상세를 받아야 할 의안. **collect.py 와 공유한다** — 두 곳에 적으면 갈라진다.

    받아야 할 이유가 넷이다:

    1. 아직 안 받았다 (``detail_collected_at IS NULL``)
    2. 원장에 살아 있는 실패가 붙어 있다 — 없으면 한 번 받은 뒤 실패한 재수집이 영영
       재시도되지 않고 ``unresolved_retriable`` 이 영구히 빨갛다
    3. **목록이 그 뒤에 바뀌었다** (``updated_at > detail_collected_at``)
    4. **계류인데 오래됐다** — 이게 없으면 계류 14,000건의 심사단계가 굳는다

    ⚠️ 상한 초과분을 빼는 마지막 절이 없으면 **영구 재시도 루프**가 된다. 발의자 수가
       안 맞으면 ``detail_collected_at`` 을 안 쓰므로, 상한(5회)을 넘겨도 1번 조건이
       계속 참이라 매 실행이 같은 의안을 다시 받는다.
    ⚠️ 주기 비교를 SQL 의 ``datetime('now','-30 day')`` 로 하지 마라. 그건 **UTC** 인데
       우리가 저장하는 시각은 ``dbm.now_str()`` 의 **KST** 라 9시간이 어긋난다.
       경계선의 의안이 하루 일찍/늦게 걸리고, 그 어긋남은 아무 에러도 내지 않는다.
       그래서 기준 시각을 파이썬에서 KST 로 계산해 넘긴다.
    """
    cutoff = (datetime.now(dbm.KST) - timedelta(days=refresh_days)).strftime("%Y-%m-%d %H:%M:%S")
    sql = """
        SELECT bill_no, bill_id FROM bills
        WHERE (bill_kind = '법률안' OR bill_kind = '미상')
          AND (detail_collected_at IS NULL
               OR EXISTS (SELECT 1 FROM collect_failures f
                          WHERE f.target_kind='bill_detail' AND f.target_key=bills.bill_no
                            AND f.kind='retriable' AND f.attempts < ?)
               OR updated_at > detail_collected_at
               OR (outcome = '계류' AND detail_collected_at < ?))
          AND NOT EXISTS (SELECT 1 FROM collect_failures f2
                          WHERE f2.target_kind='bill_detail' AND f2.target_key=bills.bill_no
                            AND (f2.kind='gone' OR f2.attempts >= ?))"""
    return sql, [dbm.MAX_ATTEMPTS, cutoff, dbm.MAX_ATTEMPTS]


def collect_detail(db, session: net.Session, bill_no: str, bill_id: str) -> bool:
    """한 의안 = 한 트랜잭션. **``detail_collected_at``은 맨 마지막에 쓴다.**

    먼저 쓰고 중간에 죽으면 완결 표시가 붙은 빈 의안이 생기고, 그 의안은 다시 수집되지 않는다.
    """
    try:
        d = parse_detail(session.get(f"{DETAIL}?billId={bill_id}").text)
        f = d["fields"]
        # ⚠️ 받은 문서가 요청한 의안이 맞는지 대조한다. record 회의록 뷰어에서 **같은 URL이
        #    다른 회의를 주는 것**을 실측했기 때문에 같은 계열 인프라를 의심하는 것이다.
        #    likms 는 15회 연속 요청에서 불일치가 없었지만(응답 크기까지 동일), 방어 비용이
        #    사실상 0이다 — billNo 가 이미 파싱하는 폼 안에 있다. 2만 건을 돌리는 동안
        #    한 번이라도 어긋나면 그 의안의 단계·발의자·표결이 통째로 남의 것이 된다.
        if f.get("billNo") != bill_no:
            raise ValueError(f"다른 의안의 문서가 왔다 (요청 {bill_no} / 문서 {f.get('billNo')})")
        info = session.xhr(INFO, {**f, "_csrf": session.likms_csrf()},
                           referer=net.LIKMS_SEARCH_PAGE).text
        stages, meetings, laws, extra = parse_info(info)
        # 마일스톤은 HTML 표에서 유도하지 않고 JSON 에서 받는다. 550바이트 · GET · 폼 불필요.
        js = session.get(f"{FINDDETAIL}?billNo={bill_no}").json()["data"]
        if js.get("billNo") != bill_no:
            raise ValueError(f"JSON 이 다른 의안이다 (요청 {bill_no} / 응답 {js.get('billNo')})")
    except Exception as e:
        dbm.record_failure(db, "bill_detail", bill_no, detail=f"parse:{type(e).__name__}: {e}")
        return False

    extra["bill_kind"] = f.get("billKindCd") or None
    extra["is_reexamination"] = 1 if f.get("reexamYn") == "Y" else 0
    extra["withdraw_count"] = int(f.get("withdrawCnt") or 0)
    extra["ref_bill_id"] = f.get("selRefBillId") or None
    extra["head_memo"] = dbm.norm_text(f.get("headMemoInfo"))
    # JSON 이 직접 말해 주는 것들. v1 은 committee_name 을 HTML 에서 유도하려다 19,985건
    # 전부 NULL 이었다.
    extra["current_committee"] = dbm.norm_text(js.get("currCmt"))
    extra["date_transferred"] = js.get("govTransDt") or None
    extra["date_promulgated"] = js.get("announceDt") or None
    try:
        s = session.get(f"{SUMMARY}?billId={bill_id}")
        if s.status_code == 200:
            # ⚠️ **body 를 통째로 담지 마라.** 이 팝업은 제목·`창닫기` 버튼·의안명·제안자에
            #    더해 **<script> 소스까지** 갖고 있고, body.text() 는 그걸 다 긁어 온다.
            #    실측에서 20,598건 전량이 그렇게 오염됐고, 이 컬럼이 키워드 검색의 주
            #    대상이라 검색이 자바스크립트를 물었다.
            # ⚠️ 본문이 없는 의안은 <pre> 가 아예 없고 '해당 의안 정보를 찾을 수 없습니다'
            #    안내문만 온다(실측 654건). 그때는 **NULL 이어야 한다** — 안내문을 담으면
            #    reason_text IS NOT NULL 이 참이 되고 검색이 그 문장을 물어 온다.
            pre = HTMLParser(s.text).css_first("pre")
            extra["reason_text"] = pre.text(strip=True) if pre else None
    except Exception:
        pass   # 제안이유는 없을 수 있다. 판정자로 쓰지 않으므로 실패해도 진행한다

    votes = proposers = None
    declared = None
    vote_unlinked: list[dict] = []
    alt_edges: list[dict] = []
    if any(st["stage"] == "본회의" for st in stages):
        try:
            # ⚠️ **폼 전체를 보낸다.** {billId, _csrf} 만 보내면 400 "Bad Request." 12바이트가
            #    오고, 우리는 그걸 "원천이 표결을 안 준다"로 읽었다 — 수정가결 22건의 표결이
            #    없다던 진단이 사실은 우리 요청 문제였다. 바로 위 billInfo.do 가 이미 같은
            #    폼을 보내고 있으므로 그대로 재사용하면 된다.
            vs, vlist, vote_unlinked = parse_votes(
                session.xhr(VOTE, {**f, "_csrf": session.likms_csrf()},
                            referer=net.LIKMS_SEARCH_PAGE).text)
            votes = (vs, vlist)
            # ⚠️ **"표결이 없다"와 "표결을 못 받았다"를 가른다.** 본회의 단계가 있어도
            #    표결이 없는 의안이 정상으로 존재한다(대안반영폐기 등) — 그걸 실패로 적으면
            #    고칠 수 없는 빨간불이 수천 건 쌓이고, 그러면 사람이 원장 전체를 안 믿는다.
            #    실제로 표결에 부쳐진 의안만 판정 대상이다.
            if vs.get("yes") is None and js.get("procResultName") in ("원안가결", "수정가결", "부결"):
                dbm.record_failure(db, "bill_vote", bill_no, detail="parse:표결 요약 숫자가 없다")
        except Exception as e:
            dbm.record_failure(db, "bill_vote", bill_no, detail=f"parse:{type(e).__name__}")
    if js.get("proposerKindCd") == "의원":
        try:
            proposers, declared = fetch_proposers(session, bill_id, bill_no)
            # 상세 카드의 대표발의자가 팝업이 선언한 대표 명단 밖이면 둘 중 하나가 틀린 것이다.
            # 데이터는 넣되(공동발의로라도 남는 편이 낫다) 흔적을 남긴다.
            if d["represent"] and not any(
                    p["open_na_id"] == d["represent"] and p["role"] == "대표발의"
                    for p in proposers):
                dbm.record_failure(db, "bill_proposer", bill_no,
                                   detail=f"parse:대표발의 불일치 (카드 {d['represent']})")
        except Exception as e:
            dbm.record_failure(db, "bill_proposer", bill_no, detail=f"parse:{type(e).__name__}")
    # 대안 관계는 결말이 그것을 시사하는 의안에만 붙인다(약 5,300건). 전량에 붙이면
    # 2만 요청이 늘고 대부분은 빈 표다.
    if (js.get("procResultName") in ("대안반영폐기", "본회의불부의")
            or js.get("proposerKindCd") == "위원장"):
        try:
            alt_no, absorbed = parse_alternatives(
                session.xhr(ANBILL, {**f, "_csrf": session.likms_csrf()},
                            referer=net.LIKMS_SEARCH_PAGE).text)
            if alt_no:
                # 한 요청이 형제 전부의 관계를 준다 — 자기 것만 담고 버리면 그 정보를 잃는다.
                alt_edges = [{"bill_no": o, "alt_bill_no": alt_no}
                             for o in dict.fromkeys([*absorbed, bill_no]) if o != alt_no]
        except Exception as e:
            dbm.record_failure(db, "bill_alt", bill_no, detail=f"parse:{type(e).__name__}")

    # ⚠️ **BEGIN 이 아니라 BEGIN IMMEDIATE 다.** 이 트랜잭션은 읽기로 시작해서(아래
    #    review_status 조회) 쓰기로 넘어가는데, 기본 DEFERRED 는 그 승격 시점에
    #    busy_timeout 을 **기다리지 않고 곧바로** SQLITE_BUSY 를 낸다 — 승격은 교착이
    #    될 수 있어 SQLite 가 대기를 거부한다. 6워커에서 실측 40건 중 3건이 그렇게
    #    'database is locked' 로 죽었다. IMMEDIATE 는 쓰기 락을 처음부터 잡아
    #    busy_timeout(30초)이 정상적으로 듣는다.
    db.execute("BEGIN IMMEDIATE")
    try:
        # ⚠️ **outcome 은 여기 한 곳에서만 계산한다.** 목록 UPSERT 가 같이 건드리면
        #    감사의 outcome_mismatch 가 "저장값 ≠ 재계산값"이라는 뜻을 잃는다.
        cur = db.execute("SELECT review_status, decision_result FROM bills WHERE bill_no = ?",
                         (bill_no,)).fetchone()
        extra["outcome"] = dbm.derive_outcome(
            cur["review_status"] if cur else None,
            cur["decision_result"] if cur else None,
            extra.get("reconsideration_result"))
        # ⚠️ **위원회를 먼저 등록하고 그 반환값을 넣는다.** 원천 표기를 그대로 UPDATE 하면
        #    committees 에 없는 이름이라 FK 위반으로 의안 하나가 통째로 롤백된다.
        if extra.get("current_committee"):
            extra["current_committee"] = dbm.upsert_committee(db, extra["current_committee"])
        sets = ", ".join(f"{k} = ?" for k in extra)
        db.execute(f"UPDATE bills SET {sets} WHERE bill_no = ?", [*extra.values(), bill_no])
        for st in stages:
            if st["committee_name"]:
                # 돌려받은 표기를 쓴다 — 원천마다 공백이 달라 다른 이름으로 넣으면 FK 위반이다
                st["committee_name"] = dbm.upsert_committee(
                    db, st["committee_name"],
                    committee_class="소위원회" if st["stage"] == "소위" else None,
                    parent=st.get("parent"))
            st.pop("parent", None)
        # ⚠️ UPSERT 가 아니라 DELETE+INSERT 다. 소위 절반이 committee_name 이 비어 정렬
        #    타이가 생기고, 타이가 뒤집히면 UPSERT 는 덮어쓰기 대신 삽입으로 동작해
        #    에러 없이 행만 는다. 이 트랜잭션이 통째로 롤백되므로 지우고 넣어도 안전하다.
        dbm.replace_children_txn(db, "bill_stages", "bill_no", bill_no,
                                 [{"bill_no": bill_no, **st} for st in stages])
        dbm.replace_children_txn(db, "bill_promulgated_laws", "bill_no", bill_no,
                                 [{"bill_no": bill_no, **lw} for lw in laws])
        if alt_edges:
            # 형제들의 행까지 같이 만든다. 자기 것만 담으면 역방향 질의가 반만 답한다.
            # ⚠️ **우리가 모르는 의안은 건너뛴다.** 형제 중 하나가 bills 에 없으면 FK 위반이
            #    나고, 그러면 그 의안의 상세가 통째로 롤백된다 — 남의 행 하나 때문에
            #    내 데이터를 잃는 구조가 된다. 목록을 먼저 받으므로 정상 경로에서는 다 있다.
            db.executemany(
                "INSERT INTO bill_alternatives (bill_no, alt_bill_no) "
                "SELECT ?, ? WHERE EXISTS (SELECT 1 FROM bills WHERE bill_no = ?) "
                "ON CONFLICT(bill_no, alt_bill_no) DO NOTHING",
                [(e["bill_no"], e["alt_bill_no"], e["bill_no"]) for e in alt_edges])
        for mt in meetings:
            db.execute("""INSERT INTO bill_meetings VALUES (?,?,?,?,?,?)
                          ON CONFLICT(bill_no, conference_id) DO UPDATE SET
                              stage=excluded.stage, result=excluded.result""",
                       (bill_no, mt["conference_id"], mt["stage"], mt["meeting_name"],
                        mt["date_meeting"], mt["result"]))
        if proposers:
            for p in proposers:
                dbm.upsert_member(db, p["open_na_id"], name=p["name"],
                                  name_hanja=p["name_hanja"], party=p["party"])
            # ⚠️ **expected 는 원천이 선언한 총원이다. len(proposers) 가 아니다.**
            #    자기 자신의 길이를 기대값으로 주면 언제나 참이라 검사가 아니고,
            #    v1 에서 발의자 34,270명이 사라진 방식이 정확히 그것이다.
            dbm.replace_children(db, "bill_proposers", "bill_no", bill_no,
                                 [{"bill_no": bill_no, "open_na_id": p["open_na_id"],
                                   "role": p["role"]} for p in proposers],
                                 expected=declared or None)
        # ⚠️ 본회의 단계가 있어도 표결이 없는 의안이 있다(대안반영폐기 등).
        #    그때 요약 행을 만들면 숫자가 전부 NULL인 유령 행이 남아 "표결이 있다"는
        #    조인이 그 의안을 잡는다. 실제 표결 숫자가 온 경우에만 쓴다.
        if votes and votes[0].get("yes") is not None:
            vs, vlist = votes
            # result 를 리터럴 None 으로 두면 이 컬럼이 영원히 빈다 — 표결 결과를 묻는
            # 질의가 bills 를 다시 조인하게 되고, 그러면 이 표를 둘 이유가 없어진다.
            # JSON 의 procResultName 이 그 값이다('원안가결'·'수정가결'·'부결').
            db.execute("""INSERT INTO bill_vote_summary VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(bill_no) DO UPDATE SET
                              date_voted=excluded.date_voted, total_seats=excluded.total_seats,
                              present=excluded.present, yes=excluded.yes, no=excluded.no,
                              abstain=excluded.abstain, result=excluded.result""",
                       (bill_no, vs.get("date_voted"), vs.get("total_seats"), vs.get("present"),
                        vs.get("yes"), vs.get("no"), vs.get("abstain"),
                        dbm.norm_text(js.get("procResultName")), dbm.now_str()))
            # slug 없는 표를 **이름으로 회수한다.** 링크가 없을 뿐 이름은 적혀 있다.
            # ⚠️ 동명이인은 회수하지 않는다 — 22대에 박지원이 둘이다(PARKJIEWON /
            #    PARKJIWON). 이름만으로 고르면 반은 남의 표가 되고, 표결 기록에서
            #    그건 조용한 오답 중 최악이다. 못 고른 것은 세어서 기대값에서 뺀다.
            unresolved = 0
            for u in vote_unlinked:
                cand = [r[0] for r in db.execute(
                    "SELECT open_na_id FROM members WHERE name = ?", (u["name"],))]
                if len(cand) == 1:
                    vlist.append({"open_na_id": cand[0], "vote": u["vote"]})
                else:
                    unresolved += 1
            vlist = list({v["open_na_id"]: v for v in vlist}.values())
            for v in vlist:
                dbm.upsert_member(db, v["open_na_id"])
            # ⚠️ 여기도 len(vlist) 를 쓰면 안 된다 — 발의자와 **똑같은 자기 자신 검사**다.
            #    독립적인 출처는 요약의 찬성+반대+기권이다.
            expected_votes = sum(vs.get(k) or 0 for k in ("yes", "no", "abstain")) - unresolved
            dbm.replace_children(db, "bill_votes", "bill_no", bill_no,
                                 [{"bill_no": bill_no, **v} for v in vlist],
                                 expected=expected_votes or None)
            if unresolved:
                dbm.record_failure(db, "bill_vote", bill_no,
                                   detail=f"parse:이름으로 회수 못한 표 {unresolved}건 (동명이인)")
        db.execute("UPDATE bills SET detail_collected_at = ? WHERE bill_no = ?",
                   (dbm.now_str(), bill_no))
        db.execute("COMMIT")
    except Exception as e:
        db.execute("ROLLBACK")
        dbm.record_failure(db, "bill_detail", bill_no, detail=f"write:{type(e).__name__}: {e}")
        return False

    dbm.clear_failure(db, "bill_detail", bill_no)
    return True


def collect_details(db_path: str, todo: list[tuple[str, str]], *,
                    rate: float = net.DEFAULT_RATE,
                    workers: int = DEFAULT_WORKERS) -> tuple[int, int]:
    """상세를 **동시에** 받는다. 워커마다 자기 ``net.Session``과 자기 DB 연결을 갖는다.

    수집 전체에서 동시성이 여기에만 있다. 다른 단계는 목록 21요청·트리 70요청·본문
    2,056건이라 순차로 분 단위에 끝나는데, 상세만 2만 건 × 약 3.5요청이다.

    ⚠️ **rps 는 손잡이가 아니었다.** 04번은 2/4/6/8 rps 를 재라고 하는데, 재 보니 네
       값의 총시간이 전부 같다(200건에 93~106초). 요청이 순차라 왕복 지연 0.5초가
       상한이어서 레이트리밋이 애초에 걸리지 않는다 — 실제 처리량은 어느 설정에서든
       ~1.9/s 였다. 실제로 듣는 손잡이는 **동시 연결 수**다.
    ⚠️ **6은 실측한 무릎 바로 아래다.** 동시성 1·2·4·6 은 처리량이 선형으로 붙으면서
       중앙 지연이 0.52s 로 그대로인데(11.2/s), 8 에서 처리량이 12.1/s 로 멎으면서
       중앙 지연이 0.63s·p90 0.77s 로 오른다 — 서버가 병렬로 처리하지 않고 큐에
       쌓기 시작하는 지점이다. 실패는 전 구간 0이었다.
       참고로 HTTP/1.1 브라우저가 호스트당 여는 연결이 정확히 6이다.
    ⚠️ **sqlite3 연결은 스레드를 넘지 못한다** — 워커마다 ``connect()`` 한다.
       WAL 이라 읽기는 겹치고 쓰기는 ``busy_timeout``(30초)으로 직렬화된다. 한 의안의
       트랜잭션이 밀리초 단위라 6워커에서 경합이 사실상 없고, 밀려서 실패해도 원장이
       받아 다음 패스가 회수한다. 스키마 생성은 호출자가 미리 한 번만 한다.
    """
    done = ok = 0
    lock = threading.Lock()

    def run(chunk: list[tuple[str, str]]) -> int:
        nonlocal done, ok
        db = dbm.connect(db_path)
        try:
            with net.Session(rate=rate) as s:
                for bn, bid in chunk:
                    good = collect_detail(db, s, bn, bid)
                    with lock:
                        done += 1
                        ok += good
                        if done % 500 == 0 or done == len(todo):
                            print(f"    {done:>6,d}/{len(todo):,}  성공 {ok:,}  "
                                  f"실패 {done - ok:,}", flush=True)
        finally:
            db.close()
        return 0

    # 라운드로빈으로 나눈다 — 앞뒤 의안의 무게가 달라(표결·발의자 유무) 연속 덩어리로
    # 자르면 한 워커만 늦게 끝난다.
    chunks = [todo[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, [c for c in chunks if c]))
    return ok, len(todo) - ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(dbm.DB_PATH))
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--bill-id", nargs="*", default=[], help="이 billId 만 상세 수집")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rate", type=float, default=net.DEFAULT_RATE)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="상세 동시 수집 수")
    a = ap.parse_args()

    db = dbm.connect(a.db)
    dbm.init_schema(db)
    if not a.bill_id:
        # 목록은 21요청뿐이라 순차로 둔다. 동시성이 필요한 것은 상세뿐이다.
        with net.Session(rate=a.rate) as s:
            print("의안 목록:")
            print(f"  총 {collect_list(db, s):,}행")
        if a.list_only:
            return 0
        sql, sql_args = todo_sql()
        todo = [(r[0], r[1]) for r in db.execute(sql, sql_args)]
    else:
        todo = [(db.execute("SELECT bill_no FROM bills WHERE bill_id=?", (b,)).fetchone()[0], b)
                if db.execute("SELECT 1 FROM bills WHERE bill_id=?", (b,)).fetchone()
                else (None, b) for b in a.bill_id]
        todo = [t for t in todo if t[0]]
    if a.limit:
        todo = todo[:a.limit]
    print(f"상세 수집 대상 {len(todo):,}건 (동시 {a.workers})")
    ok, bad = collect_details(a.db, todo, rate=a.rate, workers=a.workers)
    print(f"  성공 {ok:,} / 실패 {bad:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
