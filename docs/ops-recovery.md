# 운영 복구 런북 (서버 크래시 / 재부팅)

> 서버가 터지거나 재시작이 필요할 때 **무엇이 어떤 순서로 올라오는지** + **어떻게 검증하는지** 1페이지 정리.
> 정작 장애 때 순서가 헷갈리므로 평소에 읽어둘 것.

---

## 0. 핵심 원리 — 자동 복구는 Portainer가 아니라 `restart` 정책이 한다

서버 재부팅 시 컨테이너 자동 복구의 3요소:

1. **Docker 데몬 부팅 자동시작** — `sudo systemctl enable docker` (한 번만 설정, 필수)
2. **`restart: always`** — 이 스택 전 서비스에 설정됨 ✅
3. **named volume 영속화** — `redis_data` / `sqlite_data` / `caddy_data` / `caddy_config` ✅

→ 위 3개가 갖춰지면 **재부팅 후 Docker가 알아서 전부 다시 띄운다.** Portainer는 관리·가시성 도구이지 복구 수단이 아니다.

> ⚠️ **Portainer 자기 자신**은 이 스택 안에 없다(닭-달걀). Portainer 컨테이너에도
> `restart: always` + `portainer_data` 볼륨이 걸려 있어야 재부팅 후 같이 올라온다.
> 이게 빠지면 "앱은 살아났는데 Portainer만 안 떠서" 복구 안 된 줄 착각함.

---

## 1. 스택 구성 & 기동 순서 (의존성)

| 순서 | 스택 / 컨테이너 | 역할 | 비고 |
|---|---|---|---|
| 1 | **Portainer** | 관리 UI | 별도 compose, `restart: always` 확인 |
| 2 | **neo4j** 스택 | 그래프 DB | 네트워크 `neo4j_default` 생성·소유 |
| 3 | **litellm** 스택 | LLM 프록시 | 네트워크 `litellm_*` 생성·소유 |
| 4 | **메인 스택** (이 repo) | caddy / redis / backend / worker | 위 두 네트워크에 `external` join |

**왜 순서가 중요한가:** 메인 스택은 `neo4j_net`(=`neo4j_default`)·`litellm_net`을 **external**로 붙는다.
neo4j·litellm 스택이 먼저 떠서 그 네트워크가 존재해야 backend/worker가 붙는다.
순서가 어긋나도 `restart: always`가 계속 재시도하므로 **결국 복구되지만**, neo4j·litellm 스택도
반드시 Portainer에 등록 + `restart: always`여야 한다.

메인 스택 내부 의존: `redis → backend → caddy / billing-cron`, `redis → worker` (compose `depends_on`이 처리).

---

## 2. 전체 복구 절차 (서버가 완전히 내려갔다 올라온 경우)

대부분 **아무것도 안 해도 자동 복구**된다(0번 원리). 그래도 수동 확인/개입이 필요하면:

```bash
# 1) Docker 데몬 떠 있는지
systemctl status docker        # active 아니면: sudo systemctl start docker

# 2) 컨테이너 상태 일괄 확인
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RestartCount}}'

# 3) 외부 네트워크 존재 확인 (메인 스택 전제조건)
docker network ls | grep -E 'neo4j_default|litellm'
#   없으면 → neo4j / litellm 스택을 Portainer에서 먼저 redeploy

# 4) 메인 스택이 계속 재시작 루프면 (보통 3번이 원인) 네트워크 복구 후
#    Portainer UI → Stacks → 메인 스택 → "Update the stack" (Pull and redeploy)
```

> **수동 compose 명령은 지양.** 운영은 Portainer GitOps(master 폴링)로 관리된다. CLI로 직접
> `docker compose up` 하면 Portainer가 추적하는 상태와 어긋날 수 있다. 가능하면 Portainer UI 사용.

---

## 3. 검증 (복구 후 정상 동작 확인)

```bash
# 헬스 (프로세스 살아있음 — 1초 내 응답)
curl -fsS https://api.example.com/health
# → {"status":"healthy"}

# 깊은 헬스 (Neo4j + Redis 의존성까지)
curl -fsS https://api.example.com/health/deep
# → 의존성 ok 여부. 여기서 neo4j/redis 연결 실패면 2-3번(네트워크/스택 순서) 재점검

# (선택) 메트릭 — 같은 docker 네트워크 안에서
docker exec backend curl -fsS http://localhost:8000/metrics | head
```

체크리스트:
- [ ] `docker ps` 에 caddy / redis / backend / worker 모두 `Up`
- [ ] `/health` 200, `/health/deep` 의존성 ok
- [ ] HTTPS(Caddy 443) 정상 — 인증서 `caddy_data` 볼륨에서 복원됨
- [ ] worker 로그에 arq startup 완료

---

## 4. 환경변수 / 시크릿 체크리스트

스택 env(Portainer가 보관)에 아래가 있어야 정상 동작. 복구 후 누락 의심 시 확인:

| 변수 | 용도 | 누락 시 증상 |
|---|---|---|
| `JWT_SECRET_KEY` | 인증 (32+자) | production 부팅 거부(의도된 가드) |
| `NEO4J_URI/USERNAME/PASSWORD/DATABASE` | DB | `/health/deep` 실패 |
| `REDIS_URL` | 큐/락/throttle | worker·락 동작 안 함 |
| `PADDLE_API_KEY` / `PADDLE_WEBHOOK_SECRET` | 결제 (Paddle MoR) | webhook 미처리 → 구독 동기화 안 됨 |
| `GEMINI_API_KEY` / `LITELLM_*` | LLM | 파이프라인 실패 |
| `RESEND_API_KEY` | 이메일 | 영수증/알림 미발송(결제 흐름엔 무영향) |
| `SENTRY_DSN` (선택) | 에러추적 | 미설정 시 no-op(안전) |

> **GitOps redeploy는 stack env 보존.** 단 `PUT /api/stacks/{id}/git/redeploy` API는 env를
> wipe한 사고 이력(2026-05-16) 있음 → **정상 흐름(폴링/UI Pull&redeploy)만 사용.** (deploy.yml 참고)

---

## 5. 자주 나는 장애 & 대처

| 증상 | 원인 | 대처 |
|---|---|---|
| 재부팅 후 아무것도 안 뜸 | Docker 부팅 자동시작 꺼짐 | `sudo systemctl enable --now docker` |
| backend가 재시작 루프 | external 네트워크(neo4j_default 등) 부재 | neo4j/litellm 스택 먼저 redeploy |
| Portainer만 안 뜸 | Portainer 컨테이너에 restart 정책 없음 | `docker update --restart=always portainer` |
| 새 의존성/코드 미반영 | 이미지 재빌드 안 됨 | Portainer 스택 redeploy 시 **이미지 rebuild** 옵션 + master 최신 확인 |
| Paddle webhook 401/거부 | `PADDLE_WEBHOOK_SECRET` 불일치 | Paddle 대시보드 webhook secret 과 스택 env 대조 |
| HTTPS 인증서 오류 | `caddy_data` 볼륨 유실 | 볼륨 복원 또는 Caddy 재발급 대기(LE rate limit 주의) |

---

## 6. 평상시 예방 (한 번 해두면 좋은 것)

- [ ] `sudo systemctl enable docker` (부팅 자동시작)
- [ ] Portainer 컨테이너 `restart: always` + `portainer_data` 볼륨
- [ ] 메인 / neo4j / litellm **세 스택 모두 Portainer에 Git 연동 등록** (수동 복구 시 한곳에서)
- [ ] Neo4j 정기 백업 (`scripts/neo4j_backup.sh`) + 복원 드릴(`scripts/neo4j_drill.sh`)
- [ ] 이 문서 위치 공유
