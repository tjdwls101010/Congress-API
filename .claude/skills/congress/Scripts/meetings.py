#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "selectolax>=0.3"]
# ///
"""회의록 — 트리에서 id를 열거하고 본문에서 메타·화자·발언·안건을 뽑는다.

22대 전체가 **2,056건**이고 열거는 70요청·30초면 끝난다(실측). 본문까지 받아도
수십 분이라 의안 상세(2만 건·수 시간)보다 훨씬 싸다 — 그래서 이 도메인을 먼저 완주해
트랜잭션 경계·스텁·실패 원장·감사가 다 도는 것을 확인한 뒤 의안 상세로 간다.

    uv run meetings.py --db /tmp/t.db --enumerate-only
    uv run meetings.py --db /tmp/t.db --id 56297 57051 51889
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as dbm          # noqa: E402
import net                # noqa: E402

TREE = f"{net.RECORD}/assembly/mnts/total/22.do"
CMIT = f"{net.RECORD}/assembly/mnts/async/cmit.do"
BODY = f"{net.RECORD}/assembly/viewer/minutes/xml.do"

#: 트리의 class_id_sch → meetings.committee_class.
#:
#: ⚠️ **이 매핑이 committee_class 의 유일한 출처다.** 회의록 본문 어디에도 상임위/특위/
#:    예결위를 가르는 문자열이 없다. 그래서 열거 단계에서 id 와 class 를 쌍으로 들고
#:    다녀야 하고, 여기서 버리면 본문 적재 때 NOT NULL 컬럼을 채우지 못해 트리를 다시 훑어야 한다.
#:    "목록은 id 만 열거한다"는 원칙의 유일한 예외다.
CLASSES = {2: "상임위원회", 3: "특별위원회", 4: "예산결산특별위원회",
           5: "국정감사", 6: "국정조사"}

#: 실측 기준값(2026-08-09). 열거가 깨졌는지 판정하는 유일한 근거다.
BASELINE = {2: 1516, 5: 317, 3: 112, 4: 67, 6: 44}

META_RE = re.compile(
    r"제(?P<unit>\d+)대국회\s+"
    r"(?:제(?P<session>\d+)회\s*\((?P<kind>[^)]+)\)\s*제(?P<sitting>\d+)차"   # 일반형
    r"|(?P<year>\d{4})년도\s*국정감사)"                                        # 국정감사형
    # 날짜도 이름 그룹으로 받는다 — 위치 번호를 쓰면 앞의 분기 그룹이 하나만 늘어도
    # 조용히 밀려서 IndexError 나 엉뚱한 날짜가 된다.
    r"\s+(?P<committee>.+?)\s*\((?P<y>\d{4})\.(?P<m>\d{2})\.(?P<d>\d{2})\.?\)")


def enumerate_ids(session: net.Session) -> dict[int, int]:
    """모든 회의 id → class_id_sch. 다섯 종류를 전부 훑는다.

    ⚠️ **예결위(class=4)만 트리 최상위가 회기다.** 위원회 노드가 0개라 표준 경로면
       루프가 **에러 없이 0회 돌고 끝나** 담겠다고 한 회의 종류가 통째로 빈다.
    ⚠️ ``sess_sch``를 비운다. 채우면 그 회기만 오고, 회기마다 돌면 요청이 14배가 된다
       (국회운영위 45건 대 4건).
    """
    out: dict[int, int] = {}
    for cls in CLASSES:
        tree = session.get(f"{TREE}?class_id_sch={cls}&cmit_chk=all").text
        cmits = sorted(set(re.findall(r'id="cmit_[^"]*"[^>]*data-cmit="([A-Z0-9]+)"', tree)))
        # 위원회 노드가 없으면 최상위가 회기다 (예결위)
        targets = ([("", cm) for cm in cmits] if cmits
                   else [(s, "") for s in sorted(set(re.findall(r'data-sess="(\d+)"', tree)))])
        found = set()
        for sess, cmit in targets:
            r = session.xhr(CMIT, {
                "th_sch": "22", "class_id_sch": str(cls), "sess_sch": sess,
                "cmit_cd_sch": cmit, "cmit_chk": "all",
                "cmit_chk_cmit": "", "cmit_chk_sub": "", "cmit_chk_etc": "",
            }, referer=TREE)
            found |= {int(x) for x in re.findall(r"xml\.do\?id=(\d+)", r.text)}
            found |= {int(x) for x in re.findall(r'data-mnts_id="(\d+)"', r.text)}
        for cid in found:
            out.setdefault(cid, cls)
        base = BASELINE.get(cls)
        flag = "" if base is None or len(found) >= base else f"  ⚠️ 기준값 {base}보다 적다"
        print(f"  class={cls} {CLASSES[cls]:12s} 노드 {len(targets):>2d}개 → {len(found):>5,d}건{flag}")
    return out


def parse_meta(html: str) -> dict:
    """회의 메타 한 줄. **첫 번째 비어있지 않은 `<h2>`**에 다 있다.

    형식이 둘이다 — 하나로 가정하면 한 종류가 통째로 깨진다.

        제22대국회 제432회 (임시회) 제3차 과학기술정보방송통신위원회 (2026.02.25.)
        제22대국회 2024년도 국정감사 과학기술정보방송통신위원회 (2024.10.07.)

    ⚠️ **국정감사에는 회기도 차수도 없다.** ``session_no``·``sitting_no``가 NULL인 것이
       정상이고 파싱 실패가 아니다(317건). 04번의 차수 연속성 감사도 이 부류에는
       성립하지 않는다 — 알려진 사각지대다.
    ⚠️ ``<h2>``가 여럿이고 **첫 번째가 비어 있는 경우가 있다**(국정감사). 그냥 `css_first`를
       쓰면 국정감사가 전부 파싱 실패로 떨어진다.
    ⚠️ 더 나아가 **모든 ``h2``가 빈 응답도 온다.** 그래서 ``.tit`` → ``.minutes_header`` 까지
       훑는다. 같은 문장이 세 자리에 있으므로 한 자리가 비어도 회의를 잃지 않는다.
    """
    tree = HTMLParser(html)
    # ⚠️ **h2 하나에 기대면 안 된다.** 같은 회의록이 h2 를 빈 채로 오는 경우가 실재한다
    #    (실측: 54610 은 본문 .speaker 115개가 멀쩡한데 h2 만 비었다. 다시 받으면 들어 있다).
    #    한 번 실패로 끝내면 그 회의가 재시도 큐를 거쳐야 하므로, 같은 문서 안의 대체 자리를
    #    먼저 훑는다 — .tit 과 .minutes_header 에 같은 문장이 있다.
    m = line = None
    for node in [*tree.css("h2"), *tree.css(".tit"), *tree.css(".minutes_header")]:
        line = node.text(separator=" ", strip=True)
        if line and (m := META_RE.search(line)):
            break
    if not m:
        raise ValueError(f"메타 줄을 파싱하지 못했다: {(line or '')[:120]!r}")
    g = m.groupdict()
    committee = g["committee"].strip()

    # ── 소위원회 판별: **이름 끝의 괄호**만 소위다.
    #    국회운영위원회(국회운영개선소위원회)                    → 소위. 끝이 ')'
    #    대법관(노경필·박영재·이숙연)임명동의에관한인사청문특별위원회 → 소위 아님. 괄호가 이름 한가운데다
    #    ⚠️ "괄호가 있으면 소위"로 만들면 두 번째가 쪼개져 없는 위원회가 committees에
    #       자동 등록되고, FK는 UPSERT로 들어온 값을 막아 주지 않아 아무도 알아채지 못한다.
    parent = None
    if (sub := re.fullmatch(r"(.+?)\(([^()]+)\)", committee)):
        parent, committee = sub.group(1).strip(), sub.group(2).strip()

    return {
        "assembly_unit": int(g["unit"]),
        "session_no": int(g["session"]) if g["session"] else None,
        "session_kind": g["kind"],
        "sitting_no": int(g["sitting"]) if g["sitting"] else None,
        "committee_name": committee,
        "parent_committee": parent,
        "is_subcommittee": 1 if parent else 0,
        "date_meeting": f"{g['y']}-{g['m']}-{g['d']}",
    }


def parse_body(html: str) -> tuple[list[dict], list[dict], list[dict]]:
    """화자 · 발언 · 안건.

    ⚠️ 의원 판별은 ``.man`` 안에 ``/members/`` 링크가 있는지로만 한다. **직위 문자열로
       분류하지 마라** — 종류가 열려 있고 회의마다 새로 생긴다(실측 105블록 중 44개가
       차관·청장·수석전문위원 같은 비의원).
    ⚠️ ``speaker_no``는 원천에 없는 값이고 우리가 (이름, 직위)를 중복 제거해 붙인다.
       그래서 회의 밖에서 참조하면 안 된다 — 재파싱하면 달라질 수 있다.
    """
    tree = HTMLParser(html)
    speakers: dict[tuple, int] = {}
    speaker_rows: list[dict] = []
    utterances: list[dict] = []

    for seq, blk in enumerate(tree.css(".speaker"), start=1):
        man = blk.css_first(".man")
        if man is None:
            continue
        link = man.css_first('a[href*="/members/"]')
        slug = link.attributes.get("href", "").rstrip("/").split("/")[-1] if link else None
        name = (n.text(strip=True) if (n := man.css_first(".name")) else man.text(strip=True))
        pos = (p.text(strip=True) if (p := man.css_first(".position")) else None)

        key = (slug, name, pos)
        if key not in speakers:
            speakers[key] = len(speakers) + 1
            speaker_rows.append({"speaker_no": speakers[key], "open_na_id": slug,
                                 "display_name": name, "position": pos})
        # 여러 문단이 <br>로 이어진다. 개행으로 붙여 원문 구조를 남긴다.
        paras = [s.text(strip=True) for s in blk.css("span.spk_sub")]
        text = "\n".join(p for p in paras if p) or blk.css_first(".talk").text(
            separator="\n", strip=True) if blk.css_first(".talk") else ""
        utterances.append({"seq": seq, "speaker_no": speakers[key], "text": text})

    agenda = []
    for i, n in enumerate(tree.css(".angun"), start=1):
        txt = re.sub(r"\s*상정된 안건\s*$", "", n.text(strip=True))
        # '98. 과학기술기본법 일부개정법률안(조인철 의원 대표발의)(의안번호 2209297)'
        m = re.match(r"\s*(\d+)\.\s*(.+)", txt)
        seq_no, title = (int(m.group(1)), m.group(2)) if m else (i, txt)
        bill_no = (b.group(1) if (b := re.search(r"의안번호\s*(\d{7}|ZZ\d+)", txt)) else None)
        agenda.append({"agenda_seq": seq_no, "title": title, "bill_no": bill_no})
    return speaker_rows, utterances, agenda


#: 문서가 스스로 밝히는 자기 id. 본문의 PDF/XML 링크에 들어 있다.
SELF_ID_RE = re.compile(r"(?:pdf|xml)\.do\?id=(\d+)")


def fetch_body(session: net.Session, conference_id: int, tries: int = 4) -> str:
    """회의록 본문. **받은 문서가 요청한 회의가 맞는지 검증한다.**

    ⚠️ **같은 URL이 요청마다 다른 회의를 준다.** 실측(2026-08-09): ``id=56238``을
       연속으로 네 번 받았더니 한 번은 ``56196``의 본문이 왔고, 다른 시각에는
       ``제20대국회 … 국토교통위원회(2017)``가 왔다. 원천의 캐시 계층 문제로 보인다.

       대수 판정만으로는 못 막는다 — **틀린 문서도 22대일 수 있고**(56238 → 56196),
       그러면 그 회의의 위원회·회기·날짜·발언이 통째로 다른 회의 것으로 저장된다.
       에러가 없고 행 수도 정상이라 감사에도 안 걸린다. 이 프로젝트에서 조용히 틀릴 수
       있는 가장 위험한 자리다.

       그래서 **문서가 스스로 밝히는 id와 대조한다.** 본문의 PDF/XML 링크가 그 값이다.
    """
    last = ""
    for _ in range(tries):
        r = session.get(f"{BODY}?id={conference_id}&type=view")
        if r.status_code == 404:
            raise FileNotFoundError(f"http:404 id={conference_id}")
        r.raise_for_status()
        last = r.text
        found = set(SELF_ID_RE.findall(last))
        if str(conference_id) in found:
            return last
        # 자기 id를 아예 안 밝히는 문서는 대조할 근거가 없으니 통과시킨다 —
        # 잘못된 문서라고 단정할 수도 없다.
        if not found:
            return last
    raise ValueError(f"다른 회의의 문서가 왔다 (요청 {conference_id} / 문서 "
                     f"{sorted(set(SELF_ID_RE.findall(last)))[:3]})")


def collect_one(db, session: net.Session, conference_id: int, committee_class: str) -> bool:
    """한 회의 = 한 트랜잭션. 갈라지면 "회의는 있는데 발언이 없는" 행이 남는다."""
    try:
        html = fetch_body(session, conference_id)
    except FileNotFoundError:
        dbm.record_failure(db, "meeting_body", conference_id, kind="gone", detail="http:404")
        return False
    except ValueError as e:
        dbm.record_failure(db, "meeting_body", conference_id, detail=f"mismatch:{e}"[:200])
        return False
    except Exception as e:
        dbm.record_failure(db, "meeting_body", conference_id, detail=f"network:{type(e).__name__}")
        return False

    try:
        meta = parse_meta(html)
        speakers, utterances, agenda = parse_body(html)
        if not utterances:
            raise ValueError("발언이 0건이다 — 발언 없는 회의록은 없다")
    except Exception as e:
        dbm.record_failure(db, "meeting_body", conference_id,
                           detail=f"parse:{type(e).__name__}: {e}"[:200])
        return False

    # ⚠️ 대수가 22가 아니면 **저장하지 않는다.** 22대 트리가 준 id 인데 본문이 20·21대라는
    #    것은 둘 중 하나다 — 트리가 잘못 넣었거나, 원천이 다른 회의의 문서를 준 것이다.
    #    어느 쪽이든 그 내용을 이 conference_id 에 붙이면 안 된다.
    #
    # ⚠️ **이것을 gone 으로 분류하면 안 된다.** 실측(2026-08-09): 수집 중 21대로 읽혔던
    #    55342·55256·55396 을 나중에 다시 받으니 전부 안정적으로 22대였다. 즉 대부분
    #    일시적 플래핑이고, gone 은 재시도 큐에서 빠지므로 그 회의를 영영 잃는다.
    #    fetch_body 의 자기 id 대조가 이 부류를 못 잡는 이유는 **그 문서들이 자기 id 링크를
    #    아예 갖고 있지 않아서**다(실측 확인). 그래서 여기가 유일한 방어선이고,
    #    방어의 내용은 "버리기"가 아니라 "다음 실행에 다시 시도하기"여야 한다.
    if meta["assembly_unit"] != 22:
        dbm.record_failure(db, "meeting_body", conference_id,
                           detail=f"scope:assembly_unit={meta['assembly_unit']} "
                                  f"(원천 플래핑일 수 있다 — 다음 실행이 재시도한다)")
        return False

    db.execute("BEGIN")
    try:
        dbm.upsert_committee(db, meta["committee_name"],
                             committee_class="소위원회" if meta["is_subcommittee"] else committee_class,
                             parent=meta["parent_committee"])
        db.execute("""INSERT INTO meetings (conference_id, assembly_unit, session_no, session_kind,
                          sitting_no, committee_name, committee_class, is_subcommittee,
                          date_meeting, pdf_url, collected_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(conference_id) DO UPDATE SET
                          session_no=excluded.session_no, sitting_no=excluded.sitting_no,
                          committee_name=excluded.committee_name,
                          committee_class=excluded.committee_class,
                          is_subcommittee=excluded.is_subcommittee,
                          date_meeting=excluded.date_meeting""",
                   (conference_id, meta["assembly_unit"], meta["session_no"], meta["session_kind"],
                    meta["sitting_no"], meta["committee_name"], committee_class,
                    meta["is_subcommittee"], meta["date_meeting"],
                    f"{net.RECORD}/assembly/viewer/minutes/download/pdf.do?id={conference_id}",
                    dbm.now_str()))

        for sp in speakers:
            if sp["open_na_id"]:
                dbm.upsert_member(db, sp["open_na_id"], name=sp["display_name"])  # 참조되는 쪽 먼저
        db.execute("DELETE FROM meeting_speakers WHERE conference_id = ?", (conference_id,))
        db.executemany(
            "INSERT INTO meeting_speakers VALUES (?,?,?,?,?)",
            [(conference_id, s["speaker_no"], s["open_na_id"], s["display_name"], s["position"])
             for s in speakers])
        db.executemany("INSERT INTO meeting_utterances VALUES (?,?,?,?)",
                       [(conference_id, u["seq"], u["speaker_no"], u["text"]) for u in utterances])
        db.execute("DELETE FROM meeting_agenda WHERE conference_id = ?", (conference_id,))
        db.executemany("INSERT OR IGNORE INTO meeting_agenda VALUES (?,?,?,?)",
                       [(conference_id, a["agenda_seq"], a["title"], a["bill_no"]) for a in agenda])
        db.execute("COMMIT")
    except Exception as e:
        db.execute("ROLLBACK")
        dbm.record_failure(db, "meeting_body", conference_id, detail=f"write:{type(e).__name__}: {e}")
        return False

    dbm.clear_failure(db, "meeting_body", conference_id)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(dbm.DB_PATH))
    ap.add_argument("--id", nargs="*", type=int, default=[], help="이 회의만 받는다")
    ap.add_argument("--enumerate-only", action="store_true", help="id 열거만 하고 본문은 안 받는다")
    ap.add_argument("--limit", type=int, help="본문 수집 상한 (시험용)")
    ap.add_argument("--force", action="store_true",
                    help="이미 받은 회의도 다시 받는다 (파서를 고쳤을 때)")
    ap.add_argument("--rate", type=float, default=net.DEFAULT_RATE)
    a = ap.parse_args()

    db = dbm.connect(a.db)
    dbm.init_schema(db)
    with net.Session(rate=a.rate) as s:
        if a.id:
            targets = {i: CLASSES[2] for i in a.id}   # 낱건 시험은 class를 모르니 상임위로 둔다
        else:
            print("회의 id 열거:")
            ids = enumerate_ids(s)
            print(f"  합계 고유 {len(ids):,}건 (기준값 2,056) · 요청 {s.requests}회")
            if a.enumerate_only:
                return 0
            targets = {cid: CLASSES[cls] for cid, cls in ids.items()}

        todo = list(targets) if a.force else [c for c in targets if not db.execute(
            "SELECT 1 FROM meeting_utterances WHERE conference_id=? LIMIT 1", (c,)).fetchone()]
        if a.limit:
            todo = todo[:a.limit]
        print(f"본문 수집 대상 {len(todo):,}건")
        ok = sum(collect_one(db, s, cid, targets[cid]) for cid in todo)
        print(f"  성공 {ok:,} / 실패 {len(todo) - ok:,}")

    for label, sql in (("회의", "SELECT COUNT(*) FROM meetings"),
                       ("발언", "SELECT COUNT(*) FROM meeting_utterances"),
                       ("화자", "SELECT COUNT(*) FROM meeting_speakers"),
                       ("의원 화자", "SELECT COUNT(*) FROM meeting_speakers WHERE open_na_id IS NOT NULL"),
                       ("안건", "SELECT COUNT(*) FROM meeting_agenda"),
                       ("회기 NULL(국정감사)", "SELECT COUNT(*) FROM meetings WHERE session_no IS NULL"),
                       ("소위 회의", "SELECT COUNT(*) FROM meetings WHERE is_subcommittee=1")):
        print(f"  {label:20s} {db.execute(sql).fetchone()[0]:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
