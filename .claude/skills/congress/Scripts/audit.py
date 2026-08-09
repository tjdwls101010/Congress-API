#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""불변조건 — 성공은 종료 코드가 아니다.

각 항목은 셋 중 하나다: ``pass`` · ``fail`` · ``skip``.

``skip``이 따로 있는 이유가 이 파일의 핵심이다. **판정에 실패한 것과 판정할 근거가 없는
것은 다르다.** 둘을 섞으면 근거 없는 항목이 영구히 빨간불이 되고, 그러면 사람이 곧 전체를
무시한다 — 늘 빨간 신호는 신호가 아니다.

    uv run audit.py --db …            사람이 읽는 표
    uv run audit.py --db … --json     collect.py 가 stdout 으로 내는 형식
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as dbm          # noqa: E402

SEED = Path(__file__).resolve().parent.parent / "members_seed.json"
NUM7 = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9]"

#: 게이트 — 하나라도 fail 이면 실행이 실패다.
#:
#: ⚠️ **게이트는 "우리 수집이 틀렸다"만 담는다.** 원천이 보장하지 않는 것을 여기 두면
#:    고칠 수 없는 빨간불이 되고, 그러면 사람이 표 전체를 무시하게 된다.
#:    실측으로 둘을 여기서 내려보냈다:
#:      promulgated_without_number  원천이 공포번호 칸을 비워 둔 의안이 실제로 있다
#:                                  (2214602·2215132·2218525 — 공포일자만 있고 번호가 빈 <span>)
#:      sitting_gaps                차수 매김 규칙이 종류마다 달라 오탐이 나고, 남는 것도
#:                                  "원천이 공개하지 않는 회의"와 구분되지 않는다.
#:                                  회의 완결성의 증명은 meeting_bodies_missing 쪽이다 —
#:                                  열거한 id 를 전부 받았는가.
#:      dangling_alt_bills          selRefBillId 가 번호 없는 위원회 대안 초안(billNo='DD20811')을
#:                                  가리킨다. 그 문서는 원천의 의안검색에도 없다.
GATES: dict[str, tuple[str, str]] = {
    "bill_no_gaps": ("의안번호에 구멍이 없다", f"""
        WITH n AS (SELECT CAST(bill_no AS INTEGER) no FROM bills
                   WHERE assembly_unit=22 AND bill_no GLOB '{NUM7}')
        -- COALESCE 가 없으면 빈 테이블에서 0 이 아니라 NULL 이 나와 판정이 무너진다
        SELECT COALESCE((SELECT MAX(no)-MIN(no)+1 FROM n),0) - (SELECT COUNT(*) FROM n)"""),
    "bill_detail_missing": ("법률안 중 상세 미수집이 없다 (실패 원장 제외)", """
        SELECT COUNT(*) FROM bills b
        WHERE b.bill_kind='법률안' AND b.detail_collected_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM collect_failures f
                          WHERE f.target_kind='bill_detail' AND f.target_key=b.bill_no)"""),
    "meeting_bodies_missing": ("수집 범위 회의 중 본문 미수집이 없다", """
        SELECT COUNT(*) FROM meetings m
        WHERE NOT EXISTS (SELECT 1 FROM meeting_utterances u WHERE u.conference_id=m.conference_id)
          AND NOT EXISTS (SELECT 1 FROM collect_failures f
                          WHERE f.target_kind='meeting_body'
                            AND f.target_key=CAST(m.conference_id AS TEXT))"""),
    "orphan_bill_meetings": ("본회의를 뺀 bill_meetings 의 회의가 meetings 에 있다 (실패 원장 제외)", """
        SELECT COUNT(*) FROM bill_meetings bm
        WHERE bm.stage <> '본회의'
          AND NOT EXISTS (SELECT 1 FROM meetings m WHERE m.conference_id=bm.conference_id)
          -- 원장에 있는 회의는 아직 못 받은 것이지 트리가 빠뜨린 것이 아니다.
          -- meeting_bodies_missing 과 같은 규칙을 쓴다 — 안 그러면 재시도 대상 한 건이
          -- 그 회의를 참조하는 의안 수만큼 이 게이트를 빨갛게 만든다(실측: 1건 → 20).
          AND NOT EXISTS (SELECT 1 FROM collect_failures f
                          WHERE f.target_kind='meeting_body'
                            AND f.target_key=CAST(bm.conference_id AS TEXT))"""),
    "text_contamination": ("본문 컬럼에 페이지 부속물이 섞인 행. **0이 정상이다**", """
        -- 이 도메인의 오류는 에러가 아니라 값 안에 섞여 들어온 남의 글자로 나타난다.
        -- 실제로 나왔던 것들: 목록 배지('계류의안'), 표 칸 라벨('공포번호 20676'),
        -- UI 버튼('철회자 목록'), 그리고 제안이유에 통째로 들어간 <script> 소스.
        -- 마지막 것은 20,598건 **전량**이었고 이 컬럼이 키워드 검색의 주 대상이라
        -- 검색이 자바스크립트를 물고 있었다.
        SELECT (SELECT COUNT(*) FROM bills
                WHERE reason_text LIKE '%창닫기%' OR reason_text LIKE '%innerHTML%'
                   OR reason_text LIKE '%찾을 수 없습니다%'
                   OR title LIKE '%계류의안%' OR title LIKE '%처리의안%')
             + (SELECT COUNT(*) FROM bill_stages
                WHERE result LIKE '%<%>%' OR ref_no LIKE '%공포번호%'
                   OR date_processed LIKE '%일자%')
             + (SELECT COUNT(*) FROM bill_meetings WHERE meeting_name LIKE '%회의결과%')"""),
    "unresolved_retriable": ("상한 미달의 재시도 대상이 없다", f"""
        SELECT COUNT(*) FROM collect_failures
        WHERE kind='retriable' AND attempts < {dbm.MAX_ATTEMPTS}"""),
}

#: 보고값 — 등식이 아니다. 값과 추이를 본다.
REPORTS: dict[str, tuple[str, str]] = {
    "sitting_gaps": ("차수가 끊긴 자리. **우리 누락과 원천 미공개가 섞여 있다** — 게이트가 아니다", """
        -- 차수 매김이 종류마다 다르다. 한 질의로 재면 오탐이 쏟아진다:
        --   상임위·예결위  회기마다 1로 돌아간다        → 같은 회기 안에서 본다
        --   특위·국정조사  임기 전체에 걸쳐 이어진다    → 회기를 넘어 본다
        --                  (실측: 420회 1~4차 → 421회 5차 → 422회 6~11차)
        --   국정감사       회기·차수 개념이 없다        → 대상 아님
        -- 처음엔 전부 '같은 회기 안'으로 재서 70이 나왔는데 42가 회기 경계 오탐이었다.
        SELECT (SELECT COUNT(*) FROM meetings m
                WHERE m.sitting_no > 1 AND m.session_no IS NOT NULL
                  AND m.committee_class IN ('상임위원회','예산결산특별위원회')
                  AND NOT EXISTS (SELECT 1 FROM meetings p
                                  WHERE p.committee_name=m.committee_name
                                    AND p.session_no=m.session_no
                                    AND p.sitting_no=m.sitting_no-1))
             + (SELECT COUNT(*) FROM meetings m
                WHERE m.sitting_no > 1
                  AND m.committee_class IN ('특별위원회','국정조사')
                  AND NOT EXISTS (SELECT 1 FROM meetings p
                                  WHERE p.committee_name=m.committee_name
                                    AND p.sitting_no=m.sitting_no-1))"""),
    "promulgated_without_number": ("공포 단계인데 공포번호가 빈 의안. **원천이 비워 둔다** — 추이를 본다", """
        SELECT COUNT(*) FROM bill_stages s
        WHERE s.stage='공포' AND (s.ref_no IS NULL OR s.ref_no='')"""),
    "dangling_alt_bills": ("alt_bill_id 가 가리키는 의안이 목록에 없는 건수. **원천이 의안 아닌 문서를 가리킨다**", """
        -- 실측 4,397개 링크 중 241개(5.5%)가 안 풀린다. 그 대상들의 billNo 는
        -- 'DD20811' 처럼 의안번호가 아니다 — 번호가 붙기 전의 위원회 대안 초안이라
        -- 원천의 의안검색(20,598건)에도 안 나온다. 우리 목록이 빠뜨린 것이 아니다:
        -- bill_no_gaps=0 이 번호 있는 의안의 완결성을 이미 증명한다.
        -- 값이 크게 튀면 그때가 신호다.
        SELECT COUNT(*) FROM bills b WHERE b.alt_bill_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM bills t WHERE t.bill_id=b.alt_bill_id)"""),
    "bill_no_min": ("의안번호 하한 (기대 2200001)",
                    f"SELECT MIN(CAST(bill_no AS INTEGER)) FROM bills "
                    f"WHERE assembly_unit=22 AND bill_no GLOB '{NUM7}'"),
    "bills_total": ("의안 총수", "SELECT COUNT(*) FROM bills"),
    "meetings_total": ("회의 총수 (실측 기준 2,056)", "SELECT COUNT(*) FROM meetings"),
    "members_total": ("22대를 거쳐 간 인원. **단조 증가만 한다** — 줄면 이상이다",
                      "SELECT COUNT(*) FROM members"),
    "members_missing_party": ("정당이 빈 의원. 0이 아니면 그만큼 정당 집계가 틀린다",
                              "SELECT COUNT(*) FROM members WHERE party IS NULL"),
    "members_stub_only": ("이름이 아직 slug 인 의원 — 상세를 못 받았다는 뜻",
                          "SELECT COUNT(*) FROM members WHERE name = open_na_id"),
    "votes_without_slug": ("표결 요약과 저장된 표의 차이. 원천에 slug 가 없어 못 담은 표다", """
        SELECT COALESCE(SUM(s.yes+s.no+s.abstain
               - (SELECT COUNT(*) FROM bill_votes v WHERE v.bill_no=s.bill_no)), 0)
        FROM bill_vote_summary s WHERE s.yes IS NOT NULL"""),
    "gone_total": ("원천에 없는 것. **실패 기준이 아니다** — 값이 튀면 원천 구조 변경 신호",
                   "SELECT COUNT(*) FROM collect_failures WHERE kind='gone'"),
    "committee_near_dupes": ("공백·중점만 다른 위원회 쌍. 0이 아니면 눈으로 확인한다", """
        SELECT COUNT(*) FROM committees a JOIN committees b ON a.committee_name < b.committee_name
        WHERE replace(replace(replace(a.committee_name,' ',''),'·',''),'ㆍ','')
            = replace(replace(replace(b.committee_name,' ',''),'·',''),'ㆍ','')"""),
}


def run(db, seed_exists: bool) -> dict:
    out: dict[str, dict] = {}
    for key, (desc, sql) in GATES.items():
        v = db.execute(sql).fetchone()[0]
        out[key] = {"value": v, "status": "pass" if v == 0 else "fail", "desc": desc}
    for key, (desc, sql) in REPORTS.items():
        out[key] = {"value": db.execute(sql).fetchone()[0], "status": "report", "desc": desc}

    # ── 조건부: 명부 시드가 있을 때만 판정한다.
    #    시드가 없으면 우변을 만들 방법이 아예 없다(명부 API 403). fail 이 아니라 skip 이다.
    inc = db.execute("SELECT COUNT(*) FROM members WHERE is_incumbent=1").fetchone()[0]
    if seed_exists:
        roster = len(json.loads(SEED.read_text(encoding="utf-8")))
        out["incumbent_count"] = {"value": inc, "expected": roster, "desc": "현직 수 == 명부 총원",
                                  "status": "pass" if inc == roster else "fail"}
    else:
        out["incumbent_count"] = {"value": inc, "status": "skip", "desc":
                                  "명부 시드가 없어 대조할 총원이 없다 (실패가 아니다)"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(dbm.DB_PATH))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    db = dbm.connect(a.db)
    dbm.init_schema(db)
    res = run(db, SEED.exists())
    failed = [k for k, v in res.items() if v["status"] == "fail"]

    if a.json:
        print(json.dumps({"ok": not failed, "invariants": res}, ensure_ascii=False, indent=2))
    else:
        for status in ("fail", "pass", "skip", "report"):
            rows = [(k, v) for k, v in res.items() if v["status"] == status]
            if not rows:
                continue
            mark = {"fail": "✗", "pass": "✓", "skip": "–", "report": " "}[status]
            print(f"\n{status.upper()}")
            for k, v in rows:
                exp = f" (기대 {v['expected']})" if "expected" in v else ""
                print(f"  {mark} {k:26s} {str(v['value']):>10s}{exp}   {v['desc']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
