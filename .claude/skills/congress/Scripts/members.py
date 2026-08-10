#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "selectolax>=0.3"]
# ///
"""의원 — slug 하나로 `members` 행을 채우는 보강기.

이 모듈의 산출물은 "299명이 적재된 테이블"이 아니다. **명부 JSON API가 봇 차단으로
막혀 있어서**(curl·httpx·실제 크롬 전부 403) 시작 시점에 받을 수 있는 의원 목록이
없기 때문이다. 그래서 `members`는 선행 단계가 아니라 다른 수집의 부산물로 자란다 —
표결·발의자·발언에서 slug를 만날 때마다 `enrich()`를 부르고, 그것이 상세 페이지로
나머지를 채운다.

`members_seed.json`이 있으면 현직 명부를 먼저 깔아 `mona_cd`와 총원 대조를 더해 준다.
**없어도 수집은 완결된다** — 없는 것을 실패로 다루지 마라.

    uv run members.py --db /tmp/t.db --slug KIMHyun CHUNJAESOO
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as dbm          # noqa: E402
import net                # noqa: E402

SEED_PATH = Path(__file__).resolve().parent.parent / "members_seed.json"

#: 명부 시드의 필드 → members 컬럼.
SEED_MAP = {"monaCd": "mona_cd", "hgNm": "name", "hjNm": "name_hanja", "polyNm": "party",
            "origNm": "district", "electGbnNm": "elect_kind", "reeleGbnNm": "term_count",
            "sexGbnNm": "sex"}

#: 의원 상세 페이지의 <dt> 라벨 → members 컬럼. 여기 없는 항목은 버린다.
DT_MAP = {"선거구": "district", "당선횟수": "term_count", "사무실 전화": "office_phone",
          "사무실 호실": "office_room", "이메일": "email", "의원홈페이지": "homepage"}


def profile_url(slug: str, unit: int = 22) -> str:
    """`/members/22nd/{slug}`. 대수는 영문 서수이고 이름 자리는 한글이 아니라 slug다.

    ⚠️ slug의 대소문자에는 규칙이 없다 — KIMHyun · CHOIMinhee · KANGKYUNGSOOK · LEEHAIMIN.
       **이름에서 만들어낼 수 없으므로** 반드시 원천의 href나 시드의 openNaId에서 가져온다.
    """
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(unit % 10 if unit % 100 not in (11, 12, 13) else 0, "th")
    return f"{net.WWW}/members/{unit}{suffix}/{slug}"


def parse_profile(html: str) -> dict:
    """의원 상세 페이지에서 프로필을 뽑는다. **현직과 전직은 레이아웃이 다르다.**

    ⚠️ 전직 의원 페이지에는 소속위원회·연락처가 **아예 없다.** `<dl>` 항목을 못 찾았다고
       파싱 실패로 처리하면 안 된다 — NULL이 정상이다.
    ⚠️ 현직 판정은 명부와 대조하지 않는다. **페이지가 스스로 말한다** —
       전직이면 `jeonzik_name` 클래스가 붙고 현직이면 없다(실측: 전재수 4회 / 김현 0회).
    """
    tree = HTMLParser(html)
    out: dict = {"is_incumbent": 0 if "jeonzik" in html else 1, "committees": []}

    # ── 이름은 <title>에서 뽑는다. 두 레이아웃의 유일한 공통 자리다.
    #    현직 페이지에는 .name·.info_name 이 아예 없고, 전직 페이지의 .info_name 은
    #    '전직전재수 (田載秀)CHUN JAESOO1971-04-20' 처럼 '전직' 접두어와 로마자·생년월일이
    #    붙어 있어 그대로 쓰면 이름이 '전직전재수'가 된다.
    if t := tree.css_first("title"):
        if m := re.search(r"국회의원\s*[-–]\s*(.+?)\s*$", t.text(strip=True)):
            out["name"] = m.group(1)
    # 한자명은 '(田載秀)' 꼴. 로마자 표기가 아니라 한자만 받는다.
    if m := re.search(r"\(([㐀-䶿一-鿿]{2,6})\)", html):
        out["name_hanja"] = m.group(1)

    for dl in tree.css("dl"):
        for dt, dd in zip(dl.css("dt"), dl.css("dd")):
            label = dt.text(strip=True)
            value = dd.text(separator=" ", strip=True)
            if not value or label not in DT_MAP and label != "소속위원회":
                continue
            if label == "소속위원회":
                # 쉼표 구분. 한 의원이 둘 이상일 수 있다 (예: 김현 — 과방위·예결위)
                out["committees"] = [c.strip() for c in re.split(r"[,·]", value) if c.strip()]
            elif label == "당선횟수":
                # ⚠️ dd 에 당선횟수 뒤로 역대 임기 이력이 통째로 이어 붙는다.
                #    '재선(제19대, 제22대) 2024.05.30 ~ 제22대 국회의원 …'
                #    앞의 'N선(…)' 만 떼지 않으면 term_count 에 수백 자가 들어간다.
                # ⚠️ **re.match 가 아니라 re.search 다.** 이 원천의 dd 는 개행·탭으로
                #    시작해서 맨 앞 매칭이 매번 실패한다 — 그러면 else 로 떨어져
                #    value.split()[0] 이 들어가고, 실측으로 의원 162명의 당선횟수가
                #    '재선(제19대,' 처럼 잘렸다. 숫자 표기('3선')도 있어 [가-힣0-9] 다.
                m = re.search(r"([가-힣0-9]+선\s*\([^)]*\))", value)
                out["term_count"] = dbm.norm_text(m.group(1) if m else value.split()[0])
            else:
                out.setdefault(DT_MAP[label], value)

    # 정당·선거구는 전직 페이지에 <dt> 라벨 없이 요약 한 줄로만 온다
    # ('제22대 더불어민주당 부산 북구갑'). 현직도 같은 문자열이 info-set 에 있다.
    # ⚠️ strip=True 를 빼면 안 된다. 이 원천은 값 주위에 공백을 대량으로 넣어서,
    #    아래 정규식이 공백 덩어리를 값으로 집는다 — 실측에서 조국 의원의 district 가
    #    181자짜리 공백이 됐다.
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    # ⚠️ **catch-all 대안(`[가-힣]{2,10}당`)을 넣지 마라.** 정규식 대안은 문자열의 더
    #    앞에서 맞는 것이 이기므로, 정당명을 앞에 나열해도 안 막힌다 — '국회의사당'이
    #    페이지 위쪽에 있어서 의원 5명의 party 가 '의사당' 이 됐다. 새 정당이 생기면
    #    여기 이름을 더한다. 못 맞히면 NULL 이고, NULL 은 감사의 members_missing_party 가 센다.
    if m := re.search(r"(더불어민주당|국민의힘|조국혁신당|개혁신당|진보당|기본소득당|"
                      r"사회민주당|무소속)", text):
        out.setdefault("party", m.group(1))
    if "district" not in out:
        if m := re.search(r"제\d+대\s+\S+당\s+(\S+(?:\s+\S+)?)", text):
            out["district"] = dbm.norm_text(m.group(1))
    # ⚠️ 비례대표는 지역구가 **없는** 것이지 '비례대표'라는 이름의 지역구가 아니다.
    #    그대로 두면 GROUP BY district 에 전국구 버킷이 하나 생기고, "이 지역 의원"
    #    질의가 비례대표를 지역구 의원처럼 센다. elect_kind 가 이미 그 사실을 담는다.
    if out.get("district") == "비례대표":
        out["district"] = None
    return out


def enrich(db, session: net.Session, slug: str, **hints) -> bool:
    """slug 하나를 `members`에 채운다. 이미 있어도 상세를 다시 받아 갱신한다.

    ``hints``는 slug를 만난 자리에서 함께 얻은 것들이다 — 원천마다 주는 것이 다르다.
    발의자 팝업은 정당까지, 회의록 `.man`은 지역구까지, **표결 명단은 이름뿐**이다.
    그래서 표결에서만 만난 의원이 가장 비어 있고, 상세 페이지가 그 구멍을 메운다.

    돌려주는 값은 "상세까지 받았는가"다. False 여도 스텁 행은 만들어져 있어서
    FK는 깨지지 않는다 — **FK를 지키려고 데이터를 버리는 것이 가장 나쁘다.**
    """
    dbm.upsert_member(db, slug, **hints)          # 먼저 스텁. 상세가 실패해도 조인은 산다
    try:
        r = session.get(profile_url(slug))
        if r.status_code == 404:
            dbm.record_failure(db, "member", slug, kind="gone", detail="http:404")
            return False
        r.raise_for_status()
    except Exception as e:
        dbm.record_failure(db, "member", slug, detail=f"network:{type(e).__name__}")
        return False

    try:
        p = parse_profile(r.text)
    except Exception as e:
        dbm.record_failure(db, "member", slug, detail=f"parse:{type(e).__name__}")
        return False

    committees = p.pop("committees", [])
    dbm.upsert_member(db, slug, **p)
    # ⚠️ **INSERT OR IGNORE 만 하면 낡은 행이 영원히 쌓인다.** 위원회를 옮긴 의원에게
    #    옛 위원회가 그대로 남아 "이 위원회 소속 의원"이 실제보다 많아지고, 늘어난 쪽은
    #    에러가 아니라 그럴듯한 답이라 아무도 못 알아챈다. 소속은 **현재 상태**라
    #    통째로 갈아 끼운다. 상세를 받은 뒤라 목록이 비어 있으면 실제로 비어 있는 것이다.
    db.execute("BEGIN IMMEDIATE")          # 읽고 쓰는 트랜잭션은 승격 대신 처음부터 잡는다
    try:
        names = [dbm.upsert_committee(db, c) for c in committees]   # 참조되는 쪽 먼저
        dbm.replace_children_txn(db, "member_committees", "open_na_id", slug,
                                 [{"open_na_id": slug, "committee_name": n} for n in
                                  dict.fromkeys(names)])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    dbm.clear_failure(db, "member", slug)
    return True


def load_seed(db, path: Path = SEED_PATH) -> int | None:
    """명부 시드를 적재하고 총원을 돌려준다. **없으면 None이고 그것이 정상이다.**

    이 파일이 있어야만 `mona_cd`와 `incumbent_count == roster_count` 대조가 성립한다.
    없으면 그 불변조건은 `fail`이 아니라 `skip`이다 — 판정에 실패한 것과 판정할 근거가
    없는 것은 다르다.
    """
    if not path.exists():
        return None
    rows = json.loads(path.read_text(encoding="utf-8"))
    for m in rows:
        fields = {col: m.get(key) for key, col in SEED_MAP.items() if m.get(key)}
        dbm.upsert_member(db, m["openNaId"], is_incumbent=1, **fields)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(dbm.DB_PATH))
    ap.add_argument("--slug", nargs="*", default=[], help="채울 slug들")
    ap.add_argument("--rate", type=float, default=net.DEFAULT_RATE)
    a = ap.parse_args()

    db = dbm.connect(a.db)
    dbm.init_schema(db)
    with net.Session(rate=a.rate) as s:
        n = load_seed(db)
        print(f"시드: {n}명" if n else "시드 없음 (정상 — mona_cd와 총원 대조만 건너뛴다)")
        targets = a.slug or [r[0] for r in db.execute("SELECT open_na_id FROM members")]
        ok = sum(enrich(db, s, slug) for slug in targets)
        print(f"상세 수집: {ok}/{len(targets)}")

    for label, sql in (("총원", "SELECT COUNT(*) FROM members"),
                       ("현직", "SELECT COUNT(*) FROM members WHERE is_incumbent=1"),
                       ("전직", "SELECT COUNT(*) FROM members WHERE is_incumbent=0"),
                       ("정당 미상", "SELECT COUNT(*) FROM members WHERE party IS NULL"),
                       ("위원회 배정", "SELECT COUNT(*) FROM member_committees")):
        print(f"  {label:8s} {db.execute(sql).fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
