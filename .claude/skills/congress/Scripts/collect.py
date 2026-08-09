#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "selectolax>=0.3"]
# ///
"""수집 — 유일한 공개 명령. 오케스트레이션만 한다.

**기본 실행이 백필과 증분을 구분하지 않는다.** 같은 코드가 같은 판정("구멍이 있는가")을
하고 범위만 다르다. 백필 전용 경로를 따로 만들면 둘이 어긋난다.

어디서 죽어도 다음 실행이 이어받는다. 근거는 완결성 판정이 **DB 상태만 보고** 이뤄진다는
것이다 — 커서도, 진행 파일도, 재개 지점도 저장하지 않는다. 다음 실행이 "구멍이 있는가"를
다시 물으면 그게 곧 남은 일 목록이다.

    uv run collect.py                    정상 경로: 따라잡기
    uv run collect.py --audit-only       상태만 본다. **락을 잡지 않는다**
    uv run collect.py --only meetings
    uv run collect.py --retry all        상한 도달분 되살리기
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit              # noqa: E402
import bills              # noqa: E402
import db as dbm          # noqa: E402
import meetings           # noqa: E402
import members            # noqa: E402
import net                # noqa: E402

def lock_path(db_path: str) -> Path:
    """락은 **``--db`` 가 가리키는 그 파일 옆**에 둔다.

    ⚠️ 기본 경로로 고정하면 안 된다. 예약 실행은 러너 작업공간의 체크아웃에서 돌면서
       ``--db`` 로 실제 DB 를 겨누는데(워크플로 참고), 락을 작업공간 쪽에 만들면
       손으로 돌린 수집과 예약 수집이 **서로를 막지 못한 채 같은 DB 를 동시에 쓴다.**
    """
    return Path(db_path).with_suffix(".db.lock")


def acquire_lock(lock: Path) -> bool:
    """동시 실행은 하나만. 이미 돌고 있으면 **종료코드 0으로 빠진다 — 실패가 아니다.**

    매 실행은 슬롯이 아니라 따라잡기여서 다음 실행이 이번 몫까지 가져간다.
    이걸 실패로 처리하면 cron 이 매번 빨간불을 내고 곧 무시된다.
    """
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(dbm.DB_PATH))
    ap.add_argument("--audit-only", action="store_true", help="상태만 본다. 락을 잡지 않는다")
    ap.add_argument("--only", choices=["members", "bills", "meetings"], action="append")
    ap.add_argument("--retry", choices=["all"], help="상한 도달분을 되살린다")
    ap.add_argument("--rate", type=float, default=net.DEFAULT_RATE, help="초당 요청 수")
    ap.add_argument("--workers", type=int, default=bills.DEFAULT_WORKERS,
                    help="의안 상세 동시 수집 수 (실측 기본 6)")
    ap.add_argument("--limit", type=int, help="도메인마다 이 건수까지만 (시험용)")
    a = ap.parse_args()

    db = dbm.connect(a.db)
    dbm.init_schema(db)

    # --audit-only 가 락을 잡지 않는 것이 중요하다. 수집이 도는 중에도 상태를 볼 수 있어야
    # "지금 뭐 하고 있나"를 묻는 데 비용이 안 든다.
    if a.audit_only:
        print(json.dumps({"ok": not [k for k, v in audit.run(db, audit.SEED.exists()).items()
                                     if v["status"] == "fail"],
                          "invariants": audit.run(db, audit.SEED.exists())},
                         ensure_ascii=False, indent=2))
        return 0

    lock = lock_path(a.db)
    if not acquire_lock(lock):
        print(json.dumps({"ok": True, "skipped": f"이미 실행 중이다 ({lock})",
                          "invariants": {}}, ensure_ascii=False, indent=2))
        return 0
    # 진행 로그는 **stderr 로 보낸다.** stdout 은 판정 JSON 한 장을 위한 자리다 —
    # 섞어 두면 자동화가 그 JSON 을 파싱하지 못해 요약 표가 통째로 비고, 그러면
    # "로그를 직접 보라"는 상태로 돌아간다. 도메인 모듈들의 print 까지 한 번에 옮긴다.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if a.retry == "all":
                print(f"되살린 실패 {dbm.reset_retriable(db)}건")
            only = set(a.only or ["members", "bills", "meetings"])
            with net.Session(rate=a.rate) as s:
                if "members" in only:
                    n = members.load_seed(db)
                    print(f"의원 시드: {n}명" if n else "의원 시드 없음 (정상)")
                if "bills" in only:
                    print("의안 목록:")
                    bills.collect_list(db, s)
                    todo = [(r[0], r[1]) for r in db.execute(
                        "SELECT bill_no, bill_id FROM bills WHERE detail_collected_at IS NULL "
                        "AND bill_kind IN ('법률안','미상')")][:a.limit or None]
                    print(f"의안 상세 {len(todo):,}건 (동시 {a.workers})")
                    # 수집 전체에서 여기만 동시로 돈다 — 근거는 bills.collect_details 주석.
                    bills.collect_details(a.db, todo, rate=a.rate, workers=a.workers)
                if "meetings" in only:
                    print("회의록 열거:")
                    ids = meetings.enumerate_ids(s)
                    todo = [c for c in ids if not db.execute(
                        "SELECT 1 FROM meeting_utterances WHERE conference_id=? LIMIT 1",
                        (c,)).fetchone()][:a.limit or None]
                    print(f"회의록 본문 {len(todo):,}건")
                    for cid in todo:
                        meetings.collect_one(db, s, cid, meetings.CLASSES[ids[cid]])
                if "members" in only:
                    # 마지막에 돌린다 — 앞 단계가 만든 스텁을 상세로 채우기 위해서다.
                    todo = [r[0] for r in db.execute(
                        "SELECT open_na_id FROM members")][:a.limit or None]
                    print(f"의원 보강 {len(todo):,}명")
                    for slug in todo:
                        members.enrich(db, s, slug)
    finally:
        lock.unlink(missing_ok=True)

    res = audit.run(db, audit.SEED.exists())
    failed = [k for k, v in res.items() if v["status"] == "fail"]
    print(json.dumps({"ok": not failed, "invariants": res}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
