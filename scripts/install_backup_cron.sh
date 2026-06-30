#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Neo4j 자동 백업 cron 등록 — 호스트에서 1회 실행.
#
# [목적]
# scripts/neo4j_backup.sh 가 매일 03:30 에 자동 실행되도록 root cron 에 등록.
# 추가로 매주 일요일 04:00 에 restore drill 자동 실행 (backup 이 실제 복원
# 가능한지 검증 — backup-without-drill 은 거짓 안전감 만 줌).
#
# [사용법]
#   sudo bash scripts/install_backup_cron.sh
#
# [선택 환경변수 (cron 환경에 주입됨)]
#   BACKUP_S3_BUCKET=s3://my-bucket/neo4j      — S3 자동 업로드
#   HEALTHCHECK_PING_URL=https://hc-ping.com/X — healthchecks.io 알림
#   NEO4J_PASSWORD=...                          — drill 시 cypher health check
#
# [멱등]
# 동일 항목 중복 등록 방지 — 기존 라인 매칭 시 skip + 알림.
#
# [Uninstall]
#   sudo crontab -l | grep -v 'neo4j_backup\.sh\|neo4j_drill\.sh' | sudo crontab -
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: sudo / root 권한 필요 — sudo bash $0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="${SCRIPT_DIR}/neo4j_backup.sh"
DRILL_SCRIPT="${SCRIPT_DIR}/neo4j_drill.sh"
INTEGRITY_SCRIPT="${SCRIPT_DIR}/neo4j_integrity_check.sh"

INSTALL_DIR="/usr/local/bin"
LOG_DIR="/var/log"
BACKUP_LOG="${LOG_DIR}/neo4j_backup.log"
DRILL_LOG="${LOG_DIR}/neo4j_drill.log"
INTEGRITY_LOG="${LOG_DIR}/neo4j_integrity.log"

log() { echo "[$(date -Iseconds)] $*"; }

# ===== 1. 스크립트 복사 =====
log "1/4 — 스크립트 ${INSTALL_DIR}/ 복사"
for f in "$BACKUP_SCRIPT" "$DRILL_SCRIPT" "$INTEGRITY_SCRIPT"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: 스크립트 없음 — $f" >&2
        exit 2
    fi
    cp "$f" "${INSTALL_DIR}/$(basename "$f")"
    chmod +x "${INSTALL_DIR}/$(basename "$f")"
    log "  installed: ${INSTALL_DIR}/$(basename "$f")"
done

# ===== 2. 로그 폴더 + 파일 권한 =====
log "2/4 — 로그 파일 준비"
touch "$BACKUP_LOG" "$DRILL_LOG" "$INTEGRITY_LOG"
chmod 0640 "$BACKUP_LOG" "$DRILL_LOG" "$INTEGRITY_LOG"
log "  ready: $BACKUP_LOG, $DRILL_LOG, $INTEGRITY_LOG"

# ===== 3. crontab 등록 (멱등) =====
log "3/4 — crontab 등록"
CRON_BACKUP="30 3 * * * BACKUP_S3_BUCKET='${BACKUP_S3_BUCKET:-}' HEALTHCHECK_PING_URL='${HEALTHCHECK_PING_URL:-}' ${INSTALL_DIR}/neo4j_backup.sh >> ${BACKUP_LOG} 2>&1"
CRON_INTEGRITY="0 4 * * * NEO4J_PASSWORD='${NEO4J_PASSWORD:-}' HEALTHCHECK_PING_URL='${HEALTHCHECK_INTEGRITY_PING_URL:-${HEALTHCHECK_PING_URL:-}}' ${INSTALL_DIR}/neo4j_integrity_check.sh >> ${INTEGRITY_LOG} 2>&1"
CRON_DRILL="0 5 * * 0 NEO4J_PASSWORD='${NEO4J_PASSWORD:-}' HEALTHCHECK_PING_URL='${HEALTHCHECK_PING_URL:-}' ${INSTALL_DIR}/neo4j_drill.sh >> ${DRILL_LOG} 2>&1"

CURRENT_CRON=$(crontab -l 2>/dev/null || true)

add_if_missing() {
    local entry="$1"
    local marker="$2"
    if echo "$CURRENT_CRON" | grep -qF "$marker"; then
        log "  skip (이미 등록됨): $marker"
    else
        CURRENT_CRON="${CURRENT_CRON}
${entry}"
        log "  added: $marker"
    fi
}

add_if_missing "$CRON_BACKUP"    "neo4j_backup.sh"
add_if_missing "$CRON_INTEGRITY" "neo4j_integrity_check.sh"
add_if_missing "$CRON_DRILL"     "neo4j_drill.sh"

echo "$CURRENT_CRON" | crontab -

# ===== 4. logrotate 등록 (선택, 있으면 활용) =====
log "4/4 — logrotate 설정"
LOGROTATE_CONF="/etc/logrotate.d/neo4j_backup"
if [[ -d "/etc/logrotate.d" ]]; then
    cat > "$LOGROTATE_CONF" <<EOF
${BACKUP_LOG} ${DRILL_LOG} ${INTEGRITY_LOG} {
    weekly
    rotate 12
    compress
    missingok
    notifempty
    create 0640 root adm
}
EOF
    log "  installed: $LOGROTATE_CONF"
else
    log "  skip — /etc/logrotate.d 없음"
fi

echo ""
log "✅ 설치 완료. 다음 사항 확인:"
echo "  - crontab -l           (등록 확인)"
echo "  - 첫 백업: 내일 03:30. 즉시 검증하려면: sudo ${INSTALL_DIR}/neo4j_backup.sh"
echo "  - 첫 drill: 다음 일요일 04:00"
echo "  - 로그: tail -f ${BACKUP_LOG}"
echo ""
echo "  S3 백업 활성화: BACKUP_S3_BUCKET 환경변수 설정 후 재실행"
echo "  알림 활성화:    HEALTHCHECK_PING_URL=https://hc-ping.com/<uuid> 설정 후 재실행"
