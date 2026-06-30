# LiteLLM Proxy 운영 가이드

Gemini API 쿼터 소진(429) 시 multi-key 자동 로테이션으로 서비스 중단을 막는다.

## 동작 개요

```
backend / worker
    │
    │  POST /v1/chat/completions  (Bearer LITELLM_MASTER_KEY)
    ▼
┌─────────────────────────────┐
│  LiteLLM Proxy (컨테이너)    │
│  - GEMINI_API_KEY_1 시도     │ ← 429 받으면 60초 cooldown
│  - GEMINI_API_KEY_2 시도     │
│  - GEMINI_API_KEY_3 시도     │
│  - 모두 소진 → flash 로 fallback│
└─────────────────────────────┘
    │
    ▼
Google Generative Language API
```

`backend` 와 `worker` 는 외부 Google API 가 아니라 **내부 LiteLLM proxy** 만 호출한다.
proxy 가 multi-key 라우팅 + 429 자동 retry + 모델 fallback 까지 알아서 처리.

## Portainer Stack 환경변수 설정

`Stacks → harness-server → Environment variables` 에 다음 항목 추가/확인:

| 변수명 | 설명 | 예시 / 비고 |
|--------|------|-------------|
| `LITELLM_MASTER_KEY` | **필수.** backend↔proxy 인증 키 | `openssl rand -hex 32` 로 생성. 외부 노출 X |
| `GEMINI_API_KEY_1` | 첫 번째 Gemini 무료 키 | Google Cloud Console 에서 발급 |
| `GEMINI_API_KEY_2` | (선택) 두 번째 키 | 다른 Google 계정에서 발급 |
| `GEMINI_API_KEY_3` | (선택) 세 번째 키 | 다른 Google 계정에서 발급 |
| `LITELLM_PROXY_URL` | (선택) proxy URL 오버라이드 | 기본값 `http://litellm:4000` (컨테이너 내부) |
| `GEMINI_API_KEY` | (선택) proxy 없을 때 fallback 단일 키 | 로컬 dev 또는 비상용 |

**최소 설정**: `LITELLM_MASTER_KEY` + `GEMINI_API_KEY_1` 만 있어도 작동 (단일 키 모드).
무료 키를 더 발급할수록 쿼터 한도가 비례해서 증가.

## 무료 키 발급 방법

1. <https://aistudio.google.com/apikey> 접속 (Google 계정 별도 필요)
2. `Create API key` → 키 복사
3. 동일 절차로 계정 2~3개 더 만들어서 키 추가 발급
4. 발급한 키들을 `GEMINI_API_KEY_1`, `_2`, `_3` 에 각각 등록

> 무료 티어 한도는 모델·시점별로 다름 — Google AI Studio 의 [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) 페이지 참고.
> 키 N 개를 등록하면 RPM/RPD 한도가 N 배로 증가하는 것이 LiteLLM 운영 효과.

## 배포 후 검증

```bash
# 1) LiteLLM proxy 컨테이너 정상 기동 확인
curl http://YOUR_SERVER_IP:8000/health
# {"status":"healthy"}

# 2) backend 가 proxy 모드로 동작 중인지 로그 확인
#    Portainer → Containers → backend → Logs 에서 다음 라인 검색
#    "gemini_client: LiteLLM proxy 모드 (url=http://litellm:4000)"

# 3) 실제 파이프라인 1회 트리거 후 LiteLLM 로그 확인
#    Portainer → Containers → litellm → Logs
#    각 요청마다 어떤 키 / 모델이 라우팅됐는지 표시됨
```

## 트러블슈팅

### "AI 서비스 인증 오류" (gemini_auth)
- `LITELLM_MASTER_KEY` 가 backend / worker / litellm 컨테이너 셋 다 같은 값인지 확인
- proxy 가 시작 직후 `os.environ/LITELLM_MASTER_KEY` 못 읽으면 모든 요청 401

### "AI 사용량 한도 초과" (gemini_quota)
- 모든 키가 60초 cooldown 중 → 1분 후 재시도
- 발급된 키 모두 일일 한도 도달 → 새 키 추가 또는 다음날 reset
- LiteLLM 로그에서 `key cooldown active` 메시지로 상태 확인

### proxy 자체가 죽었을 때
- `backend` 의 `LITELLM_PROXY_URL` 환경변수를 비우면 → 자동으로 직접 호출 모드 fallback
- 임시방편: Portainer 에서 `LITELLM_PROXY_URL` 값 삭제 후 backend 재시작

## 보안

- LiteLLM proxy 의 포트 4000 은 **외부 노출 안 함** (docker-compose 에 `ports:` 미설정)
- backend↔proxy 통신은 docker default network 내부에서만 이뤄짐
- Admin UI 가 필요하면 SSH tunnel:
  ```
  ssh -L 4000:localhost:4000 root@YOUR_SERVER_IP
  # 그 후 브라우저에서 http://localhost:4000/ui 접속
  ```
