# Neo4j Backup & Restore 운영 가이드

## 왜 필요한가

[2026-05-26 AI Agent 프로젝트 사고]
CPS_Document V1~V6 의 properties 가 모두 비워진 상태로 발견. 정확한 wipe 시점
추적 불가. 백업 없으면 사용자가 수동으로 V1~V20 모두 재처리해야 함. 백업이
있으면 24h 전 상태로 복원 가능 — 손실 0.

## 활성화 (1회 실행, 약 1분)

운영 호스트에 SSH 접속 후:

```bash
cd /path/to/harness-server  # repo clone 위치
sudo bash scripts/install_backup_cron.sh
```

자동으로:
- `/usr/local/bin/neo4j_backup.sh` + `/usr/local/bin/neo4j_drill.sh` 복사
- root crontab 에 매일 03:30 backup + 매주 일요일 04:00 drill 등록 (멱등 — 재실행 안전)
- `/var/log/neo4j_backup.log`, `/var/log/neo4j_drill.log` 생성
- `/etc/logrotate.d/neo4j_backup` 주간 회전 설정

설치 후:
```bash
crontab -l    # 확인
tail -f /var/log/neo4j_backup.log
```

## 즉시 백업 (수동 트리거)

```bash
sudo /usr/local/bin/neo4j_backup.sh
# 결과: /var/backups/neo4j/YYYY-MM-DD/neo4j.dump
```

### ⚠ 다운타임 — 약 10~30초

Neo4j **Community Edition** 은 offline dump 만 지원 (Enterprise 만 online).
스크립트는 다음 흐름으로 동작:
1. `docker stop neo4j` (다운타임 시작)
2. 같은 data 볼륨으로 별도 컨테이너 띄워 `neo4j-admin database dump` 실행
3. `docker start neo4j` (다운타임 종료)

매일 03:30 새벽 시간이라 사용자 영향 최소. dump 실패 시 trap 이 컨테이너 재시작
시도 — 데이터 손실 없이 컨테이너만 stopped 로 남는 것 방지.

운영자 추가 점검:
- 백업 도중 backend 가 503 응답 — Caddy 가 그대로 전달.
- 백업 후 backend 가 다음 요청에서 Neo4j 재연결 (드라이버가 자동 재시도).
- 만약 backend 가 회복 못하면 `docker restart backend worker` 로 수동 재시작.

## S3 업로드 활성화 (권장)

호스트 외부에도 보관해야 디스크 손상 시도 안전.

```bash
# AWS CLI + IAM 인증 사전 설정 후
BACKUP_S3_BUCKET=s3://my-bucket/neo4j \
HEALTHCHECK_PING_URL=https://hc-ping.com/<uuid> \
  sudo bash scripts/install_backup_cron.sh
```

`install_backup_cron.sh` 가 crontab 에 환경변수를 함께 등록하므로 cron 실행 시 자동 사용.

S3 라이프사이클 (콘솔 또는 IaC) 권장:
- 30일 이후 → STANDARD_IA
- 90일 이후 → GLACIER
- 365일 이후 → 삭제

## 모니터링 (healthchecks.io)

1. https://healthchecks.io 가입 + 무료 plan (5개 check)
2. "Add Check" → schedule: daily, grace: 1h
3. URL 복사 → `HEALTHCHECK_PING_URL` 환경변수
4. `install_backup_cron.sh` 재실행

이후:
- 백업 성공 → URL 호출 (success)
- 백업 실패 → `URL/fail` 호출
- 24h 동안 ping 없으면 healthchecks.io 가 이메일/Slack 알림

## Restore Drill (주 1회 자동)

`scripts/neo4j_drill.sh` 가 매주 일요일 04:00 자동 실행:
1. 가장 최근 dump 식별 (최근 2일 내)
2. **격리 컨테이너** 띄움 (운영 neo4j 안 건드림)
3. dump load
4. cypher `RETURN 1` health check
5. 노드 카운트 검증 (최소 1개, User/Project 라벨 존재 확인)
6. 임시 컨테이너 + 볼륨 제거
7. healthchecks.io ping

**왜 drill 이 중요한가**: "복원 안 되는 백업" 은 백업 없는 것보다 위험 (잘못된
안전감). drill 이 매주 검증해야 신뢰 가능.

수동 drill:
```bash
sudo HEALTHCHECK_PING_URL='' /usr/local/bin/neo4j_drill.sh
```

## 진짜 복구 절차

drill 이 아닌 실제 운영 복원이 필요한 상황 (데이터 손상 발생):

```bash
# 1. 복원 대상 dump 식별
ls -la /var/backups/neo4j/

# 2. dry-run 으로 명령 확인
sudo /usr/local/bin/neo4j_restore.sh /var/backups/neo4j/2026-05-25/neo4j.dump --dry-run

# 3. 실제 복원 (DESTRUCTIVE — 운영 DB 덮어씀)
sudo NEO4J_PASSWORD=<password> /usr/local/bin/neo4j_restore.sh \
    /var/backups/neo4j/2026-05-25/neo4j.dump
# → 'YES' 확인 입력
```

복원 후 체크리스트:
- `/auth/me` API 로 로그인 가능 확인
- 프로젝트 리스트 정상 조회
- 인덱스/제약 자동 복원 확인 (`SHOW INDEXES;`, `SHOW CONSTRAINTS;`)

## Uninstall

```bash
sudo crontab -l | grep -v 'neo4j_backup\.sh\|neo4j_drill\.sh' | sudo crontab -
sudo rm -f /usr/local/bin/neo4j_backup.sh /usr/local/bin/neo4j_drill.sh
sudo rm -f /etc/logrotate.d/neo4j_backup
```

## 참고
- 백업 스크립트: [scripts/neo4j_backup.sh](../../scripts/neo4j_backup.sh)
- 복원 스크립트: [scripts/neo4j_restore.sh](../../scripts/neo4j_restore.sh)
- Drill 스크립트: [scripts/neo4j_drill.sh](../../scripts/neo4j_drill.sh)
- 설치 자동화: [scripts/install_backup_cron.sh](../../scripts/install_backup_cron.sh)
