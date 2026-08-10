#!/usr/bin/env bash
# Scripts/*.py 를 고치면 db.py selftest 를 돌린다. **통과하면 아무 말도 하지 않는다.**
#
# 왜 훅인가: 지난 개편에서 Scripts/*.py 를 열 번 넘게 고치며 selftest 를 손으로 돌렸는데도
#   bill_kind 회귀가 세 시간 뒤에야 잡혔고, 그 사이 bill_detail_missing 게이트는
#   아무것도 검사하지 않으면서 초록불이었다. 0.06초짜리 검사를 사람의 기억에 맡긴 대가다.
#
# 왜 PostToolUse 인가: 편집을 막을 필요가 없고 막을 수도 없다(도구가 이미 성공한 뒤에 뜬다).
#   목적은 회귀를 **그 자리에서** 알리는 것이다.
#
# ⚠️ 성공 시 침묵이 핵심이다. 편집마다 "전부 통과"를 출력하면 40번의 편집이 40줄의
#    노이즈가 되고, 그러면 41번째의 진짜 실패가 그 안에 묻힌다.
set -uo pipefail

input=$(cat)

# jq 가 없는 환경에서도 죽지 않는다 — 훅이 조용히 실패하면 아무도 모른다.
if command -v jq >/dev/null 2>&1; then
    path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
else
    path=$(printf '%s' "$input" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
fi

# 매처가 Write|Edit 전체라 실제 필터는 여기다. 이 조건이 이 훅의 유일한 정확성 보장이다.
case "$path" in
    *.claude/skills/congress/Scripts/*.py) ;;
    *) exit 0 ;;
esac

root="${CLAUDE_PROJECT_DIR:-$(git -C "$(dirname "$path")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
[ -f "$root/.claude/skills/congress/Scripts/db.py" ] || exit 0

# ⚠️ **mktemp -d 다. -u 가 아니다.** -u 는 경로 문자열만 만들고 디렉터리를 안 만들어
#    sqlite 가 파일을 못 연다 — 훅이 매번 실패해 곧 무시하게 된다.
tmp=$(mktemp -d) || exit 0
trap 'rm -rf "$tmp"' EXIT

# ⚠️ selftest 는 첫 동작이 대상 DB 삭제다. 실제 CONGRESS.db 를 넘기는 일이 절대 없어야 한다.
#    여기서 mktemp 경로만 넘기고, db.py 의 argparse 가드가 두 번째 겹으로 막는다.
#    **그 가드를 이 훅 때문에 완화하지 마라** — News 에서 4.25GB 를 잃은 이유가 그 가드다.
if ! out=$(cd "$root" && uv run .claude/skills/congress/Scripts/db.py selftest \
                              --db "$tmp/selftest.db" 2>&1); then
    printf 'db.py selftest 실패 — %s 편집 뒤 회귀다.\n\n%s\n' \
        "$path" "$(printf '%s' "$out" | grep -E 'FAIL|실패 [0-9]' || printf '%s' "$out" | tail -20)" >&2
    exit 2
fi
exit 0
