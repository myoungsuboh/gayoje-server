#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Neo4j 복구 스크립트 — neo4j_backup.sh 와 짝.
#
# [사용법]
#   scripts/neo4j_restore.sh <dump-file-path> [--dry-run] [--force]
#
#   <dump-file-path>: neo4j-admin database dump 가 생성한 .dump 파일.
#                     예: /var/backups/neo4j/2026-05-17/neo4j.dump
#
# [옵션]
#   --dry-run : 실제 적용 안 함. 어떤 명령이 실행될지만 출력 (drill 용).
#   --force   : 확인 프롬프트 건너뛰기 (cron / CI 용).
#
# [흐름]
#   1. dump 파일 존재 / 크기 검증
#   2. 사용자 확인 (--force 미사용 시) — DESTRUCTIVE: 기존 DB 덮어씀
#   3. neo4j 컨테이너 stop
#   4. dump 파일 컨테이너 안으로 복사
#   5. neo4j-admin database load --overwrite-destination
#   6. 컨테이너 start
#   7. health check (bolt 접속 확인)
#
# [복구 후 체크리스트]
#   - 사용자 로그인 가능 (auth_routes /auth/me)
#   - 프로젝트 리스트 정상 (project_repository)
#   - 인덱스 / 제약 자동 복원되는지 확인
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

# ===== 설정 =====
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j}"
DATABASE_NAME="${NEO4J_DATABASE:-neo4j}"
HEALTHCHECK_PING_URL="${HEALTHCHECK_PING_URL:-}"

DRY_RUN=false
FORCE=false
DUMP_PATH=""

log() { echo "[$(date -Iseconds)] $*"; }
err() { echo "[$(date -Iseconds)] ERROR: $*" >&2; }

usage() {
    grep -E '^#( |─)' "$0" | sed 's/^# \?//' | head -30
    exit 1
}

# ===== argv 파싱 =====
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --force)   FORCE=true; shift ;;
        -h|--help) usage ;;
        -*) err "알 수 없는 옵션: $1"; usage ;;
        *)
            if [[ -z "$DUMP_PATH" ]]; then
                DUMP_PATH="$1"
            else
                err "dump 파일은 1개만 지정 가능"
                usage
            fi
            shift
            ;;
    esac
done

if [[ -z "$DUMP_PATH" ]]; then
    err "dump 파일 경로 필수"
    usage
fi

# ===== dump 검증 =====
if [[ ! -f "$DUMP_PATH" ]]; then
    err "dump 파일 없음: $DUMP_PATH"
    exit 2
fi

DUMP_SIZE=$(stat -c%s "$DUMP_PATH" 2>/dev/null || stat -f%z "$DUMP_PATH")
if [[ "$DUMP_SIZE" -lt 1024 ]]; then
    err "dump 파일이 너무 작음 (${DUMP_SIZE} bytes) — 손상 의심"
    exit 3
fi

log "복구 대상 — container=${NEO4J_CONTAINER} db=${DATABASE_NAME}"
log "dump 파일 — ${DUMP_PATH} ($(du -h "$DUMP_PATH" | cut -f1))"

# ===== 사용자 확인 =====
if [[ "$FORCE" != "true" && "$DRY_RUN" != "true" ]]; then
    echo ""
    echo "⚠️  DESTRUCTIVE: 기존 ${DATABASE_NAME} DB 를 덮어씁니다."
    echo "   진행하려면 'YES' 입력:"
    read -r CONFIRM
    if [[ "$CONFIRM" != "YES" ]]; then
        log "사용자 취소"
        exit 4
    fi
fi

# ===== dry-run helper =====
run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        log "exec: $*"
        eval "$@"
    fi
}

fail_ping() {
    if [[ -n "$HEALTHCHECK_PING_URL" && "$DRY_RUN" != "true" ]]; then
        curl -fsS --retry 3 -X POST "${HEALTHCHECK_PING_URL}/fail" \
            -H 'Content-Type: text/plain' --data-binary "$1" || true
    fi
}
on_error() {
    local exit_code=$?
    err "restore failed (exit=$exit_code)"
    fail_ping "Neo4j restore failed at $(date -Iseconds) on $(hostname): exit=$exit_code"
    exit "$exit_code"
}
trap on_error ERR

# ===== 1. stop =====
log "step 1/5 — neo4j 컨테이너 stop"
run "docker stop \"$NEO4J_CONTAINER\""

# ===== 2. copy dump into container =====
log "step 2/5 — dump 파일 컨테이너 안으로 복사"
run "docker cp \"$DUMP_PATH\" \"${NEO4J_CONTAINER}:/tmp/${DATABASE_NAME}.dump\""

# ===== 3. load =====
log "step 3/5 — neo4j-admin database load (overwrite)"
# Community Edition 5.x — neo4j-admin database load --from-path=<dir> <db>
# Note: --from-path 는 디렉토리, 파일명은 <db>.dump 로 매칭됨.
run "docker run --rm \
    --volumes-from \"$NEO4J_CONTAINER\" \
    -v /tmp:/tmp \
    neo4j:5.26.0 neo4j-admin database load \"${DATABASE_NAME}\" \
        --from-path=/tmp \
        --overwrite-destination"

# ===== 4. start =====
log "step 4/5 — neo4j 컨테이너 start"
run "docker start \"$NEO4J_CONTAINER\""

# ===== 5. health check =====
log "step 5/5 — bolt health check (최대 30초 대기)"
if [[ "$DRY_RUN" != "true" ]]; then
    for i in {1..30}; do
        if docker exec "$NEO4J_CONTAINER" \
            cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-neo4j}" \
            'RETURN 1' >/dev/null 2>&1; then
            log "Neo4j 정상 — 복구 완료."
            if [[ -n "$HEALTHCHECK_PING_URL" ]]; then
                curl -fsS --retry 3 "$HEALTHCHECK_PING_URL" || true
            fi
            exit 0
        fi
        sleep 1
    done
    err "Neo4j 가 30초 안에 응답 없음 — 수동 확인 필요"
    exit 5
else
    log "[DRY-RUN] health check skip"
fi

log "복구 완료"
