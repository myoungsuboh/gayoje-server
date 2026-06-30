#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Master Integrity Daily Check — backup 외 추가 detection layer.
#
# [목적]
# backup 은 "사고 발생 후 복구". integrity check 는 "사고 발생 즉시 감지".
# 매일 backup 직후(04:00) 실행해 master CPS/PRD 가 비어있는 프로젝트 detect.
#
# [검사 항목]
# 1. 각 프로젝트의 Master CPS/PRD 의 full_markdown size > 50 bytes
# 2. cps_total / prd_total > 0 인데 master full_markdown 비어있으면 RED ALERT
# 3. type='Master' + is_latest=true 인 노드가 프로젝트별 0개 또는 2개 이상이면 WARNING
#
# [실패 시]
# - HEALTHCHECK_PING_URL 에 fail ping
# - 로그 + exit code 비-0 (cron 운영자가 인지)
#
# [실행]
#   sudo /usr/local/bin/neo4j_integrity_check.sh
#
# install_backup_cron.sh 가 매일 04:00 cron 등록 (backup 03:30 이후).
# ─────────────────────────────────────────────────────────────────────
# [2026-05-26 fix] set -e 제거 — grep -v 의 "매칭 0건 → exit 1" 이 트리거하면
# 진단 도중 silent 종료. 진단 스크립트는 에러 발생해도 가능한 한 진행 + 마지막에
# 종합 집계 + 적절한 exit code 명시 반환.
set -uo pipefail

NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j}"
DATABASE_NAME="${NEO4J_DATABASE:-neo4j}"
HEALTHCHECK_PING_URL="${HEALTHCHECK_PING_URL:-}"
MIN_MASTER_SIZE="${INTEGRITY_MIN_MASTER_SIZE:-50}"

log() { echo "[$(date -Iseconds)] $*"; }
err() { echo "[$(date -Iseconds)] ERROR: $*" >&2; }

fail_ping() {
    if [[ -n "$HEALTHCHECK_PING_URL" ]]; then
        curl -fsS --retry 3 -X POST "${HEALTHCHECK_PING_URL}/fail" \
            -H 'Content-Type: text/plain' --data-binary "$1" || true
    fi
}
success_ping() {
    if [[ -n "$HEALTHCHECK_PING_URL" ]]; then
        curl -fsS --retry 3 "$HEALTHCHECK_PING_URL" || true
    fi
}

run_cypher() {
    local query="$1"
    docker exec "$NEO4J_CONTAINER" \
        cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-neo4j}" --format plain \
        "$query" 2>/dev/null
}

log "integrity check 시작 — container=${NEO4J_CONTAINER} db=${DATABASE_NAME}"

# ─── 1. 빈 Master CPS 가 있는 프로젝트 ──────────────────────────
# 누적된 CPS 가 있는데 (cps_total > 0) Master 의 full_markdown 이 비어있으면
# 데이터 손상 의심. AI Agent 사고 시나리오와 정확히 일치.
# [2026-05-26 fix] cypher-shell 의 :param 가 non-interactive 호출에서 silent fail.
# bash 단에서 ${MIN_MASTER_SIZE} 를 query 안에 직접 치환 — query 가 정수 리터럴 사용.
EMPTY_CPS_QUERY="
MATCH (cps:CPS_Document)
WITH cps.project AS proj, count(cps) AS total
OPTIONAL MATCH (m:CPS_Document {project: proj, type: 'Master', is_latest: true})
WITH proj, total, m, size(coalesce(m.full_markdown, '')) AS md_size
WHERE total > 0 AND (m IS NULL OR md_size < ${MIN_MASTER_SIZE})
RETURN proj AS project, total AS cps_total, md_size AS master_md_size
"
EMPTY_CPS=$(run_cypher "$EMPTY_CPS_QUERY" || true)
EMPTY_CPS_LINES=$(echo "$EMPTY_CPS" | grep -v '^project' | grep -v '^$' | wc -l)

# ─── 2. 빈 Master PRD 가 있는 프로젝트 ──────────────────────────
EMPTY_PRD_QUERY="
MATCH (prd:PRD_Document)
WITH prd.project AS proj, count(prd) AS total
OPTIONAL MATCH (m:PRD_Document {project: proj, type: 'Master', is_latest: true})
WITH proj, total, m, size(coalesce(m.full_markdown, '')) AS md_size
WHERE total > 0 AND (m IS NULL OR md_size < ${MIN_MASTER_SIZE})
RETURN proj AS project, total AS prd_total, md_size AS master_md_size
"
EMPTY_PRD=$(run_cypher "$EMPTY_PRD_QUERY" || true)
EMPTY_PRD_LINES=$(echo "$EMPTY_PRD" | grep -v '^project' | grep -v '^$' | wc -l)

# ─── 3. type=Master + is_latest=true 노드가 프로젝트별 정확히 1개 ──
DUPLICATE_MASTER_QUERY="
MATCH (m {type: 'Master', is_latest: true})
WHERE m:CPS_Document OR m:PRD_Document
WITH m.project AS proj, labels(m)[0] AS label, count(m) AS cnt
WHERE cnt > 1
RETURN proj AS project, label, cnt AS master_count
"
DUPLICATES=$(run_cypher "$DUPLICATE_MASTER_QUERY" || true)
DUPLICATES_LINES=$(echo "$DUPLICATES" | grep -v '^project' | grep -v '^$' | wc -l)

# ─── 결과 집계 ───────────────────────────────────────────────────
TOTAL_ISSUES=$((EMPTY_CPS_LINES + EMPTY_PRD_LINES + DUPLICATES_LINES))

if [[ "$TOTAL_ISSUES" -eq 0 ]]; then
    log "✅ integrity OK — 데이터 손상 0건"
    success_ping
    exit 0
fi

err "❌ integrity issues — 총 ${TOTAL_ISSUES} 건 발견:"
if [[ "$EMPTY_CPS_LINES" -gt 0 ]]; then
    err "  Empty Master CPS (${EMPTY_CPS_LINES} 건):"
    echo "$EMPTY_CPS" | sed 's/^/    /' >&2
fi
if [[ "$EMPTY_PRD_LINES" -gt 0 ]]; then
    err "  Empty Master PRD (${EMPTY_PRD_LINES} 건):"
    echo "$EMPTY_PRD" | sed 's/^/    /' >&2
fi
if [[ "$DUPLICATES_LINES" -gt 0 ]]; then
    err "  Duplicate Master (${DUPLICATES_LINES} 건):"
    echo "$DUPLICATES" | sed 's/^/    /' >&2
fi

fail_ping "Neo4j integrity check failed at $(date -Iseconds): ${TOTAL_ISSUES} issues"
exit 1
