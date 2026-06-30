#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Neo4j 백업 스크립트 — cron 으로 일일 실행 권장.
#
# [사용법]
#   1. 서버에 복사 후 실행권한:
#        sudo cp scripts/neo4j_backup.sh /usr/local/bin/
#        sudo chmod +x /usr/local/bin/neo4j_backup.sh
#   2. cron 등록 (root) — 매일 새벽 3:30 (서버 timezone 기준):
#        sudo crontab -e
#        30 3 * * * /usr/local/bin/neo4j_backup.sh >> /var/log/neo4j_backup.log 2>&1
#   3. 외부 보관소 동기화 (선택):
#        BACKUP_S3_BUCKET=s3://my-bucket/neo4j 환경변수 설정 시 aws s3 sync
#
# [정책]
# - 백업: neo4j-admin database dump (online 가능, lock 없음).
# - 보관: 로컬 7일 (rotate), S3 30일 (S3 lifecycle 설정 가정).
# - 출력: /var/backups/neo4j/YYYY-MM-DD/neo4j.dump
#
# [복구]
#   docker stop neo4j
#   docker run --rm -v neo4j_data:/data -v $(pwd):/restore \
#     neo4j:5.27 neo4j-admin database load neo4j --from-path=/restore --overwrite-destination
#   docker start neo4j
#
# [모니터링]
# 실패 시 healthchecks.io ping 권장 — HEALTHCHECK_PING_URL 설정 시 success/fail ping.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

# ===== 설정 =====
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j}"
# [2026-05-26 fix] 호스트의 neo4j image 와 동일 버전으로 dump 실행 (다른 image 면
# storage format 불일치로 dump 실패 가능). 운영자가 다른 버전 쓰면 override.
NEO4J_IMAGE="${NEO4J_BACKUP_IMAGE:-neo4j:5.26.0}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/neo4j}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
DATABASE_NAME="${NEO4J_DATABASE:-neo4j}"
TODAY=$(date +%F)
TARGET_DIR="${BACKUP_ROOT}/${TODAY}"

# 선택: 실패 / 성공 알림용 healthchecks.io 또는 webhook URL
HEALTHCHECK_PING_URL="${HEALTHCHECK_PING_URL:-}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"

log() { echo "[$(date -Iseconds)] $*"; }
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
on_error() {
    local exit_code=$?
    log "ERROR: backup failed (exit=$exit_code)"
    fail_ping "Neo4j backup failed at $(date -Iseconds) on $(hostname): exit=$exit_code"
    exit "$exit_code"
}
trap on_error ERR

# ===== 실행 =====
log "neo4j backup start — container=${NEO4J_CONTAINER} db=${DATABASE_NAME} target=${TARGET_DIR}"
mkdir -p "$TARGET_DIR"
# [2026-05-26 fix] dump container 의 user 가 host 의 0755 root-only dir 에 write
# 못 하는 호스트 환경(userns remap / SELinux 등) 호환을 위해 sticky world-write.
# Sticky bit 로 다른 user 의 파일 삭제는 막음. cron 운영 root 만 dump 저장.
chmod 1777 "$TARGET_DIR"

# [2026-05-26 fix] Community Edition 은 offline dump 만 지원 (Enterprise 만 online).
# 컨테이너 stop → dump → start 패턴. 다운타임 ~10-30초 (새벽 03:30 영향 최소).
#
# stop 후 별도 컨테이너에서 --volumes-from 로 같은 data 볼륨 마운트 + dump.
# 호스트의 $TARGET_DIR 을 컨테이너의 /backup 으로 마운트 → dump 가 직접 호스트
# 디스크에 저장. docker cp 단계 불필요.
#
# 실패 시 trap on_error 가 컨테이너를 다시 start 시도하도록 ensure_started 호출.
ensure_started() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${NEO4J_CONTAINER}$"; then
        log "WARN: neo4j 컨테이너가 stopped — start 재시도"
        docker start "$NEO4J_CONTAINER" >/dev/null 2>&1 || true
    fi
}
trap 'ensure_started; on_error' ERR

# 1. neo4j 컨테이너 stop
log "step 1/3 — neo4j 컨테이너 stop (offline dump 위해)"
docker stop "$NEO4J_CONTAINER" >/dev/null

# 2. 같은 data 볼륨으로 별도 컨테이너 띄워 dump.
# --user root: 기본 neo4j user (uid 7474) 가 마운트된 호스트 디렉토리에 write
# 권한 부족해 dump 실패. root 로 실행해 권한 회피. 결과 파일은 7474:7474 소유.
log "step 2/3 — dump 생성 (image=${NEO4J_IMAGE})"
docker run --rm \
    --volumes-from "$NEO4J_CONTAINER" \
    --user root \
    -v "${TARGET_DIR}:/backup" \
    "$NEO4J_IMAGE" \
    neo4j-admin database dump "$DATABASE_NAME" \
        --to-path=/backup \
        --overwrite-destination >/dev/null

# 3. 컨테이너 start (서비스 복구)
log "step 3/3 — neo4j 컨테이너 start"
docker start "$NEO4J_CONTAINER" >/dev/null

SIZE=$(du -h "${TARGET_DIR}/${DATABASE_NAME}.dump" | cut -f1)
log "dump created: ${TARGET_DIR}/${DATABASE_NAME}.dump (${SIZE})"

# 3. (선택) S3 업로드
if [[ -n "$BACKUP_S3_BUCKET" ]]; then
    log "uploading to S3: ${BACKUP_S3_BUCKET}/${TODAY}/"
    aws s3 cp "${TARGET_DIR}/${DATABASE_NAME}.dump" \
        "${BACKUP_S3_BUCKET}/${TODAY}/${DATABASE_NAME}.dump" \
        --storage-class STANDARD_IA
fi

# 4. 로컬 보관 — RETENTION_DAYS 일 이상 오래된 디렉토리 삭제
log "cleaning local backups older than ${RETENTION_DAYS} days"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +

log "neo4j backup complete"
success_ping
