# gayoje-server

**가요제 통합 플랫폼** 백엔드. 전국 가요제·노래대회 정보를 1차 공공 출처
(공공데이터포털 표준데이터·TourAPI·문화예술공연 OpenAPI·지자체 eGov 게시판)에서
수집·정규화해 무료 통합 제공하고, 음원/악보 제출 대행·주최 접수 SaaS·제휴 마켓으로
수익화하는 PWA의 서버.

> ⚠️ **현재 상태**: 이 레포는 인프라 재사용을 위해 다른 서비스 백엔드를 복사한 뒤
> 도메인 코드를 도려내고 가요제로 전환(strip & rebrand)하는 중입니다.
> 빌드 작업 순서는 [`singaservertasklist/`](singaservertasklist/) 참조.

## 기술 스택

| 영역 | 기술 |
|---|---|
| 웹 프레임워크 | FastAPI (ASGI, `uvicorn`) |
| 사용자/결제 SOR | PostgreSQL |
| 관계 그래프(아티스트·곡·팀·출연) | Neo4j |
| 캐시·큐 브로커·토큰 블랙리스트 | Redis |
| 비동기 잡 | arq |
| LLM 게이트웨이 | LiteLLM proxy |
| 리버스 프록시 / 자동 HTTPS | Caddy (Let's Encrypt) |
| 인증 | JWT (access/refresh 로테이션) + 카카오/네이버 OAuth |
| 결제 | 토스 결제위젯 |
| 호스팅 | 한국 리전 (NCP / AWS 서울) |

프런트엔드는 별도 레포 **gayoje-client** (Vue 3 + Vuetify, PWA).

## 디렉터리 개요

```
app/
  api/        FastAPI 라우터 (main.py = 앱·미들웨어 등록)
  clients/    외부 클라이언트 (LLM, Neo4j)
  core/       인프라 (config, 보안/JWT, 관측, rate limit, 암호화, OAuth …)
  service/    저장소 계층 (auth, audit, user, usage, notification …)
  queue/      arq 큐/워커 (client, settings, worker, jobs)
  pipelines/  LLM 파이프라인 유틸 (base.py = JSON 추출/재시도)
evals/        데이터 품질 점수화 골격
litellm/      LiteLLM proxy 설정
scripts/      운영 스크립트 (Neo4j 백업/복구 등)
tests/        pytest
```

## 로컬 개발

### 1) 의존성

```bash
python -m venv .venv
. .venv/Scripts/activate    # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt -r requirements-dev.txt
```

### 2) 환경변수

```bash
cp .env.example .env
# .env 를 열어 실제 값(시크릿)을 채운다. .env 는 절대 커밋하지 않는다(.gitignore 처리됨).
```

필수: `JWT_SECRET_KEY`(운영은 `openssl rand -hex 32`), `NEO4J_*`, `REDIS_URL`.
LLM 호출이 필요하면 `LITELLM_PROXY_URL` 또는 `GEMINI_API_KEY`.

### 3) 인프라 (Docker)

```bash
docker compose up -d redis        # 로컬은 Redis 만으로도 부팅 가능
# Neo4j / LiteLLM / Caddy 는 docker-compose.yml 참조
```

### 4) 실행

```bash
python run.py                     # API 서버 (기본 :8000)
arq app.queue.worker.WorkerSettings   # 워커 (별도 터미널)
```

헬스체크: `GET http://localhost:8000/health` → `200`

## 테스트

```bash
pytest                            # 전체
pytest tests/core                 # 인프라만
```

## 법적·데이터 원칙

- 수집은 **1차 공공 출처에서만**. 경쟁사 가공 DB 스크래핑 금지.
- 포스터/영상/요강 **재호스팅 금지** — 출처표기 + 공식 임베드.
- `robots.txt`·레이트리밋·개인정보보호법(PIPA) 준수.
