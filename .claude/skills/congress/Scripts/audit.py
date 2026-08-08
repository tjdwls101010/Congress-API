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
    "sitting_gaps": ("차수가 연속이다 (국정감사 제외 — 알려진 사각지대)", """
        SELECT COUNT(*) FROM meetings m
        WHERE m.sitting_no > 1 AND m.session_no IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM meetings p
                          WHERE p.committee_name=m.committee_name
                            AND p.session_no=m.session_no
                            AND p.sitting_no=m.sitting_no-1)"""),
    "dangling_alt_bills": ("alt_bill_id 가 가리키는 의안이 전부 있다", """
        SELECT COUNT(*) FROM bills b WHERE b.alt_bill_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM bills t WHERE t.bill_id=b.alt_bill_id)"""),
    "orphan_bill_meetings": ("본회의를 뺀 bill_meetings 의 회의가 meetings 에 있다", """
        SELECT COUNT(*) FROM bill_meetings bm
        WHERE bm.stage <> '본회의'
          AND NOT EXISTS (SELECT 1 FROM meetings m WHERE m.conference_id=bm.conference_id)"""),
    "unresolved_retriable": ("상한 미달의 재시도 대상이 없다", f"""
        SELECT COUNT(*) FROM collect_failures
        WHERE kind='retriable' AND attempts < {dbm.MAX_ATTEMPTS}"""),
    "promulgated_without_number": ("공포 단계가 있는데 공포번호가 빈 의안이 없다", """
        SELECT COUNT(*) FROM bill_stages s
        WHERE s.stage='공포' AND (s.ref_no IS NULL OR s.ref_no='')"""),
}

#: 보고값 — 등식이 아니다. 값과 추이를 본다.
REPORTS: dict[str, tuple[str, str]] = {
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
