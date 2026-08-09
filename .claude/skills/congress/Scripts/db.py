#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""CONGRESS.db — 스키마, 연결, 그리고 완결성을 떠받치는 쓰기 규율.

이 모듈이 지키는 약속은 셋이다.

**① 스키마가 곧 문서다.** `.schema` 한 번을 읽은 클로드가 추가 문서 없이 테이블 간
관계와 각 컬럼의 함정을 파악할 수 있어야 한다. 그래서 주석은 **반드시 `CREATE TABLE`의
괄호 안**에 있다 — SQLite는 문장을 파싱해 다시 쓰면서 괄호 **밖**의 주석을 버리므로,
테이블 위에 적은 설명은 `.schema`에 도달하지 못한다. `selftest`가 이걸 잠근다.

**② 수집 상태를 corpus에 두지 않는다.** `detail_status='ok'|'pending'` 같은 열거형
컬럼이 생기면 모든 조회 질의가 그 술어를 기억해야 하고, 한 번 빠뜨리면 미완성 행이
조용히 섞인다. 대신 판정자를 하나만 두고 그것이 거짓말할 수 없게 만든다 —
`bills.detail_collected_at`은 상세 트랜잭션이 커밋될 때만 값이 들어가고,
회의 본문은 `meeting_utterances`에 자식 행이 있는지로 판정한다.

**③ 부분 실패가 데이터를 지우지 못한다.** 발의자·표결처럼 목록 전체를 다시 받는
자식 테이블은 `DELETE 후 INSERT`가 자연스러워 보이지만, 파싱이 한 번 실패하면 그 의안의
발의자가 전멸한다. 그래서 자식 목록 교체는 `replace_children`만 쓰고, 그 함수는
**원천이 스스로 말한 기대 건수와 일치할 때만** 지운다.

직접 실행하면 스키마를 만들고 자체 점검을 돈다:

    uv run db.py selftest --db /tmp/t.db
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))

#: 재시도 상한. 넘으면 큐에서 빠진다 — 상한이 없으면 완결성 조건이 영영 성립하지 않아
#: 매 실행이 실패로 보이고, 그러면 사람이 곧 경고 전체를 무시한다.
MAX_ATTEMPTS = 5

#: 실제 수집 DB. selftest 는 이 경로를 거부한다 (아래 main 참조).
DB_PATH = Path(__file__).resolve().parent.parent / "CONGRESS.db"

SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ═══════════════════════════════════════════════════════════
-- 공유 차원 — 세 도메인이 함께 가리키는 것
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS committees (
    -- 위원회. **의안·회의록·의원이 전부 이 이름을 들고 있어서 차원 테이블이 필요하다.**
    --
    -- 이게 없으면 bills.committee / meetings.committee / member_committees.committee가
    -- 각자 문자열을 들고 있고 **셋이 일치한다는 보장이 아무 데도 없다.** 한쪽에 공백이나
    -- 표기 차이가 하나 생기면 조인이 조용히 0건이 되는데 잡아 줄 것이 없다.
    -- FK가 있으면 그런 값은 애초에 들어오지 못한다.
    --
    -- ⚠️ **FK 때문에 데이터를 버리지 마라.** 수집 중 모르는 위원회명을 만나면 거부하지 말고
    --    여기 먼저 자동 등록(UPSERT)한 뒤 진행한다. FK의 목적은 일관성이지 수집 차단이 아니다.
    --    대신 유사 중복(공백·중점 차이만 있는 두 행)은 감사 질의로 따로 잡는다.
    committee_name   TEXT PRIMARY KEY,       -- 원천에 적힌 정식 명칭. 약칭으로 바꾸지 마라 — 조인 키다.
                     -- ⚠️ **소위원회는 '상위 위원회 + 공백 + 소위명'이다.**
                     --    '과학기술정보방송통신위원회 정보통신방송법안심사소위원회'
                     --    소위 이름만으로 조회하면 **0건이 온다** — 그리고 0건은 에러가
                     --    아니라 그대로 답이 되어 버린다. 이름을 모르면 추측하지 말고
                     --    SELECT DISTINCT 로 확인하라.
                     -- 왜 수식하나: 소위 이름이 상위를 넘어 중복된다. '법안심사제1소위원회'는
                     --    법사위·행안위·복지위·정무위에 전부 있고(실측: 123회 중 표본 12개가
                     --    상위 4곳), '예산결산기금심사소위원회'는 상위 5곳이었다. 맨 이름을
                     --    키로 쓰면 서로 다른 위원회가 한 행으로 접혀 "이 소위 회의"가
                     --    남의 위원회 회의를 섞어 준다.
                     -- ⚠️ **표기는 먼저 본 쪽으로 모인다.** 두 원천이 같은 위원회를 다르게
                     --    적는다 — likms '기후위기 특별위원회' / record '기후위기특별위원회'.
                     --    upsert_committee 가 공백·중점을 지운 형태로 기존 행을 찾아 그
                     --    표기를 쓰고 **실제로 저장된 이름을 돌려준다.** 자식 행은 반드시
                     --    그 반환값으로 넣어라 — 넘긴 이름 그대로 넣으면 FK 위반이다.
    committee_class  TEXT,                   -- '상임위원회'|'특별위원회'|'예산결산특별위원회'|'소위원회'|…
    parent_committee TEXT REFERENCES committees(committee_name),
                     -- 소위원회면 상위 위원회. 아니면 NULL.
                     -- 회의록 본문 헤더가 '…위원회회의록 (소위원회명)' 형태로 둘을 한 번에 준다
                     -- (02번 8절). 괄호 밖이 여기, 괄호 안이 committee_name이다.
                     -- ⚠️ **자기참조 FK라 상위 위원회를 먼저 넣어야 한다.** 소위를 먼저
                     --    INSERT하면 FK 위반이다. 한 회의를 넣는 트랜잭션 안에서
                     --    상위 → 소위 순으로 UPSERT하라.
    first_seen_at    TEXT NOT NULL
                     -- ⚠️ **UPSERT가 이 값을 덮어쓰지 않게 하라.** committees에는
                     --    updated_at이 없어서(members·bills와 다르다) 이 컬럼이 유일한
                     --    시각이고, 덮어쓰면 "언제 처음 봤나"가 매 실행 오늘로 밀린다.
                     --    ON CONFLICT DO UPDATE의 SET 절에 first_seen_at을 넣지 마라 —
                     --    parent_committee·committee_class처럼 나중에 보강되는 것만 넣는다.
                     --    보강도 COALESCE로 감싼다: 이미 아는 값을 NULL로 되돌리지 않기 위해서다.
);

-- ═══════════════════════════════════════════════════════════
-- 의원
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS members (
    -- 22대 국회의원. 사퇴·의원직 상실로 떠난 사람도 행을 지우지 않는다 —
    -- 그 사람의 과거 표결·발의·발언이 전부 여기를 참조하고 있어서 지우면 이력이 끊긴다.
    open_na_id      TEXT PRIMARY KEY,
                    -- 'KIMHyun'. 의원 상세 URL의 마지막 조각이고,
                    -- **표결 명단·발의자 명단·회의록 발언에 나오는 유일한 식별자다.**
                    -- ⚠️ 대소문자 규칙이 없다: KIMHyun · CHOIMinhee · KANGKYUNGSOOK · LEEHAIMIN.
                    --    이름에서 만들어낼 수 없으므로 반드시 원천의 href/openNaId에서 가져온다.
    mona_cd         TEXT UNIQUE,
                    -- '86R9476S'. 국회 공식 의원 코드. 바깥 데이터셋과 맞출 때만 쓴다.
                    -- PK로 쓰지 않은 이유: 우리가 긁는 HTML 어디에도 이 값이 없어서
                    -- 모든 조인이 slug→mona_cd 변환을 거쳐야 하고, 변환이 실패하면 그 행을 잃는다.
                    -- ⚠️ **NOT NULL로 두면 안 된다. NULL이 정상인 행이 두 종류나 있다.**
                    --    ① 전직 의원 — 현직 명부에 없으므로 mona_cd를 얻을 원천이 아예 없다.
                    --       NOT NULL이면 04번이 지시하는 스텁 행 INSERT가 구조적으로 불가능해진다.
                    --    ② 명부 시드가 없는 상태 — 이 값의 유일한 출처인 명부 JSON API가
                    --       봇 차단으로 막혀 있다(curl·httpx·실제 크롬 전부 403, 2026-08-09 실측).
                    --       시드 파일이 없으면 전 행이 NULL이고 **그래도 정상 동작한다.**
                    -- 조인은 언제나 open_na_id로 한다. 이 컬럼이 비어도 잃는 기능이 없다.
    name            TEXT NOT NULL,           -- '김현'
                    -- ⚠️ 조인 키로 절대 쓰지 마라. 동명이인이 실재한다 —
                    --    원천 사이트에 동명이인 전용 팝업(selSameNmMemb.do)이 따로 있는 것이 증거다.
    name_hanja      TEXT,                    -- '金玄'
    party           TEXT,                    -- 최신 정당. 덮어쓴다 — 변경 이력은 두지 않는다(위 주석 참조).
                    -- ⚠️ NULL이면 조용히 샌다. `GROUP BY party`가 그 의원을 NULL 버킷으로
                    --    몰아넣어 정당별 집계에서 빠지는데 에러가 안 난다. 이전 프로젝트에서
                    --    떠난 의원 20명이 이렇게 공동발의 21만 행의 5.2%를 왜곡했다.
                    --    발의자 팝업이 정당을 주므로(`이해민 李海珉 조국혁신당`) 스텁을 만들 때
                    --    거기서 채운다. 표결 명단은 이름만 주니 그쪽에서만 만난 의원이 위험하다.
    district        TEXT,                    -- '경기 안산시을'. 비례대표는 NULL.
                    -- 정규화하지 마라. '전남광주통합특별시 순천시광양시곡성군구례군갑'처럼
                    -- 행정구역 통합이 반영된 긴 이름이 정상이다.
    elect_kind      TEXT,                    -- '지역구' | '비례대표'
    term_count      TEXT,                    -- '초선' | '재선' | …
    sex             TEXT,
    assembly_unit   INTEGER NOT NULL,        -- 22
    is_incumbent    INTEGER NOT NULL DEFAULT 1,
                    -- 판정은 **의원 상세 페이지의 `jeonzik_name` 클래스 유무**로 한다.
                    -- 전직이면 그 클래스가 붙고 현직이면 없다(실측: 전재수 4회 / 김현 0회).
                    -- 명부 API와 대조할 필요가 없다 — 그쪽은 403이라 못 쓸 수도 있다.
                    -- 손으로 관리하지 않고 매 갱신이 다시 판정한다.
                    -- ⚠️ 0인 행을 지우지 마라. 22대 중 떠난 21명의 과거 표결·발의·발언이
                    --    전부 이 행을 참조한다. 지우면 그 기록이 조인에서 통째로 사라진다.
                    --    22대를 거쳐 간 총원은 320명(현직 299 + 전직 21)이다.
    -- ⚠️ 아래 넷은 **전직 의원 페이지에 아예 없다.** NULL이 정상이고 파싱 실패가 아니다.
    office_phone    TEXT,
    office_room     TEXT,
    email           TEXT,
    homepage        TEXT,
    profile_text    TEXT,                    -- 주요약력 원문
    collected_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_members_name  ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_party ON members(party);

CREATE TABLE IF NOT EXISTS member_committees (
    -- 의원의 소속 위원회. 한 의원이 둘 이상일 수 있어(예: 과방위 + 예결위) 별도 테이블이다.
    open_na_id      TEXT NOT NULL REFERENCES members(open_na_id) ON DELETE CASCADE,
    committee_name  TEXT NOT NULL REFERENCES committees(committee_name),
    PRIMARY KEY (open_na_id, committee_name)
);

-- 정당 변경 이력 테이블은 두지 않는다.
--
-- 유일한 실제 쓰임은 "그 표결 당시 이 의원은 어느 당이었나"인데, 수집 시작 **이전**의
-- 변경은 웹사이트에 없어서 채울 수 없다. 즉 그 질문에 답하지 못하는 채로 테이블만 늘어난다.
-- members.party는 최신값으로 덮어쓴다. 나중에 이력이 정말 필요해지면 그때 만든다 —
-- 그때는 무엇이 필요한지 알고 만들게 된다.

-- ═══════════════════════════════════════════════════════════
-- 의안
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bills (
    -- 22대에 제출된 **모든 의안**. 법률안뿐 아니라 예산안·결의안·동의안·규칙안까지 들어온다.
    --
    -- 왜 전체를 담나: 의안번호가 종류 구분 없이 연속된 정수라서, 전체를 담아야
    -- "번호에 구멍이 없다 = 누락이 없다"가 성립한다. 법률안만 담으면 구멍이 생기고
    -- 그 구멍이 비법률안인지 수집 실패인지 영원히 구분할 수 없다.
    -- 목록 행은 싸다(1000행/요청, 1.9초). 비싼 것은 상세이고 그건 법률안만 받는다.
    bill_no         TEXT PRIMARY KEY,
                    -- '2214631'. '22' + 5자리 일련번호. 사람이 읽을 수 있고
                    -- 보도·보도자료와 대조되는 유일한 키라 PK로 삼았다.
                    -- **전수 실측: 2200001~2220510의 20,510건이 구멍 0·중복 0으로 연속이다.**
                    -- ⚠️ INTEGER가 아니라 TEXT인 이유: 'ZZ22124' 형식이 88건 있다
                    --    (국정조사계획서·기금운용평가보고서 등 의안번호 체계 밖의 문서).
                    --    정수로 두면 이들이 깨진다. 7자리 고정폭이라 TEXT여도 사전순 = 번호순이다.
                    --    완결성 검증은 숫자인 것만 대상으로 한다 (04번 문서의 질의 참조).
    bill_id         TEXT NOT NULL UNIQUE,
                    -- 'PRC_A2I6…' 또는 'ARC_Z2Z6…'. 상세·표결·발의자 요청에 넣는 핸들.
                    -- ⚠️ 접두어가 둘이다. 전수 실측 PRC_ 20,128건 · ARC_ 470건(정부제출 쪽).
                    --    'PRC_'만 가정하는 정규식을 쓰면 470건이 조용히 사라진다.
                    -- ⚠️ 영구키로 가정하지 마라. 국회 시스템마다 같은 의안에 다른 값을 줄 수 있다.
                    --    동일성 판단의 기준은 언제나 bill_no다.
    assembly_unit   INTEGER NOT NULL,        -- 22
    bill_kind       TEXT NOT NULL,           -- '법률안' | '예산안' | '결의안' | '동의안' | …
                    -- 상세 페이지의 billKindCd에서 온다. 의안명에서 추정하지 마라 — 원천에 값이 있다.
    title           TEXT NOT NULL,           -- 의안명
                    -- ⚠️ 목록 셀 앞에 '계류의안'/'처리의안' 배지 텍스트가 붙어 있다. 떼고 저장한다.
    proposer_kind   TEXT,                    -- '의원' | '위원장' | '정부'
    proposer_summary TEXT,                   -- '이해민의원 등 13인' 원문.
                    -- 신원은 bill_proposers가 정본이고 이건 원문 보존용이다.
    date_proposed   TEXT,                    -- 제안일자
    date_decided    TEXT,                    -- 의결일자 (목록 기준). 미의결이면 NULL.
    decision_result TEXT,                    -- 의결결과
                    -- 실제 값 집합: 원안가결 · 수정가결 · 대안반영폐기 · 수정안반영폐기 · 철회 · 폐기 · 부결
                    -- ⚠️ **`'가결'`이라는 값은 존재하지 않는다.** 통과 판정은 IN ('원안가결','수정가결').
                    -- ⚠️ **NULL은 미처리이지 부결이 아니다**(이전 프로젝트 실측 71%가 NULL, 실제 부결은 2건).
                    -- ⚠️ **이 컬럼만으로 통과를 판정하면 재의 부결 법안을 통과로 잘못 센다.**
                    --    대통령이 거부권을 쓰면 본회의가 재의에 부치는데, 그 결과가 여기 반영되지 않고
                    --    원표결 값이 그대로 남는다. 반드시 reconsideration_result를 함께 봐라(아래).
    review_status   TEXT,                    -- 심사진행상태: '접수'|'위원회심사'|…
                    -- ⚠️ 상시 변한다. 갱신 대상이고, 과거 값은 보존되지 않는다.

    -- ── 여기부터는 상세 페이지에서만 오는 것들. 법률안만 채워진다. ──────────
    committee_name  TEXT REFERENCES committees(committee_name),   -- 소관위원회
    proposal_session TEXT,                   -- 제안회기
    reason_text     TEXT,                    -- 제안이유 및 주요내용 (평문)
                    -- 키워드 검색의 주 대상. 조문 원문(HWP/PDF)은 수집하지 않는다.
                    -- ⚠️ 이 값이 NULL인 것을 '상세 미수집'의 판정자로 쓰지 마라.
                    --    제안이유가 원래 비어 있는 의안이 있을 수 있고, 그러면 그 의안은
                    --    영원히 미수집으로 잡혀 매 실행이 자기를 다시 받는다.
    alt_bill_id     TEXT,                    -- selRefBillId. 대안반영폐기 시 내용을 흡수한 법률안.
                    -- ⚠️ bills.bill_id를 가리키지만 FK를 걸지 않았다 —
                    --    수집 순서상 대안이 아직 안 들어와 있을 수 있는 전방 참조이기 때문이다.
                    --    백필이 끝나면 전부 해소된다. 미해소분은 07번 문서의 점검 질의로 센다.
    is_reexamination INTEGER,                -- reexamYn. 대통령 거부권 후 재의 대상인가.
    withdraw_count  INTEGER,                 -- withdrawCnt
    head_memo       TEXT,                    -- '비용추계요구서 제출됨.' 같은 상단 메모
    reconsideration_result TEXT,             -- 재의(대통령 거부권) 결과: '부결' | '가결' | NULL
                    -- status_memo에서 '재의를 부친 결과 ○○됨' 패턴을 뽑아 채운다.
                    -- **22대에 28건뿐이라 전수 검증이 가능하다** — 파싱 결과를 눈으로 다 확인하라.
                    -- ⚠️ 통과 판정은 이 컬럼을 **함께** 봐야 한다:
                    --     decision_result IN ('원안가결','수정가결')
                    --       AND (reconsideration_result IS NULL OR reconsideration_result = '가결')
                    --    이걸 빠뜨리면 재의에서 부결된 법을 통과로 센다(실측: 상법 2208496).
                    -- 원문은 status_memo에 남으므로 파싱이 틀려도 복구된다.
    status_memo     TEXT,                    -- billInfo.do의 <pre class="bill_memo"> 자유텍스트.
                    -- ⚠️ **재의결 결과가 오직 여기에만 있다.** 실측(상법 2208496):
                    --    '※ 2025. 4. 1. 헌법 제53조에 따라 대통령으로부터 재의요구서가 제출됨.
                    --      ※ 2025. 4. 17. 본회의에 재의를 부친 결과 부결됨.'
                    --    그런데 목록의 decision_result는 여전히 '원안가결'이다.
                    -- 캡션 붙은 테이블 밖에 있으므로 caption 매칭 파서로는 절대 안 잡힌다.
                    --    <pre class="bill_memo">를 따로 집어야 한다.
                    -- 구조화하지 않고 원문 그대로 둔다 — 문구가 정형이 아니고 종류도 열려 있다.
                    --    "정말 통과했는가"를 물을 때 이 텍스트에 '부결'이 있는지 함께 본다.
    promulgated_law_name TEXT,               -- 공포법률명. 공포된 법률안만 채워진다.
                    -- ⚠️ 의안명과 다르다. 의안명 '…기본법안(대안)' → 공포법률명 '…기본법'.
                    --    '안(대안)' 꼬리가 떨어지므로 법제처 등 바깥 자료와 이름으로 맞출 때는
                    --    반드시 이쪽을 쓴다. 공포일은 bills가 아니라 bill_stages('공포')에 있다.

    collected_at    TEXT NOT NULL,           -- 목록 행을 처음 본 시각
    updated_at      TEXT NOT NULL,           -- 목록 정보를 마지막으로 확인한 시각
    detail_collected_at TEXT
                    -- 상세를 받은 시각. **NULL = 상세 미수집**이고, 이것이 유일한 판정자다.
                    -- 상세 수집 트랜잭션(상세 + 단계 + 발의자 + 표결)이 커밋될 때만 값이 들어가므로
                    -- 값이 있다는 것은 그 전부가 들어왔다는 뜻이다.
                    -- 비법률안은 영원히 NULL이고 그게 정상이다 — bill_kind가 그 이유를 말해 준다.
);

CREATE INDEX IF NOT EXISTS idx_bills_proposed ON bills(date_proposed);
CREATE INDEX IF NOT EXISTS idx_bills_kind     ON bills(bill_kind);
CREATE INDEX IF NOT EXISTS idx_bills_status   ON bills(review_status);
CREATE INDEX IF NOT EXISTS idx_bills_title    ON bills(title);
CREATE INDEX IF NOT EXISTS idx_bills_alt      ON bills(alt_bill_id);
-- 상세 미수집 법률안을 찾는 질의가 매 실행 첫머리에 돈다.
CREATE INDEX IF NOT EXISTS idx_bills_pending  ON bills(bill_kind, detail_collected_at);

CREATE TABLE IF NOT EXISTS bill_stages (
    -- 심사 단계. **UPDATE가 아니라 INSERT로 쌓는다.**
    --
    -- 왜 상태 컬럼이 아니라 사건 행인가: 의안 상세 페이지 자체가 단계의 나열이다
    -- (소관위 → 소위 → 체계자구 → 본회의 → 정부이송 → 공포). 이걸 bills의 컬럼 몇 개로
    -- 접으면 "언제 어떻게 바뀌었나"가 사라지고, 재수집이 파괴적 연산이 된다.
    -- 행으로 두면 재수집이 멱등해진다 — 같은 단계를 다시 봐도 같은 PK라 덮어쓸 뿐이다.
    -- ⚠️ 조회할 때 **언제나 LEFT JOIN이다.** 대안·정부제출 법안은 소관위 회부 단계 자체가 없어
    --    이 테이블에 행이 아예 없거나 적다(이전 프로젝트 실측: 가결 법안의 64~67%).
    --    INNER JOIN하면 하필 법이 될 가능성이 가장 높은 법안들이 빠진다.
    bill_no         TEXT NOT NULL REFERENCES bills(bill_no) ON DELETE CASCADE,
    stage           TEXT NOT NULL
                    CHECK (stage IN ('소관위','소위','관련위','체계자구','본회의','정부이송','공포')),
                    -- '접수'는 여기 없다 — 제안일자·제안자·제안회기는 의안 자체의 속성이라
                    -- bills에 있다. 단계로 중복해 두면 두 곳이 어긋난다.
    seq             INTEGER NOT NULL DEFAULT 1,
                    -- 같은 단계가 여러 번 있을 수 있다 (소위가 둘, 관련위가 여럿).
                    -- ⚠️ **부여 규칙을 파싱 순서로 두지 마라.** 04번의 "재수집이 멱등하다"는
                    --    주장은 같은 사건이 매번 같은 seq를 받는다는 전제 위에 서 있는데,
                    --    보이는 순서대로 1,2,3을 매기면 원천이 행을 재정렬하거나 앞에
                    --    끼워 넣는 순간 같은 사건이 다른 seq를 받는다. 그러면 PK가 충돌하지
                    --    않아 UPSERT가 덮어쓰기가 아니라 **새 행 삽입**으로 동작하고,
                    --    에러 없이 행만 늘어난다.
                    -- 규칙: **(stage, committee_name, date_referred)로 정렬한 순위**를 쓴다.
                    --    날짜가 없으면 상정일 → 처리일 순으로 대체하고, 셋 다 없으면
                    --    committee_name 사전순. 전부 원천의 값이라 재파싱해도 같은 답이 나온다.
                    -- 감사: 재수집 후 bill_stages 총 행 수가 순증하면 이 규칙이 깨진 것이다.
    committee_name  TEXT REFERENCES committees(committee_name),
                    -- 소관위·소위·관련위·체계자구 단계의 위원회. 본회의·정부이송·공포는 NULL.
                    -- ⚠️ 여기를 자유 텍스트로 두면 committees와 표기가 갈라져서
                    --    "이 위원회를 거친 법안 전부"가 조용히 일부만 준다.
    date_referred   TEXT,                    -- 회부일
    date_presented  TEXT,                    -- 상정일  (본회의는 상정일)
    date_processed  TEXT,                    -- 처리일  (본회의는 의결일, 정부이송은 이송일, 공포는 공포일)
    result          TEXT,                    -- 처리결과 · 회의결과
                    -- ⚠️ 값 집합이 본회의와 위원회에서 다르다. 위원회에는 '대안가결'이 있다.
                    -- ⚠️ **대안반영폐기가 위원회 단계에만 적히는 경우가 있다**(이전 프로젝트 실측 459~487건).
                    --    본회의에 못 올라가고 소관위에서 종료된 원안이 그렇다.
                    --    "대안에 흡수된 법안"을 bills.decision_result로만 찾으면 그 500건 가까이를 놓친다.
    ref_no          TEXT,                    -- 공포번호 등 단계별 부가 식별자
    doc_url         TEXT,                    -- 검토보고서·심사보고서 등
    PRIMARY KEY (bill_no, stage, seq)
);

CREATE TABLE IF NOT EXISTS bill_proposers (
    -- 발의자. 대표발의와 공동발의를 role로 가른다.
    bill_no         TEXT NOT NULL REFERENCES bills(bill_no) ON DELETE CASCADE,
    open_na_id      TEXT NOT NULL REFERENCES members(open_na_id),
    role            TEXT NOT NULL CHECK (role IN ('대표발의','공동발의')),
                    -- ⚠️ **이 테이블만 조인하면 가결 법안의 64%가 조용히 사라진다.**
                    --    위원장 대안(실측 22대 934건)·정부제출(601건)에는 개별 의원 발의자가 없다.
                    --    그리고 하필 법이 되는 것은 대부분 위원장 대안 쪽이다.
                    --    "정당별 통과 법안 수"를 물으면 반드시 bills.proposer_kind로 그 셋을 따로 세라.
    PRIMARY KEY (bill_no, open_na_id)
                    -- 한 의안에서 한 사람이 대표이면서 공동일 수는 없으므로 role은 키가 아니다.
);

CREATE INDEX IF NOT EXISTS idx_proposers_member ON bill_proposers(open_na_id, role);

CREATE TABLE IF NOT EXISTS bill_vote_summary (
    -- 본회의 표결 요약. 의안당 최대 1행.
    bill_no         TEXT PRIMARY KEY REFERENCES bills(bill_no) ON DELETE CASCADE,
    date_voted      TEXT,
    total_seats     INTEGER,                 -- 재적
    present         INTEGER,                 -- 재석
    yes             INTEGER,                 -- 찬성
    no              INTEGER,                 -- 반대
    abstain         INTEGER,                 -- 기권
    result          TEXT,                    -- '원안가결' | '수정가결' | '부결'
    collected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bill_votes (
    -- 의원별 표결. **불참은 여기 없다.**
    --
    -- ⚠️ 행이 없다 ≠ 불참. 원천이 찬성·반대·기권 명단만 주고 불참자 명단은 주지 않는다.
    --    재적 299 / 재석 196이면 103명이 안 나온 것인데 그 103명이 누구인지는 알 수 없다.
    --    불참 수를 세려면 bill_vote_summary의 숫자로 계산하고, 명단으로 유도하지 마라.
    bill_no         TEXT NOT NULL REFERENCES bills(bill_no) ON DELETE CASCADE,
    open_na_id      TEXT NOT NULL REFERENCES members(open_na_id),
    vote            TEXT NOT NULL CHECK (vote IN ('찬성','반대','기권')),
    PRIMARY KEY (bill_no, open_na_id)
);

CREATE INDEX IF NOT EXISTS idx_votes_member ON bill_votes(open_na_id, vote);

CREATE TABLE IF NOT EXISTS bill_meetings (
    -- 의안 상세의 회의정보에서 읽은 "이 의안이 다뤄진 회의".
    -- meeting_agenda와 겹치지만 같지 않다 — 이쪽은 **본회의처럼 본문을 수집하지 않는 회의**까지
    -- 닿고, 어느 단계의 회의인지(소관위/법사위/본회의)를 알려준다.
    bill_no         TEXT NOT NULL REFERENCES bills(bill_no) ON DELETE CASCADE,
    conference_id   INTEGER NOT NULL,        -- record.assembly.go.kr의 회의록 id
    stage           TEXT,                    -- '소관위' | '체계자구' | '본회의'
    meeting_name    TEXT,                    -- 회의명
    date_meeting    TEXT,
    result          TEXT,                    -- 회의결과
    PRIMARY KEY (bill_no, conference_id)
                    -- meetings에 FK를 걸지 않는다: 본회의 회의는 수집 범위 밖이라
                    -- meetings에 행이 없을 수 있고, 그때 이 행까지 막으면 연결 정보를 잃는다.
);

CREATE INDEX IF NOT EXISTS idx_bill_meetings_conf ON bill_meetings(conference_id);

-- ═══════════════════════════════════════════════════════════
-- 회의록
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS meetings (
    -- 우리가 존재를 아는 모든 회의. 본문 수집 여부와 무관하게 행이 생긴다.
    -- 본문을 받았는지는 meeting_utterances에 행이 있는지로 판정한다 (상태 컬럼 없음).
    conference_id   INTEGER PRIMARY KEY,     -- xml.do?id= / pdf.do?id= 의 그 id
    assembly_unit   INTEGER NOT NULL,        -- 22
    session_no      INTEGER,                 -- 432 (제432회)
    session_kind    TEXT,                    -- '임시회' | '정기회'
    sitting_no      INTEGER,                 -- 3 (제3차)
    committee_name  TEXT NOT NULL REFERENCES committees(committee_name),
                    -- 소위원회 회의면 소위원회 이름이 들어온다.
                    -- 상위 위원회는 committees.parent_committee로 따라간다.
    committee_class TEXT NOT NULL
                    CHECK (committee_class IN
                      ('상임위원회','예산결산특별위원회','특별위원회','국정감사','국정조사')),
                    -- ⚠️ **유일한 출처가 회의록 트리의 class_id_sch(2,3,4,5,6)다.**
                    --    본문 헤더 어디에도 상임위/특위/예결위를 가르는 문자열이 없다(실측).
                    --    그래서 열거 단계에서 id와 class를 쌍으로 들고 와야 한다 —
                    --    "목록은 id만 열거한다"는 원칙의 유일한 예외다.
                    -- '본회의'는 여기 없다. 회의록 트리가 class 2~6만 돌아 본회의 회의는
                    --    meetings에 아예 들어오지 않는다. 의안 쪽 참조는 bill_meetings가
                    --    FK 없이 들고 있고(아래), 그래서 이 CHECK에 값을 둘 이유가 없다.
                    -- '기타'도 없다. 실측된 회의 종류는 위 다섯뿐이고, 여섯 번째 값을 열어 두면
                    --    파서가 분류에 실패한 회의를 조용히 거기 버리게 된다.
    is_subcommittee INTEGER NOT NULL DEFAULT 0,
                    -- 판정: 본문 헤더 제목 줄에서 **'회의록' 뒤에 오는 괄호**의 유무.
                    --   '국회운영위원회회의록 (국회운영개선소위원회)'          → 1
                    --   '예산결산특별위원회회의록 (2025년도제1회…조정소위원회)' → 1
                    -- ⚠️ **"괄호가 있으면 소위"로 만들면 틀린다.** 위원회명 자체에 괄호가 들어간다:
                    --    '대법관(노경필·박영재·이숙연)임명동의에관한인사청문특별위원회회의록' (id 52240)
                    --    소위 괄호는 언제나 '회의록' 뒤이므로 거기에 앵커를 잡아라.
                    --    잘못 잡으면 없는 위원회가 committees에 자동 등록되고, FK는
                    --    UPSERT로 등록된 값을 막아 주지 않으므로 아무도 알아채지 못한다.
                    -- 소위일 때 committee_name = 괄호 안, parent_committee = 괄호 밖.
    date_meeting    TEXT NOT NULL,
    pdf_url         TEXT,
    collected_at    TEXT NOT NULL
    -- ⚠️ committee_class를 뺀 나머지 메타는 **회의록 본문 헤더**에서 파싱한다.
    --    트리의 위원회명·회기·차수에 기대면 트리 구조가 바뀔 때 함께 깨진다.
    -- ⚠️ **헤더는 한 줄이 아니고, 종류마다 형식이 다르다**(02번 8절의 실측표).
    --    · 차수는 제목 줄이 아니라 <p class="num">에 있고 단위가 '차'가 아니라 '호'다('제3호')
    --    · 국정감사(class=5)만 회기도 차수도 없다 — '2024년도 국정감사 …위원회회의록'.
    --      그 부류는 session_no·sitting_no가 NULL인 것이 정상이다(317건).
    --    · 대수는 헤더에 없다. 트리를 th_sch=22로 부르므로 요청 쪽 값을 쓴다.
);

CREATE INDEX IF NOT EXISTS idx_meetings_date  ON meetings(date_meeting);
CREATE INDEX IF NOT EXISTS idx_meetings_cmt   ON meetings(committee_name, date_meeting);

CREATE TABLE IF NOT EXISTS meeting_speakers (
    -- 한 회의의 화자들. 발언 105건에 고유 화자는 20명 안팎이라 여기서 한 번만 적는다.
    --
    -- position이 members가 아니라 여기 있는 이유: 김현 의원은 그 소위에서 '소위원장'이지만
    -- 과방위 전체회의에서는 '위원'이다. 직위는 사람의 속성이 아니라 (회의 × 사람)의 속성이다.
    conference_id   INTEGER NOT NULL REFERENCES meetings(conference_id) ON DELETE CASCADE,
    speaker_no      INTEGER NOT NULL,
                    -- 그 회의 안에서 몇 번째 화자인가. **의원 식별자가 아니다.**
                    -- 원천에 없는 값이라 파싱하면서 (이름, 직위)를 중복 제거해 우리가 붙인다.
                    -- 그래서 회의 밖에서 이 번호를 참조하지 마라 — 재파싱하면 달라질 수 있고,
                    -- 회의 하나를 통째로 다시 넣는 트랜잭션 안에서만 일관성이 보장된다.
                    -- 사람과의 연결은 아래 open_na_id가 한다.
    open_na_id      TEXT REFERENCES members(open_na_id),
                    -- ⚠️ NULL이면 국회의원이 아니다 (장관·차관·청장·전문위원·증인·참고인).
                    --    판별은 회의록 HTML에서 .man 안에 /members/ 링크가 있는지로 한다.
                    --    실측: 한 회의 105블록 중 61개가 의원, 44개가 비의원.
                    --    직위 문자열로 분류하지 마라 — 종류가 열려 있고 회의마다 새로 생긴다.
    display_name    TEXT NOT NULL,           -- 회의록에 적힌 이름 그대로
    position        TEXT,                    -- '소위원장' · '과학기술정보통신부제2차관' · '수석전문위원'
    PRIMARY KEY (conference_id, speaker_no)
);

CREATE INDEX IF NOT EXISTS idx_speakers_member ON meeting_speakers(open_na_id);

CREATE TABLE IF NOT EXISTS meeting_utterances (
    -- 발언. 회의 안에서의 순서를 보존한다.
    conference_id   INTEGER NOT NULL,
    seq             INTEGER NOT NULL,        -- 회의 안에서의 발언 순서 (1부터)
    speaker_no      INTEGER NOT NULL,
    text            TEXT NOT NULL,           -- 발언 본문. 원천의 여러 문단을 개행으로 이어 붙인다.
    PRIMARY KEY (conference_id, seq),
    FOREIGN KEY (conference_id, speaker_no)
        REFERENCES meeting_speakers(conference_id, speaker_no) ON DELETE CASCADE
                    -- ⚠️ bill_no 컬럼을 여기 두지 마라.
                    --    회의에 상정된 안건은 알 수 있지만 **개별 발언이 어느 안건에 대한 것인지는
                    --    알 수 없다.** 의원마다 질의에서 다루는 순서가 다르고, 안건을 하나씩
                    --    끝내고 넘어가는 방식이 아니다. 그 컬럼은 채우는 순간 거짓이 된다.
);

CREATE INDEX IF NOT EXISTS idx_utterances_speaker ON meeting_utterances(conference_id, speaker_no);

CREATE TABLE IF NOT EXISTS meeting_agenda (
    -- 그 회의에 상정된 안건 목록. **회의 단위 사실이다.**
    conference_id   INTEGER NOT NULL REFERENCES meetings(conference_id) ON DELETE CASCADE,
    agenda_seq      INTEGER NOT NULL,        -- 회의록에 적힌 안건 번호 (1, 2, … 122)
    title           TEXT NOT NULL,
    bill_no         TEXT,
                    -- 의안이 아닌 안건(청원 심사기간 연장요구의 건 등)은 NULL.
                    -- bills FK를 걸지 않는다: 22대 밖 의안이나 아직 안 받은 의안을 가리킬 수 있다.
    PRIMARY KEY (conference_id, agenda_seq)
);

CREATE INDEX IF NOT EXISTS idx_agenda_bill ON meeting_agenda(bill_no);

-- ═══════════════════════════════════════════════════════════
-- 수집 잔여물
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS collect_failures (
    -- 받으려다 실패한 것들. **corpus 밖이다** — 조회 질의는 이 테이블을 모른다.
    -- 성공하면 행이 삭제되므로, 비어 있는 것이 정상 상태다.
    target_kind     TEXT NOT NULL
                    CHECK (target_kind IN ('bill_detail','bill_vote','bill_proposer','meeting_body','member')),
    target_key      TEXT NOT NULL,           -- bill_no · conference_id · open_na_id
    kind            TEXT NOT NULL CHECK (kind IN ('retriable','gone')),
                    -- 'gone' = 원천에 없다(404 등). 재시도하지 않는 것이 정상이다.
    detail          TEXT,                    -- 'network:ReadTimeout' · 'parse:no-caption'
                    -- 진단의 흔적이다. network:가 몰리면 전송 문제, parse:가 몰리면 DOM 변경.
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    PRIMARY KEY (target_kind, target_key)
);
"""


def now_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    # SCHEMA 안의 PRAGMA foreign_keys 는 이 연결에 적용되지 않는다 — 연결마다 켜야 한다.
    # 이걸 빠뜨리면 FK가 조용히 꺼진 채 돌고, 고아 행이 들어와도 아무 에러가 없다.
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    return db


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    # 주석만 달라진 테이블은 여기서 조용히 최신화한다. 구조가 달라진 것은 손대지 않고
    # 이름을 돌려주므로, 그건 사람이 봐야 한다.
    if structural := migrate_comments(db):
        print(f"⚠️ 구조가 달라진 테이블이 있다 (주석만이 아니다): {', '.join(structural)}\n"
              f"   IF NOT EXISTS 라 기존 DB에 적용되지 않았다. 데이터를 옮기는 마이그레이션이 필요하다.",
              file=sys.stderr)


def _structure_only(sql: str) -> str:
    """DDL에서 주석과 여분 공백을 걷어내 **구조만** 남긴다."""
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip()


def migrate_comments(db: sqlite3.Connection) -> list[str]:
    """드리프트가 **주석 차이뿐**인 테이블의 DDL 텍스트를 제자리에서 갈아 끼운다.

    돌려주는 것은 **손대지 않은** 테이블들 — 구조까지 달라져 사람이 봐야 하는 것들이다.

    왜 필요한가: DDL이 ``CREATE TABLE IF NOT EXISTS``라 기존 DB에는 주석 수정이 적용되지
    않는다. 그런데 이 프로젝트에서 **주석이 곧 문서다** — 클로드가 읽는 1차 인터페이스가
    `.schema` 출력이라 주석이 낡으면 문서가 낡는다. 그렇다고 주석 한 줄 고치는 데 수 시간짜리
    재수집을 붙이면, 곧 주석을 안 고치게 되고 그게 이 설계의 가장 큰 자산을 깎는다.

    ⚠️ **구조가 달라진 테이블은 건드리지 않는다.** 컬럼 추가·타입 변경·제약 변경은 데이터를
       옮겨야 하는 진짜 마이그레이션이고, 그걸 이 함수가 몰래 하면 조용히 데이터를 잃는다.
       주석과 공백만 다를 때에만 손댄다.
    ⚠️ ``writable_schema``는 위험한 손잡이다 — 잘못 쓰면 DB가 열리지 않는다. 그래서
       바꾸기 전에 구조가 같음을 확인하고, 바꾼 뒤 ``integrity_check``로 확인한다.
    """
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA)
    want = {r[0]: r[1] for r in
            fresh.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
    have = {r[0]: r[1] for r in
            db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}

    comment_only, structural = [], []
    for n in sorted(want):
        if n not in have or want[n] == have[n]:
            continue
        (comment_only if _structure_only(want[n]) == _structure_only(have[n])
         else structural).append(n)
    if not comment_only:
        return structural

    ver = db.execute("PRAGMA schema_version").fetchone()[0]
    db.execute("PRAGMA writable_schema = ON")
    try:
        for n in comment_only:
            db.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?", (want[n], n))
        # schema_version 을 올려야 다른 연결이 캐시된 옛 스키마를 버린다.
        db.execute(f"PRAGMA schema_version = {ver + 1}")
    finally:
        db.execute("PRAGMA writable_schema = OFF")
    if (r := db.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise RuntimeError(f"주석 최신화 뒤 integrity_check 실패: {r}")
    print(f"스키마 주석 최신화: {', '.join(comment_only)}", file=sys.stderr)
    return structural


def consolidate_committees(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """공백·중점만 다른 위원회 행들을 하나로 합치고, 합친 쌍을 돌려준다.

    ``upsert_committee``는 **새로** 갈라지는 것만 막는다 — 이미 두 행이 되어 있으면
    각자 자식을 거느린 채로 남는다. 그 상태에서 "이 위원회를 거친 것 전부"는 조용히
    절반만 준다. 감사의 ``committee_near_dupes``가 그걸 세고, 이 함수가 치운다.

    ⚠️ **자동으로 돌리지 않는다.** 데이터를 다시 쓰는 연산이라 사람이 값을 보고
       불러야 한다: ``uv run db.py consolidate --db …``
    ⚠️ 남길 표기는 **먼저 본 것**(``first_seen_at``)이다. 어느 쪽이 옳은지 판정할 근거가
       원천에 없다 — likms 는 '기후위기 특별위원회', record 는 '기후위기특별위원회'로
       적고 둘 다 그 사이트의 정식 표기다. 그래서 옳음이 아니라 **하나로 모으는 것**만
       보장한다.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for name, seen in db.execute(
            "SELECT committee_name, first_seen_at FROM committees ORDER BY first_seen_at, committee_name"):
        key = name.replace(" ", "").replace("·", "").replace("ㆍ", "")
        groups.setdefault(key, []).append((name, seen))

    merged: list[tuple[str, str]] = []
    db.execute("BEGIN")
    try:
        for members_ in groups.values():
            if len(members_) < 2:
                continue
            keep = members_[0][0]
            for drop, _ in members_[1:]:
                for table, col in (("meetings", "committee_name"),
                                   ("bill_stages", "committee_name"),
                                   ("member_committees", "committee_name"),
                                   ("committees", "parent_committee")):
                    db.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (keep, drop))
                # 버리는 쪽만 알고 있던 값은 남기는 쪽으로 옮긴다
                db.execute("""UPDATE committees SET
                                  committee_class = COALESCE(committee_class,
                                      (SELECT committee_class FROM committees WHERE committee_name=?)),
                                  parent_committee = COALESCE(parent_committee,
                                      (SELECT parent_committee FROM committees WHERE committee_name=?))
                              WHERE committee_name = ?""", (drop, drop, keep))
                db.execute("DELETE FROM committees WHERE committee_name = ?", (drop,))
                merged.append((drop, keep))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return merged


def merge_dead_member_slugs(db: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[str]]:
    """상세가 404인 slug 를 같은 이름의 살아 있는 slug 로 합친다. ``(합친 쌍, 지운 잔재)``.

    **두 호스트가 같은 의원을 다르게 로마자화한다.** record 의 회의록은
    ``/members/22nd/JUNGHYEKYUNG`` 으로 링크하는데 www 의 실제 slug 는
    ``JEONGHYEKYEONG`` 이고 앞의 것은 404다. 그러면 한 사람이 두 행이 되고,
    "그 의원의 발언"과 "그 의원의 발의 법안"이 **서로 다른 PK** 를 가리켜 조인이
    조용히 절반만 준다. 실측 3명: 정혜경 · 권향엽 · 김윤덕.

    ⚠️ **이름만으로 합치면 안 된다 — 동명이인이 실재한다.** 22대에 박지원이 둘이다
       (``PARKJIEWON`` 5선 전남 해남완도진도 / ``PARKJIWON`` 초선 전북 군산김제부안을).
       둘 다 상세가 200이다. 그래서 **죽은 slug(상세 404)만** 합치기 대상으로 본다 —
       실재하는 사람은 언제나 살아 있는 페이지를 가진다.
    ⚠️ 살아 있는 동명이 정확히 하나일 때만 합친다. 둘 이상이면 사람이 봐야 한다.

    참조가 하나도 없는 죽은 행은 잔재다 — 원천 플래핑으로 다른 대수 회의록이 들어왔던
    시절에 만들어진 21대 의원들이다. 그건 지운다.
    """
    dead = {r[0] for r in db.execute(
        "SELECT target_key FROM collect_failures WHERE target_kind='member' AND kind='gone'")}
    merged: list[tuple[str, str]] = []
    dropped: list[str] = []
    db.execute("BEGIN")
    try:
        for slug in sorted(dead):
            row = db.execute("SELECT name FROM members WHERE open_na_id=?", (slug,)).fetchone()
            if row is None:
                continue
            live = [r[0] for r in db.execute(
                "SELECT open_na_id FROM members WHERE name=? AND open_na_id<>?",
                (row[0], slug)) if r[0] not in dead]
            refs = sum(db.execute(f"SELECT COUNT(*) FROM {t} WHERE open_na_id=?",
                                  (slug,)).fetchone()[0]
                       for t in ("meeting_speakers", "bill_proposers", "bill_votes",
                                 "member_committees"))
            if len(live) == 1:
                for t in ("meeting_speakers", "bill_proposers", "bill_votes",
                          "member_committees"):
                    # OR IGNORE — 살아 있는 쪽이 이미 그 행을 갖고 있으면 죽은 쪽이 중복이다
                    db.execute(f"UPDATE OR IGNORE {t} SET open_na_id=? WHERE open_na_id=?",
                               (live[0], slug))
                    db.execute(f"DELETE FROM {t} WHERE open_na_id=?", (slug,))
                merged.append((slug, live[0]))
            elif refs or live:
                continue          # 참조가 남아 있거나 후보가 여럿 — 사람이 봐야 한다
            else:
                dropped.append(slug)
            db.execute("DELETE FROM members WHERE open_na_id=?", (slug,))
            db.execute("DELETE FROM collect_failures WHERE target_kind='member' AND target_key=?",
                       (slug,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return merged, dropped


def schema_drift(db: sqlite3.Connection) -> list[str]:
    """이 DB에 실제로 박힌 스키마가 현재 SCHEMA와 다른 테이블 이름들.

    DDL이 ``CREATE TABLE IF NOT EXISTS``라서 **기존 DB에는 변경이 조용히 적용되지 않는다.**
    그래서 스키마 주석을 고쳐도 실제 수집 DB는 옛 주석을 그대로 들고 있고, 클로드가
    `.schema`로 읽는 것은 그 옛 주석이다 — 이 프로젝트의 1차 인터페이스가 낡는데
    아무도 모르는 상태가 된다. selftest 는 새 DB에서 도니 이걸 잡지 못한다.

    그래서 ``init``이 매번 대조한다. 값이 비어 있지 않으면 마이그레이션이 필요하다는 뜻이다.
    """
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA)
    want = {r[0]: r[1] for r in
            fresh.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
    have = {r[0]: r[1] for r in
            db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
    return sorted(n for n in want if n in have and want[n] != have[n])


# ═══════════════════════════════════════════════════════════════
# 차원 UPSERT — 참조되는 쪽을 먼저 쓴다
# ═══════════════════════════════════════════════════════════════

def upsert_committee(db: sqlite3.Connection, name: str, *,
                     committee_class: str | None = None,
                     parent: str | None = None) -> str:
    """모르는 위원회명을 만나면 거부하지 말고 먼저 등록한다. **실제로 저장된 표기를 돌려준다.**

    FK의 목적은 표기가 갈라지지 않게 하는 것이지 새 값을 막는 것이 아니다.
    수집 중 이 함수를 부르지 않고 바로 INSERT하면 FK 위반으로 **그 데이터를 잃는다.**

    ⚠️ **돌려준 이름을 써서 INSERT하라.** 넘긴 이름과 다를 수 있다(아래 표기 통일).
       넘긴 이름 그대로 자식 행을 넣으면 FK 위반으로 그 행이 통째로 실패한다.
    ⚠️ ``parent``가 있으면 상위 위원회를 먼저 등록한다 — 자기참조 FK라 순서가 강제된다.
    ⚠️ ``first_seen_at``은 절대 덮어쓰지 않는다. committees에는 updated_at이 없어서
       이 컬럼이 유일한 시각이고, 덮어쓰면 "언제 처음 봤나"가 매 실행 오늘로 밀린다.
    """
    if parent:
        parent = upsert_committee(db, parent)
    # ⚠️ **두 원천이 같은 위원회를 다르게 적는다** — likms 는 '기후위기 특별위원회',
    #    record 는 '기후위기특별위원회'. 그대로 두면 committees 에 두 행이 되고,
    #    "이 위원회를 거친 것 전부"가 조용히 절반만 온다. 에러는 나지 않는다.
    #    공백·중점만 다른 기존 행이 있으면 **그 표기를 쓴다** — 먼저 본 표기가 이긴다.
    #    어느 쪽이 옳다고 판정할 근거가 원천에 없어서 한쪽으로 모으기만 한다.
    #    감사의 committee_near_dupes 가 이 규칙이 새는지 계속 지켜본다.
    if row := db.execute(
            "SELECT committee_name FROM committees WHERE "
            "replace(replace(replace(committee_name,' ',''),'·',''),'ㆍ','') = "
            "replace(replace(replace(?,' ',''),'·',''),'ㆍ','')", (name,)).fetchone():
        name = row[0]
    db.execute(
        """INSERT INTO committees (committee_name, committee_class, parent_committee, first_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(committee_name) DO UPDATE SET
               committee_class  = COALESCE(excluded.committee_class, committee_class),
               parent_committee = COALESCE(excluded.parent_committee, parent_committee)""",
        (name, committee_class, parent, now_str()))
    return name


def upsert_member(db: sqlite3.Connection, open_na_id: str, *,
                  name: str | None = None, assembly_unit: int = 22, **fields) -> None:
    """slug를 만난 자리에서 members 행을 만든다. 이미 있으면 아는 것만 보탠다.

    명부 API가 막혀 있어 시작 시점의 의원 목록이 없다. 그래서 members는 선행 단계가
    아니라 다른 수집의 부산물로 자란다 — 표결·발의자·발언에서 slug를 만날 때마다
    여기를 부르고, 그 다음 상세 페이지가 나머지를 채운다.

    ⚠️ 모든 보강은 COALESCE다. 정보가 적은 원천(표결 명단은 이름만 준다)이 나중에
       와도 이미 아는 값을 NULL로 되돌리지 않는다. 이게 없으면 수집 순서에 따라
       정당이 있다 없다 한다.
    ⚠️ 이름만 있고 정당이 NULL인 행은 에러 없이 샌다 — GROUP BY party가 NULL 버킷으로
       몰아넣기 때문이다. 그래서 감사에 members_missing_party 가 있다.
    """
    cols = {"name": name, "assembly_unit": assembly_unit, **fields}
    cols = {k: v for k, v in cols.items() if v is not None}
    # 이름조차 모르면 slug를 임시로 둔다 — name 이 NOT NULL 이라 자리는 채워야 한다.
    cols.setdefault("name", open_na_id)
    now = now_str()
    names = ["open_na_id", *cols, "collected_at", "updated_at"]
    vals = [open_na_id, *cols.values(), now, now]
    # ⚠️ name 만 COALESCE 로 두면 **임시 이름(slug)이 진짜 이름을 영영 막는다.**
    #    스텁이 name='KIMHyun' 을 심어 두면 나중에 상세가 '김현' 을 들고 와도
    #    COALESCE(excluded.name, name) 가 'KIMHyun' 을 이긴다 — 에러 없이 이름만 slug 로 남는다.
    #    그래서 들어온 값이 slug 그 자체이면 "모른다"로 취급해 기존 값을 지킨다.
    sets = ", ".join(
        f"{c} = CASE WHEN excluded.name = excluded.open_na_id THEN {c} "
        f"ELSE COALESCE(excluded.{c}, {c}) END" if c == "name"
        else f"{c} = COALESCE(excluded.{c}, {c})"
        for c in cols)
    db.execute(
        f"""INSERT INTO members ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})
            ON CONFLICT(open_na_id) DO UPDATE SET {sets}, updated_at = excluded.updated_at""",
        vals)


# ═══════════════════════════════════════════════════════════════
# 의안 — 목록 갱신이 상세를 지우지 못하게
# ═══════════════════════════════════════════════════════════════

#: 목록 요청에서 오는 컬럼. 이 밖의 것은 상세에서만 오고 목록 갱신이 건드리면 안 된다.
BILL_LIST_COLS = ("bill_id", "assembly_unit", "bill_kind", "title", "proposer_kind",
                  "proposer_summary", "date_proposed", "date_decided", "decision_result",
                  "review_status")


def upsert_bill_list(db: sqlite3.Connection, row: dict) -> None:
    """목록 행을 넣거나 갱신한다. **이미 받아 둔 상세 컬럼은 건드리지 않는다.**

    ⚠️ ``INSERT OR REPLACE``를 쓰면 안 된다. REPLACE는 행을 지우고 다시 넣으므로
       명시하지 않은 컬럼이 전부 기본값으로 돌아간다 — 애써 받은 reason_text 와
       detail_collected_at 이 조용히 사라지고, 중복 행이 안 생기니 겉보기엔 멀쩡하며,
       증상은 "매 실행마다 전량을 다시 받는다"로만 나타나 성능 문제로 오진하기 쉽다.

    ⚠️ **빈 문자열은 NULL 로 바꿔 넣는다.** 목록 표의 빈 칸은 ``''`` 로 파싱되는데,
       그대로 두면 ``decision_result IS NULL`` 이 **0건**을 돌려준다 — SKILL.md 가
       "미처리"를 판별하라고 가르치는 바로 그 질의다. 실측에서 전체 20,598건 중
       **14,712건(71%)** 이 ``''`` 였고, 그 질의는 에러 없이 빈 답을 냈다.
       "값이 없다"를 표현하는 방법은 이 DB 안에서 하나여야 한다.
    """
    cols = {k: (v if v not in ("", None) else None)
            for k in BILL_LIST_COLS if k in row for v in (row.get(k),)}
    now = now_str()
    names = ["bill_no", *cols, "collected_at", "updated_at"]
    vals = [row["bill_no"], *cols.values(), now, now]
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols)
    db.execute(
        f"""INSERT INTO bills ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})
            ON CONFLICT(bill_no) DO UPDATE SET {sets}, updated_at = excluded.updated_at""",
        vals)


# ═══════════════════════════════════════════════════════════════
# 자식 목록 교체 — 부분 실패가 데이터를 지우지 못하게
# ═══════════════════════════════════════════════════════════════

class PartialListError(RuntimeError):
    """받은 목록이 기대 건수에 못 미친다. 지우지 않고 실패로 기록해야 한다."""


def replace_children(db: sqlite3.Connection, table: str, parent_col: str, parent_val,
                     rows: list[dict], *, expected: int | None) -> int:
    """부모에 딸린 자식 목록을 통째로 교체한다. **기대 건수와 맞을 때만 지운다.**

    ``expected``는 원천이 스스로 말한 숫자다 — 발의자는 ``proposer_summary``의
    '등 13인', 표결은 ``bill_vote_summary``의 찬성+반대+기권. 그 값과 받은 행 수가
    다르면 파싱이 샌 것이므로 **기존 행을 그대로 두고** PartialListError 를 낸다.

    ⚠️ "비어 있으면 지우지 않는다"만으로는 부족하다. 13명 중 4명만 파싱된 부분 실패가
       그 조건을 통과해 나머지 9명을 지운다 — 막으려던 사고가 한 겹 얇아진 채 다시 들어온다.
    ⚠️ ``expected=None``은 "기대 건수를 모른다"이지 "검사하지 마라"가 아니다.
       그때는 **빈 목록만** 거부한다 (원천이 200에 빈 응답을 주는 일이 흔하다).
    """
    if expected is None:
        if not rows:
            raise PartialListError(f"{table}: 빈 목록이고 기대 건수를 모른다")
    elif len(rows) != expected:
        raise PartialListError(f"{table}: {len(rows)}행을 받았는데 기대는 {expected}행")

    db.execute(f"DELETE FROM {table} WHERE {parent_col} = ?", (parent_val,))
    if not rows:
        return 0
    cols = list(rows[0])
    db.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in rows])
    return len(rows)


# ═══════════════════════════════════════════════════════════════
# 실패 원장 — corpus 밖. 비어 있는 것이 정상이다
# ═══════════════════════════════════════════════════════════════

def record_failure(db: sqlite3.Connection, target_kind: str, target_key: str, *,
                   kind: str = "retriable", detail: str | None = None) -> int:
    """실패를 원장에 남기고 누적 시도 횟수를 돌려준다.

    ``detail``에 접두어를 붙여라 — ``network:``가 몰리면 전송 문제,
    ``parse:``가 몰리면 DOM 변경이나 파서 결함이다. 다음 조치가 달라진다.

    ⚠️ **예외 타입만 적지 마라.** 한 함수가 서로 다른 이유로 같은 타입을 던지면
       (예: 메타 파싱 실패와 "발언 0건"이 둘 다 ValueError) 원장이 둘을 구분하지
       못해 진단이 한 걸음 늦는다. 메시지까지 넣어라 — 실제로 그렇게 헤맸다.
    """
    db.execute(
        """INSERT INTO collect_failures (target_kind, target_key, kind, detail, attempts, last_attempt_at)
           VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(target_kind, target_key) DO UPDATE SET
               kind = excluded.kind, detail = excluded.detail,
               attempts = attempts + 1, last_attempt_at = excluded.last_attempt_at""",
        (target_kind, str(target_key), kind, detail, now_str()))
    return db.execute(
        "SELECT attempts FROM collect_failures WHERE target_kind = ? AND target_key = ?",
        (target_kind, str(target_key))).fetchone()[0]


def clear_failure(db: sqlite3.Connection, target_kind: str, target_key: str) -> None:
    """성공했으면 지운다. 원장이 비어 있는 것이 정상 상태다."""
    db.execute("DELETE FROM collect_failures WHERE target_kind = ? AND target_key = ?",
               (target_kind, str(target_key)))


def failure_queue(db: sqlite3.Connection, target_kind: str | None = None) -> list[str]:
    """재시도 대상. ``gone``과 상한 도달분은 빠진다 — 그것들은 정상 상태다."""
    sql = ("SELECT target_key FROM collect_failures "
           "WHERE kind = 'retriable' AND attempts < ?")
    args: list = [MAX_ATTEMPTS]
    if target_kind:
        sql += " AND target_kind = ?"
        args.append(target_kind)
    return [r[0] for r in db.execute(sql, args)]


def retry_queue(db: sqlite3.Connection, target_kind: str) -> set[str]:
    """다시 시도해야 할 대상 키들 — **원장이 곧 재시도 큐다.**

    ⚠️ "이미 받았는가"만으로 할 일을 정하면 안 된다. 한 번 성공한 뒤의 재수집이 실패하면
       (원천 플래핑, 락 경합, 일시적 5xx) 데이터는 남아 있으므로 "받았다"로 보이는데
       원장에는 실패가 남는다. 그러면 그 항목은 **영영 재시도되지 않고** 게이트
       ``unresolved_retriable`` 이 영구히 빨갛다 — 이 파일이 가장 경계하는 상태다.
       실측으로 그렇게 4건이 물렸다(회의 52429·52705·55411·55813).
    """
    return {r[0] for r in db.execute(
        "SELECT target_key FROM collect_failures WHERE target_kind = ? "
        "AND kind = 'retriable' AND attempts < ?", (target_kind, MAX_ATTEMPTS))}


def reset_retriable(db: sqlite3.Connection) -> int:
    """파서를 고쳤을 때 상한 도달분을 되살린다 (``collect.py --retry all``)."""
    cur = db.execute(
        "UPDATE collect_failures SET attempts = 0 WHERE kind = 'retriable' AND attempts >= ?",
        (MAX_ATTEMPTS,))
    return cur.rowcount


# ═══════════════════════════════════════════════════════════════
# selftest
# ═══════════════════════════════════════════════════════════════

#: `.schema` 출력에 반드시 살아 있어야 할 경고 키워드.
#:
#: 스키마 주석이 이 프로젝트의 1차 인터페이스인데, 주석은 코드처럼 조용히 퇴행한다.
#: 잠그는 것은 표현이 아니라 **경고의 존재**다 — 문장 전체를 대조하면 문구를 다듬을
#: 때마다 깨져서 곧 무시하게 된다.
#:
#: ⚠️ DDL 소스가 아니라 `.schema` 출력을 검사한다. SQLite가 CREATE TABLE 앞의 주석을
#:    버리므로 둘이 다르고, 소스를 보면 "주석은 있는데 클로드에게는 안 보이는" 상태를
#:    통과시키게 된다. 그게 바로 이 테스트가 막으려는 상황이다.
COMMENT_GUARDS = {
    "meeting_utterances": "안건",
    "bill_votes": "불참",
    "members": "동명이인",
    "bills": "영구키",
    "committees": "자동 등록",
    "bill_stages": "INSERT로 쌓는다",
    "meetings": "국정감사",
}


def selftest(db_path: str) -> int:
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    db = connect(p)
    init_schema(db)
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    print("congress db selftest")

    # ── 구조
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"committees", "members", "member_committees", "bills", "bill_stages",
                "bill_proposers", "bill_vote_summary", "bill_votes", "bill_meetings",
                "meetings", "meeting_speakers", "meeting_utterances", "meeting_agenda",
                "collect_failures"}
    check("테이블 14개", expected <= tables, str(sorted(expected - tables)))
    check("뷰 없음", not {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'")})
    check("FTS 없음", not any("_fts" in t for t in tables))
    check("bills에 상태 컬럼 없음",
          not ({r[1] for r in db.execute("PRAGMA table_info(bills)")} & {"detail_status", "status"}))
    check("meeting_utterances에 bill_no 없음",
          "bill_no" not in {r[1] for r in db.execute("PRAGMA table_info(meeting_utterances)")})
    check("FK가 이 연결에서 켜져 있다", db.execute("PRAGMA foreign_keys").fetchone()[0] == 1)

    # ── 주석 회귀: `.schema` 출력을 본다
    schema_sql = {r[0]: r[1] for r in
                  db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND type='table'")}
    for tab, kw in COMMENT_GUARDS.items():
        check(f"주석 생존 {tab}:{kw}", kw in schema_sql.get(tab, ""),
              "괄호 밖에 있으면 .schema 에 안 나온다")

    # ── 차원: 자동 등록과 first_seen_at 보존
    upsert_committee(db, "정보통신방송법안심사소위원회",
                     committee_class="소위원회", parent="과학기술정보방송통신위원회")
    check("소위 등록이 상위 위원회를 먼저 만든다",
          db.execute("SELECT COUNT(*) FROM committees").fetchone()[0] == 2)
    first = db.execute("SELECT first_seen_at FROM committees WHERE committee_name = ?",
                       ("과학기술정보방송통신위원회",)).fetchone()[0]
    db.execute("UPDATE committees SET first_seen_at = '2000-01-01 00:00:00' "
               "WHERE committee_name = '과학기술정보방송통신위원회'")
    upsert_committee(db, "과학기술정보방송통신위원회", committee_class="상임위원회")
    check("UPSERT가 first_seen_at을 덮어쓰지 않는다",
          db.execute("SELECT first_seen_at FROM committees WHERE committee_name = ?",
                     ("과학기술정보방송통신위원회",)).fetchone()[0] == "2000-01-01 00:00:00", first)
    check("UPSERT가 committee_class는 보강한다",
          db.execute("SELECT committee_class FROM committees WHERE committee_name = ?",
                     ("과학기술정보방송통신위원회",)).fetchone()[0] == "상임위원회")
    # 두 원천의 표기 차이를 한쪽으로 모은다 — likms '기후위기 특별위원회' / record '기후위기특별위원회'
    before = db.execute("SELECT COUNT(*) FROM committees").fetchone()[0]
    got = upsert_committee(db, "과학기술정보 방송통신위원회")          # 공백만 다르다
    check("공백만 다른 위원회가 새 행을 만들지 않는다",
          db.execute("SELECT COUNT(*) FROM committees").fetchone()[0] == before)
    check("기존 표기를 돌려준다 — 이 값으로 자식 행을 넣어야 FK가 산다",
          got == "과학기술정보방송통신위원회", got)
    check("중점(·)만 다른 것도 같은 위원회로 본다",
          upsert_committee(db, "과학기술정보·방송통신위원회") == "과학기술정보방송통신위원회")
    # ⚠️ 상위로 수식한 소위 이름은 **별개 위원회다.** 여기서 접히면 법사위 제1소위와
    #    복지위 제1소위가 한 행이 되어 조회가 조용히 남의 회의를 섞어 준다.
    a = upsert_committee(db, "법제사법위원회 법안심사제1소위원회", parent="법제사법위원회")
    b = upsert_committee(db, "보건복지위원회 법안심사제1소위원회", parent="보건복지위원회")
    check("상위가 다른 같은 이름 소위는 서로 다른 행이다", a != b, f"{a} / {b}")

    # ── 의원: 스텁 생성과 COALESCE 보강
    upsert_member(db, "LEEHAIMIN", name="이해민", party="조국혁신당")
    upsert_member(db, "LEEHAIMIN", name="이해민")          # 정보가 적은 원천이 나중에 온다
    check("적은 정보로 다시 넣어도 정당이 안 지워진다",
          db.execute("SELECT party FROM members WHERE open_na_id='LEEHAIMIN'").fetchone()[0]
          == "조국혁신당")
    # 스텁이 먼저(이름 모름) → 상세가 나중(이름 있음). 흔한 실제 순서다.
    upsert_member(db, "CHOIMinhee")                        # 표결 명단에서 slug만 봤다
    check("이름을 모르면 slug가 자리를 채운다",
          db.execute("SELECT name FROM members WHERE open_na_id='CHOIMinhee'").fetchone()[0]
          == "CHOIMinhee")
    upsert_member(db, "CHOIMinhee", name="최민희", party="더불어민주당")
    check("진짜 이름이 임시 이름(slug)을 덮는다",
          db.execute("SELECT name FROM members WHERE open_na_id='CHOIMinhee'").fetchone()[0]
          == "최민희")
    upsert_member(db, "CHOIMinhee")                        # 다시 이름 없는 원천이 와도
    check("임시 이름이 진짜 이름을 되돌리지 못한다",
          tuple(db.execute("SELECT name, party FROM members WHERE open_na_id='CHOIMinhee'")
                .fetchone()) == ("최민희", "더불어민주당"))
    upsert_member(db, "KIMHyun", name="김현", party="더불어민주당", is_incumbent=1)
    upsert_member(db, "CHUNJAESOO", name="전재수", party="더불어민주당", is_incumbent=0)
    check("mona_cd가 NULL이어도 들어간다 (전직 의원·시드 없음이 정상)",
          db.execute("SELECT COUNT(*) FROM members WHERE mona_cd IS NULL").fetchone()[0] == 4)
    upsert_member(db, "AAA", name="동명", mona_cd="X1")
    try:
        upsert_member(db, "BBB", name="동명", mona_cd="X1")
        check("mona_cd UNIQUE가 중복을 막는다", False, "통과해 버렸다")
    except sqlite3.IntegrityError:
        check("mona_cd UNIQUE가 중복을 막는다", True)
    check("동명이인이 다른 행으로 공존한다",
          db.execute("SELECT COUNT(*) FROM members WHERE name='동명'").fetchone()[0] == 1)

    # ── 의안: 목록 갱신이 상세를 지우지 않는다 (REPLACE 금지 회귀)
    upsert_bill_list(db, {"bill_no": "2214631", "bill_id": "PRC_A", "assembly_unit": 22,
                          "bill_kind": "법률안", "title": "소프트웨어 진흥법 일부개정법률안",
                          "review_status": "위원회 심사"})
    db.execute("UPDATE bills SET reason_text = '제안이유 본문', detail_collected_at = ? "
               "WHERE bill_no = '2214631'", (now_str(),))
    upsert_bill_list(db, {"bill_no": "2214631", "bill_id": "PRC_A", "assembly_unit": 22,
                          "bill_kind": "법률안", "title": "소프트웨어 진흥법 일부개정법률안",
                          "review_status": "본회의 통과"})
    r = db.execute("SELECT reason_text, detail_collected_at, review_status "
                   "FROM bills WHERE bill_no='2214631'").fetchone()
    check("목록 재갱신이 reason_text를 보존한다", r["reason_text"] == "제안이유 본문")
    check("목록 재갱신이 detail_collected_at을 보존한다", bool(r["detail_collected_at"]))
    check("목록 재갱신이 변한 상태는 반영한다", r["review_status"] == "본회의 통과")
    check("bills 행이 늘지 않았다",
          db.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1)

    # ── bill_stages: 같은 단계를 다시 봐도 행이 안 는다
    stage = dict(bill_no="2214631", stage="소관위", seq=1,
                 committee_name="과학기술정보방송통신위원회", date_referred="2025-11-28")
    for _ in range(3):
        db.execute("""INSERT INTO bill_stages (bill_no, stage, seq, committee_name, date_referred)
                      VALUES (:bill_no, :stage, :seq, :committee_name, :date_referred)
                      ON CONFLICT(bill_no, stage, seq) DO UPDATE SET
                          committee_name = excluded.committee_name,
                          date_referred  = excluded.date_referred""", stage)
    check("같은 단계를 세 번 넣어도 1행",
          db.execute("SELECT COUNT(*) FROM bill_stages").fetchone()[0] == 1)

    # ── CHECK 제약이 오타 상태값을 막는다
    for sql, label in (
            ("INSERT INTO bill_votes VALUES ('2214631','KIMHyun','찬성이')", "vote 오타 거부"),
            ("INSERT INTO bill_proposers VALUES ('2214631','KIMHyun','대표빌의')", "role 오타 거부"),
            ("INSERT INTO bill_stages (bill_no,stage) VALUES ('2214631','접수')", "stage 오타 거부"),
            ("INSERT INTO collect_failures (target_kind,target_key,kind) "
             "VALUES ('bill_detail','1','retriabel')", "kind 오타 거부")):
        try:
            db.execute(sql)
            check(f"CHECK가 {label}", False, "통과해 버렸다")
        except sqlite3.IntegrityError:
            check(f"CHECK가 {label}", True)
    # 위 거부가 정말 CHECK 때문이었는지 — 같은 키에 정상값이면 들어가야 한다
    db.execute("INSERT INTO bill_votes VALUES ('2214631','KIMHyun','찬성')")
    check("정상값은 통과한다 (앞의 거부가 FK가 아니었다)",
          db.execute("SELECT COUNT(*) FROM bill_votes").fetchone()[0] == 1)

    # ── FK: members에 없는 slug는 거부된다
    try:
        db.execute("INSERT INTO bill_votes VALUES ('2214631','NOBODY','찬성')")
        check("FK가 모르는 slug를 거부한다", False, "통과해 버렸다")
    except sqlite3.IntegrityError:
        check("FK가 모르는 slug를 거부한다", True)

    # ── meetings: committee_class CHECK 과 국정감사 NULL
    upsert_committee(db, "국회운영위원회", committee_class="상임위원회")
    db.execute("""INSERT INTO meetings (conference_id, assembly_unit, session_no, session_kind,
                      sitting_no, committee_name, committee_class, is_subcommittee,
                      date_meeting, collected_at)
                  VALUES (51889, 22, NULL, NULL, NULL, '국회운영위원회', '국정감사', 0,
                          '2024-10-07', ?)""", (now_str(),))
    check("국정감사는 회기·차수가 NULL이어도 들어간다",
          db.execute("SELECT COUNT(*) FROM meetings WHERE session_no IS NULL").fetchone()[0] == 1)
    try:
        db.execute("""INSERT INTO meetings (conference_id, assembly_unit, committee_name,
                          committee_class, date_meeting, collected_at)
                      VALUES (1, 22, '국회운영위원회', '본회의', '2026-01-01', ?)""", (now_str(),))
        check("committee_class가 '본회의'를 거부한다", False, "통과해 버렸다")
    except sqlite3.IntegrityError:
        check("committee_class가 '본회의'를 거부한다", True)

    # ── 자식 목록 교체: 부분 실패가 지우지 못한다
    db.execute("INSERT INTO bill_proposers VALUES ('2214631','LEEHAIMIN','대표발의')")
    db.execute("INSERT INTO bill_proposers VALUES ('2214631','KIMHyun','공동발의')")
    for label, rows, expected_n in (
            ("빈 목록", [], 2),
            ("부분 목록(2 기대에 1행)", [dict(bill_no="2214631", open_na_id="LEEHAIMIN",
                                              role="대표발의")], 2)):
        try:
            replace_children(db, "bill_proposers", "bill_no", "2214631", rows, expected=expected_n)
            check(f"{label}이 거부된다", False, "통과해 버렸다")
        except PartialListError:
            check(f"{label}이 거부된다", True)
    check("거부 후에도 기존 발의자가 남아 있다",
          db.execute("SELECT COUNT(*) FROM bill_proposers").fetchone()[0] == 2)
    n = replace_children(db, "bill_proposers", "bill_no", "2214631",
                         [dict(bill_no="2214631", open_na_id="LEEHAIMIN", role="대표발의")],
                         expected=1)
    check("기대 건수와 맞으면 축소가 반영된다",
          n == 1 and db.execute("SELECT COUNT(*) FROM bill_proposers").fetchone()[0] == 1)

    # ── 실패 원장: 누적·상한·되살리기·성공 시 삭제
    for _ in range(MAX_ATTEMPTS):
        att = record_failure(db, "bill_detail", "2214631", detail="network:ReadTimeout")
    check("attempts 누적", att == MAX_ATTEMPTS, str(att))
    check("상한 도달분은 재시도 큐 밖", not failure_queue(db, "bill_detail"))
    check("--retry all이 되살린다",
          reset_retriable(db) == 1 and len(failure_queue(db, "bill_detail")) == 1)
    record_failure(db, "meeting_body", "99999", kind="gone", detail="http:404")
    check("gone은 재시도 큐에 없다", failure_queue(db, "meeting_body") == [])
    clear_failure(db, "bill_detail", "2214631")
    check("성공하면 원장에서 지워진다",
          db.execute("SELECT COUNT(*) FROM collect_failures WHERE target_kind='bill_detail'")
          .fetchone()[0] == 0)

    # ── 판정자: 상세 미수집은 detail_collected_at 하나로 판정된다
    upsert_bill_list(db, {"bill_no": "2200001", "bill_id": "PRC_B", "assembly_unit": 22,
                          "bill_kind": "법률안", "title": "다른 법률안"})
    check("detail_collected_at IS NULL이 미수집을 정확히 센다",
          db.execute("SELECT COUNT(*) FROM bills WHERE bill_kind='법률안' "
                     "AND detail_collected_at IS NULL").fetchone()[0] == 1)

    # ── 멱등성: 같은 DB에 init_schema 를 다시 돌려도 터지지 않는다
    try:
        init_schema(db)
        check("init_schema 재실행이 안전하다 (IF NOT EXISTS)", True)
    except sqlite3.OperationalError as e:
        check("init_schema 재실행이 안전하다 (IF NOT EXISTS)", False, str(e))

    # ── 죽은 slug 합치기: 합치는 쪽과 **동명이인을 지키는** 쪽을 둘 다 본다
    upsert_member(db, "JEONGHYEKYEONG", name="정혜경", party="진보당")
    upsert_member(db, "JUNGHYEKYUNG", name="정혜경")        # record 가 준 404 slug
    record_failure(db, "member", "JUNGHYEKYUNG", kind="gone", detail="http:404")
    db.execute("INSERT INTO member_committees VALUES ('JUNGHYEKYUNG','과학기술정보방송통신위원회')")
    upsert_member(db, "PARKJIEWON", name="박지원", party="더불어민주당")
    upsert_member(db, "PARKJIWON", name="박지원", party="더불어민주당")   # 진짜 동명이인
    merged, _ = merge_dead_member_slugs(db)
    check("죽은 slug 를 살아 있는 동명으로 합친다",
          merged == [("JUNGHYEKYUNG", "JEONGHYEKYEONG")], str(merged))
    check("합칠 때 자식 행을 살아 있는 쪽으로 옮긴다",
          db.execute("SELECT COUNT(*) FROM member_committees WHERE open_na_id='JEONGHYEKYEONG'"
                     ).fetchone()[0] == 1)
    # ⚠️ 이름만으로 합쳤으면 여기서 두 사람이 한 사람이 된다. 실제로 22대에 박지원이 둘이다.
    check("둘 다 살아 있는 동명이인은 건드리지 않는다",
          db.execute("SELECT COUNT(*) FROM members WHERE name='박지원'").fetchone()[0] == 2)

    # ── 드리프트 감지가 실제로 작동한다
    check("정상 DB에는 드리프트가 없다", schema_drift(db) == [], str(schema_drift(db)))
    db.execute("ALTER TABLE bill_meetings RENAME TO bill_meetings_x")
    db.execute("CREATE TABLE bill_meetings (bill_no TEXT, conference_id INTEGER)")  # 주석 없는 옛 정의 흉내
    check("주석이 사라진 테이블을 드리프트로 잡는다", "bill_meetings" in schema_drift(db))

    # ── 주석 최신화: 갈아 끼우는 쪽과 **거부하는** 쪽을 둘 다 본다
    def _fake(conn, old: str, new: str) -> None:
        """옛 DB 를 흉내낸다 — 저장된 DDL 텍스트만 바꾼다."""
        v = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute("UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name='committees'",
                     (old, new))
        conn.execute(f"PRAGMA schema_version = {v + 1}")
        conn.execute("PRAGMA writable_schema = OFF")

    a = sqlite3.connect(":memory:")
    a.isolation_level = None
    a.executescript(SCHEMA)
    _fake(a, "조인 키다", "조인 키다(옛 주석)")
    check("주석이 어긋나면 드리프트다", "committees" in schema_drift(a))
    check("주석만 다르면 갈아 끼운다",
          migrate_comments(a) == [] and schema_drift(a) == [], str(schema_drift(a)))

    b = sqlite3.connect(":memory:")
    b.isolation_level = None
    b.executescript(SCHEMA)
    _fake(b, "committee_class  TEXT,", "committee_class  TEXT, zzz TEXT,")
    # ⚠️ 여기서 조용히 갈아 끼우면 실제 저장 구조와 DDL 이 어긋나 데이터를 잃는다.
    check("구조가 다르면 손대지 않고 이름만 돌려준다",
          migrate_comments(b) == ["committees"] and "committees" in schema_drift(b))

    print(f"\n{'실패 ' + str(len(fails)) + '건: ' + ', '.join(fails) if fails else '전부 통과'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "schema", "selftest", "consolidate"])
    ap.add_argument("--db", default=str(DB_PATH),
                    help="기본값은 실제 CONGRESS.db다. selftest 는 대상을 지우므로 받지 않는다")
    a = ap.parse_args()

    if a.command == "selftest":
        # **selftest 는 --db 를 반드시 받는다.** 이 함수의 첫 동작이 대상 .db/-wal/-shm 을
        # 무조건 unlink 하는 것이라, 기본값이 실제 DB인 채로 인자를 잊으면 수집물이 통째로
        # 사라진다. News 프로젝트에서 실제로 그렇게 4.25GB 를 잃었다 — 이름이 selftest 라
        # 안전할 것이라고 넘겨짚고 사용법을 읽지 않은 것이 전부였다.
        #
        # 그래서 경고 문구가 아니라 **인자 자체를 필수로** 만든다.
        # 문서는 안 읽히지만 인자는 안 주면 실행이 안 된다.
        if "--db" not in sys.argv:
            ap.error("selftest 는 --db 가 필수다 (대상을 지운다). 예: --db /tmp/t.db")
        if Path(a.db).resolve() == DB_PATH.resolve():
            ap.error("selftest 로 실제 CONGRESS.db 를 지목할 수 없다. 임시 경로를 써라.")
        return selftest(a.db)

    db = connect(a.db)
    init_schema(db)
    if a.command == "schema":
        for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
            print(r[0], ";\n", sep="")
        return 0
    if a.command == "consolidate":
        # 같은 실체가 두 행이 된 것을 합친다. **자동이 아니다** — 데이터를 다시 쓰는
        # 연산이라 감사(committee_near_dupes · gone_total)를 보고 사람이 부른다.
        for drop, keep in (m := consolidate_committees(db)):
            print(f"  {drop!r}\n    → {keep!r}")
        print(f"합친 위원회 {len(m)}쌍")
        merged, dropped = merge_dead_member_slugs(db)
        for drop, keep in merged:
            print(f"  {drop} → {keep}")
        print(f"합친 의원 {len(merged)}명 · 참조 없는 잔재 {len(dropped)}행 삭제")
        return 0

    print(f"initialized {a.db}")
    drift = schema_drift(db)
    if drift:
        print(f"\n⚠️  스키마 드리프트 {len(drift)}건: {', '.join(drift)}", file=sys.stderr)
        print("   DDL이 IF NOT EXISTS 라서 이 DB에는 변경이 적용되지 않았다.", file=sys.stderr)
        print("   .schema 로 읽히는 것은 옛 정의이므로 마이그레이션이 필요하다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
