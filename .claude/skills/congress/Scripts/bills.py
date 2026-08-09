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

#: caption 앞머리 → bill_stages.stage.
#:
#: ⚠️ caption 텍스트로 판별한다. 클래스나 순서에 의존하지 마라 — 의안마다 있는 테이블이 다르다.
#: ⚠️ '정보이송정보'는 오타가 아니다. 화면 제목은 '정부이송정보'인데 caption 은 그렇게 적혀 있다.
STAGE_BY_CAPTION = {
    "소관위 심사정보": "소관위", "소위 심사정보": "소위", "관련위 심사정보": "관련위",
    "법사위 체계자구 심사정보": "체계자구", "본회의 심의정보": "본회의",
    "정보이송정보": "정부이송", "공포정보": "공포",
}
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
STAGE_CELLS = {
    "committeeName": "committee_name", "submitDt": "date_referred",
    "presentDt": "date_presented", "inscResultCd": "result", "announceNo": "ref_no",
    # 처리일의 이름이 단계마다 다르다: 처리일 · 의견서제시일 · 정부이송일 · 공포일자.
    # 한 행에 둘이 함께 오는 경우는 없다.
    "procDt": "date_processed", "govTransDt": "date_processed",
    "announceDt": "date_processed",
}

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
        out[cls[0]] = re.sub(r"\s+", " ", text).strip() or None
    return out


def parse_info(html: str) -> tuple[list[dict], list[dict], dict]:
    """caption 테이블 → 심사 단계 · 회의 · bills 보강값."""
    tree = HTMLParser(html)
    stages, meetings, extra = [], [], {}

    for table in tree.css("table"):
        cap = table.css_first("caption")
        if cap is None:
            continue
        head = re.split(r"\s*:", cap.text(strip=True))[0].strip()
        rows = [(tr, c) for tr in table.css("tr") if (c := cell_values(tr))]
        if not rows:
            continue

        if head == "의안접수정보":
            extra["proposal_session"] = rows[0][1].get("sessionTitle")
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

        if head not in STAGE_BY_CAPTION:
            continue
        stage = STAGE_BY_CAPTION[head]
        for _tr, c in rows:
            row = {"stage": stage, "committee_name": None, "date_referred": None,
                   "date_presented": None, "date_processed": None, "result": None,
                   "ref_no": None, "doc_url": None, "seq": 1}
            for cls, field in STAGE_CELLS.items():
                if c.get(cls) is not None:
                    row[field] = c[cls]
            if name := c.get("lawName"):
                # ⚠️ 공포법률명은 의안명과 다르다 — '…기본법안(대안)' → '…기본법'.
                #    바깥 자료와 이름으로 맞출 때 쓰는 것은 이쪽이다.
                extra["promulgated_law_name"] = name
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

    # seq — **파싱 순서가 아니라 정렬 순위다.** 근거는 db.py 의 bill_stages.seq 주석:
    # 보이는 순서대로 매기면 원천이 행을 재정렬하거나 앞에 끼워 넣는 순간 같은 사건이
    # 다른 seq 를 받아 UPSERT 가 덮어쓰기 대신 **삽입**으로 동작한다 — 에러 없이 행만 는다.
    # 관련위가 6행인 의안(2215928)이 실제로 이 규칙이 필요한 자리다.
    for stage in {s["stage"] for s in stages}:
        group = sorted((s for s in stages if s["stage"] == stage),
                       key=lambda s: (s["date_referred"] or s["date_presented"]
                                      or s["date_processed"] or "",
                                      s["committee_name"] or ""))
        for i, s in enumerate(group, 1):
            s["seq"] = i
    return stages, meetings, extra


def parse_votes(html: str) -> tuple[dict, list[dict], int]:
    """표결 요약과 의원별 표. 돌려주는 셋째 값은 **slug 없는 표의 수**다.

    ⚠️ 명단은 찬성·반대·기권 셋뿐이고 **불참 명단은 없다.** 행이 없는 것은 불참이 아니라
       "그 의원의 표를 우리가 모른다"이다. 불참을 세려면 재적·재석 숫자로 계산하라.
    ⚠️ 이름은 있는데 ``/members/`` 링크가 없는 li 가 있다(실측: 찬성 193 중 8).
       PK 가 slug 라 저장할 방법이 없으므로 **그 수를 세어 돌려준다** — 조용히 버리면
       "표결 행 수 == 찬성+반대+기권"이 영영 안 맞는데 이유를 알 수 없게 된다.
    """
    tree = HTMLParser(html)
    text = re.sub(r"\s+", " ", tree.body.text(separator=" ") if tree.body else "")
    num = lambda pat: (int(m.group(1).replace(",", "")) if (m := re.search(pat, text)) else None)
    summary = {"total_seats": num(r"재적\s*([\d,]+)"), "present": num(r"재석\s*([\d,]+)"),
               "yes": num(r"찬성\s*([\d,]+)"), "no": num(r"반대\s*([\d,]+)"),
               "abstain": num(r"기권\s*([\d,]+)")}
    if d := re.search(r"(\d{4}-\d{2}-\d{2})", text):
        summary["date_voted"] = d.group(1)

    votes, missing = [], 0
    for uid, label in (("voteAgreeList", "찬성"), ("voteDisAgreeList", "반대"),
                       ("voteAbsList", "기권")):
        ul = tree.css_first(f"#{uid}")
        if ul is None:
            continue
        for li in ul.css("li"):
            if not li.text(strip=True):
                continue
            a = li.css_first('a[href*="/members/"]')
            if a is None:
                missing += 1
                continue
            votes.append({"open_na_id": a.attributes["href"].rstrip("/").split("/")[-1],
                          "vote": label})
    return summary, list({v["open_na_id"]: v for v in votes}.values()), missing


def parse_proposers(html: str, represent: str | None) -> list[dict]:
    """발의자 명단. 대표발의는 상세 페이지 카드가 정하고 나머지는 공동발의다.

    한글명·한자명·정당을 한 번에 준다. 표결 명단은 이름만 주고 의원 상세는 1인 1요청이라,
    정당이 공짜로 오는 자리가 여기다.

    ⚠️ **앵커 *뒤*의 텍스트를 읽으면 안 된다.** 한 명이 사진 카드로 오고 값은 앵커 *안*에 있다:

        <a href=".../members/22nd/LEEHAIMIN"><div><img></div>
           <p>이해민</p><p>李海珉</p><p class="jdang">조국혁신당</p></a>

       앵커 다음 텍스트를 잡는 정규식은 ``<div>`` 앞의 공백만 집어 **이름·한자·정당이
       전부 None 으로 들어간다.** 200 이고 발의자 수도 맞아서 어디서도 안 걸린다 —
       실측에서 의원 332명 **전원**의 party 가 NULL 이었던 원인이 이것이다.
    """
    out = []
    for a in HTMLParser(html).css('a[href*="/members/22nd/"]'):
        slug = a.attributes["href"].rstrip("/").split("/")[-1]
        party = next((p.text(strip=True) for p in a.css("p.jdang")), None)
        plain = [t for p in a.css("p") if (t := p.text(strip=True)) and t != party]
        out.append({"open_na_id": slug,
                    "role": "대표발의" if slug == represent else "공동발의",
                    "name": plain[0] if plain else None,
                    "name_hanja": plain[1] if len(plain) > 1 else None,
                    "party": party})
    return list({p["open_na_id"]: p for p in out}.values())


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
        stages, meetings, extra = parse_info(info)
    except Exception as e:
        dbm.record_failure(db, "bill_detail", bill_no, detail=f"parse:{type(e).__name__}: {e}")
        return False

    extra["bill_kind"] = f.get("billKindCd") or None
    extra["is_reexamination"] = 1 if f.get("reexamYn") == "Y" else 0
    extra["withdraw_count"] = int(f.get("withdrawCnt") or 0)
    extra["alt_bill_id"] = f.get("selRefBillId") or None
    extra["head_memo"] = f.get("headMemoInfo") or None
    try:
        s = session.get(f"{SUMMARY}?billId={bill_id}")
        if s.status_code == 200:
            txt = HTMLParser(s.text).body
            extra["reason_text"] = txt.text(separator="\n", strip=True) if txt else None
    except Exception:
        pass   # 제안이유는 없을 수 있다. 판정자로 쓰지 않으므로 실패해도 진행한다

    votes = proposers = None
    vote_missing = 0
    if any(st["stage"] == "본회의" for st in stages):
        try:
            vs, vlist, vote_missing = parse_votes(
                session.xhr(VOTE, {"billId": bill_id, "_csrf": session.likms_csrf()},
                            referer=net.LIKMS_SEARCH_PAGE).text)
            votes = (vs, vlist)
        except Exception as e:
            dbm.record_failure(db, "bill_vote", bill_no, detail=f"parse:{type(e).__name__}")
    if f.get("billKindCd") == "법률안" and d["represent"]:
        try:
            proposers = parse_proposers(
                session.xhr(PROPOSER, {"billId": bill_id, "billNo": bill_no,
                                       "_csrf": session.likms_csrf()},
                            referer=net.LIKMS_SEARCH_PAGE).text, d["represent"])
        except Exception as e:
            dbm.record_failure(db, "bill_proposer", bill_no, detail=f"parse:{type(e).__name__}")

    db.execute("BEGIN")
    try:
        sets = ", ".join(f"{k} = ?" for k in extra)
        db.execute(f"UPDATE bills SET {sets} WHERE bill_no = ?", [*extra.values(), bill_no])
        for st in stages:
            if st["committee_name"]:
                # 돌려받은 표기를 쓴다 — 원천마다 공백이 달라 다른 이름으로 넣으면 FK 위반이다
                st["committee_name"] = dbm.upsert_committee(
                    db, st["committee_name"],
                    committee_class="소위원회" if st["stage"] == "소위" else None,
                    parent=st.get("parent"))
            db.execute("""INSERT INTO bill_stages (bill_no, stage, seq, committee_name,
                              date_referred, date_presented, date_processed, result, ref_no, doc_url)
                          VALUES (?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(bill_no, stage, seq) DO UPDATE SET
                              committee_name=excluded.committee_name,
                              date_referred=excluded.date_referred,
                              date_presented=excluded.date_presented,
                              date_processed=excluded.date_processed,
                              result=excluded.result, ref_no=excluded.ref_no""",
                       (bill_no, st["stage"], st["seq"], st["committee_name"],
                        st["date_referred"], st["date_presented"], st["date_processed"],
                        st["result"], st["ref_no"], st["doc_url"]))
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
            dbm.replace_children(db, "bill_proposers", "bill_no", bill_no,
                                 [{"bill_no": bill_no, "open_na_id": p["open_na_id"],
                                   "role": p["role"]} for p in proposers],
                                 expected=len(proposers))
        # ⚠️ 본회의 단계가 있어도 표결이 없는 의안이 있다(대안반영폐기 등).
        #    그때 요약 행을 만들면 숫자가 전부 NULL인 유령 행이 남아 "표결이 있다"는
        #    조인이 그 의안을 잡는다. 실제 표결 숫자가 온 경우에만 쓴다.
        if votes and votes[0].get("yes") is not None:
            vs, vlist = votes
            db.execute("""INSERT INTO bill_vote_summary VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(bill_no) DO UPDATE SET
                              present=excluded.present, yes=excluded.yes, no=excluded.no,
                              abstain=excluded.abstain""",
                       (bill_no, vs.get("date_voted"), vs.get("total_seats"), vs.get("present"),
                        vs.get("yes"), vs.get("no"), vs.get("abstain"), None, dbm.now_str()))
            for v in vlist:
                dbm.upsert_member(db, v["open_na_id"])
            dbm.replace_children(db, "bill_votes", "bill_no", bill_no,
                                 [{"bill_no": bill_no, **v} for v in vlist],
                                 expected=len(vlist))
        db.execute("UPDATE bills SET detail_collected_at = ? WHERE bill_no = ?",
                   (dbm.now_str(), bill_no))
        db.execute("COMMIT")
    except Exception as e:
        db.execute("ROLLBACK")
        dbm.record_failure(db, "bill_detail", bill_no, detail=f"write:{type(e).__name__}: {e}")
        return False

    dbm.clear_failure(db, "bill_detail", bill_no)
    if vote_missing:
        print(f"    ⚠️ {bill_no}: slug 없는 표 {vote_missing}건 (저장 불가)")
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
        todo = [(r[0], r[1]) for r in db.execute(
            "SELECT bill_no, bill_id FROM bills WHERE detail_collected_at IS NULL "
            "AND (bill_kind = '법률안' OR bill_kind = '미상')")]
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
