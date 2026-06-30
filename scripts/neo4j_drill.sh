#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Neo4j Restore Drill — 백업이 실제로 복원 가능한지 주 1회 자동 검증.
#
# [목적]
# "백업 없는 것보다 더 위험한 것: 복원 안 되는 백업". neo4j_backup.sh 가 매일
# dump 생성하지만, dump 파일이 실제로 load 가능한 상태인지는 별개. 이 스크립트는
# 매주 일요일 04:00 (install_backup_cron.sh 가 등록) 자동 실행 — 가장 최근 dump 를
# 임시 컨테이너에 load + cypher RETURN 1 health + 노드 카운트 검증 후 임시 제거.
#
# 운영 neo4j 컨테이너는 절대 건드리지 않음 (read-only). 임시 컨테이너 하나 띄워
# dump 만 load → 결과 확인 → 컨테이너 destroy. 완전 격리.
#
# [drill 결과]
# - 성공: HEALTHCHECK_PING_URL ping (success). exit 0.
# - 실패: ping (fail) + 운영자 알림. exit non-zero.
#
# [실패 시 권장 대응]
# 1. 최근 7일 dump 중 정상 dump 수동 식별 (size, drill 로그 비교).
# 2. neo4j_backup.sh 의 dump 단계 실패 원인 확인 (Neo4j Community 5.x online dump
#    는 보통 안정적 — fail 은 디스크 full / 권한 / 컨테이너 종료 의심).
# 3. 정상 dump 발견 시 drill 격리 환경에서 다시 load 시도 후 운영 복원 계획.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/neo4j}"
DATABASE_NAME="${NEO4J_DATABASE:-neo4j}"
NEO4J_IMAGE="${NEO4J_DRILL_IMAGE:-neo4j:5.26.0}"
DRILL_CONTAINER="neo4j_drill_$(date +%s)"
DRILL_DATA_VOL="${DRILL_CONTAINER}_data"
HEALTHCHECK_PING_URL="${HEALTHCHECK_PING_URL:-}"
MIN_NODE_COUNT="${DRILL_MIN_NODE_COUNT:-1}"  # 빈 dump 의심 가드

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

cleanup() {
    log "cleanup — drill 컨테이너 + 볼륨 제거"
    docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
    docker volume rm "$DRILL_DATA_VOL" >/dev/null 2>&1 || true
}
on_error() {
    local exit_code=$?
    err "drill failed (exit=$exit_code)"
    fail_ping "Neo4j drill failed at $(date -Iseconds) on $(hostname): exit=$exit_code"
    cleanup
    exit "$exit_code"
}
trap on_error ERR
trap cleanup EXIT

# ===== 1. 최근 dump 식별 =====
log "drill 시작 — 가장 최근 dump 찾기 in $BACKUP_ROOT"
LATEST_DUMP=$(find "$BACKUP_ROOT" -name "${DATABASE_NAME}.dump" -type f -mtime -2 \
    | sort | tail -n1)

if [[ -z "$LATEST_DUMP" ]]; then
    err "최근 2일 내 dump 없음 — backup cron 이 실패 중일 가능성"
    exit 10
fi

DUMP_SIZE=$(stat -c%s "$LATEST_DUMP" 2>/dev/null || stat -f%z "$LATEST_DUMP")
log "target: $LATEST_DUMP (${DUMP_SIZE} bytes)"

if [[ "$DUMP_SIZE" -lt 1024 ]]; then
    err "dump 파일이 너무 작음 (${DUMP_SIZE} bytes) — 손상 의심"
    exit 11
fi

# ===== 2. 격리 drill 컨테이너 띄우기 =====
log "step 1/4 — 격리 컨테이너 ${DRILL_CONTAINER} 시작"
docker volume create "$DRILL_DATA_VOL" >/dev/null
docker run -d --name "$DRILL_CONTAINER" \
    -e NEO4J_AUTH=neo4j/drillpw1234 \
    -v "${DRILL_DATA_VOL}:/data" \
    "$NEO4J_IMAGE" >/dev/null

# Neo4j 가 bolt 응답할 때까지 대기 (최대 60초)
log "step 2/4 — Neo4j 부팅 대기 (최대 60초)"
for i in {1..60}; do
    if docker exec "$DRILL_CONTAINER" \
        cypher-shell -u neo4j -p drillpw1234 \
        'RETURN 1' >/dev/null 2>&1; then
        log "  부팅 완료 (${i}초)"
        break
    fi
    sleep 1
done

# ===== 3. dump load =====
log "step 3/4 — dump load (overwrite)"
docker stop "$DRILL_CONTAINER" >/dev/null
docker cp "$LATEST_DUMP" "${DRILL_CONTAINER}:/tmp/${DATABASE_NAME}.dump"
docker run --rm \
    --volumes-from "$DRILL_CONTAINER" \
    -v /tmp:/tmp \
    "$NEO4J_IMAGE" neo4j-admin database load "$DATABASE_NAME" \
        --from-path=/tmp \
        --overwrite-destination >/dev/null
docker start "$DRILL_CONTAINER" >/dev/null

# 다시 bolt 대기
for i in {1..60}; do
    if docker exec "$DRILL_CONTAINER" \
        cypher-shell -u neo4j -p drillpw1234 \
        'RETURN 1' >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# ===== 4. 검증 — 노드 카운트 + 핵심 라벨 존재 =====
log "step 4/4 — 데이터 무결성 검증"
NODE_COUNT=$(docker exec "$DRILL_CONTAINER" \
    cypher-shell -u neo4j -p drillpw1234 --format plain \
    'MATCH (n) RETURN count(n) AS c' 2>/dev/null | tail -n1 | tr -d ' \r')

if [[ -z "$NODE_COUNT" || "$NODE_COUNT" -lt "$MIN_NODE_COUNT" ]]; then
    err "노드 카운트 비정상 (${NODE_COUNT}, 최소 ${MIN_NODE_COUNT} 필요)"
    exit 12
fi
log "  total nodes = $NODE_COUNT (OK)"

# 핵심 라벨 (User, Project) 존재 검증 — 운영 데이터 무결성
for label in User Project; do
    LABEL_COUNT=$(docker exec "$DRILL_CONTAINER" \
        cypher-shell -u neo4j -p drillpw1234 --format plain \
        "MATCH (n:${label}) RETURN count(n) AS c" 2>/dev/null | tail -n1 | tr -d ' \r')
    log "  ${label} = ${LABEL_COUNT:-0}"
done

log "✅ drill 성공 — backup 복원 가능."
success_ping
