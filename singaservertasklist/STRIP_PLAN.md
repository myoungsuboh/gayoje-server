# STRIP_PLAN — gayoje-server (harness-server 카피본 정리)

> 이 레포는 `harness-server`를 통째 복사한 상태다. 가요제로 전환하려면 **도메인 코드를 도려내고**(이 문서) → **남긴 인프라를 adapt**(REUSE_FROM_HARNESS.md) → Phase 0 빌드.
> 경로는 레포 루트(`gayoje-server/`) 기준. 삭제 전 **계획 승인 → 단계 커밋**. import 깨지는 건 삭제 후 일괄 정리.

---

## 0. 🚨 보안 먼저 (push 전 필수)

- [ ] `.env` (실 시크릿) → `.gitignore` 확인 + `.env.example`(키 이름만)로 대체. **이미 push했으면 키 로테이션**
- [ ] `git remote -v` 가 **gayoje-server** 인지 확인 (harness `.git` 딸려왔으면 `git remote set-url`)
- [ ] `README.md`(40KB), `docker-compose.yml`, `Caddyfile` 의 harness 식별자 스크럽: IP `158.247.196.111`, 도메인 `api.harness-system.com`, 이메일 `kaki3010@naver.com`
- [ ] `.venv/`, `.pytest_cache/` → gitignore (커밋 금지)

## 1. 통째로 삭제할 디렉토리

```bash
git rm -r app/mcp                      # MCP (harness 전용)
git rm -r app/prompts                  # 37개 .md 전부 도메인(cps/prd/design/lint/notion/skill/guide/interview) → 빈 폴더로 재생성
git rm -r app/pipelines/cps_pipeline app/pipelines/design_pipeline app/pipelines/design_validator \
          app/pipelines/lint_pipeline app/pipelines/prd_lint app/pipelines/guide app/pipelines/interview
git rm -r evals/scenarios evals/snapshots
```

## 2. app/pipelines/ — `base.py`만 남기고 전부 삭제

✅ 유지: `app/pipelines/base.py`(JSON 추출 유틸 — adapt), `__init__.py`
🗑️ 삭제:
```bash
git rm app/pipelines/{api_spec_autofill_pipeline,cleanup_master_prd_pipeline,create_md_pipeline,delete_pipeline}.py
git rm app/pipelines/{fix_spec_pipeline,github_onboard_code_evidence,github_onboard_pipeline,lineage_pipeline}.py
git rm app/pipelines/{lint_evidence,notion_classify_pipeline,notion_normalize_pipeline}.py
git rm app/pipelines/{prd_autofix_pipeline,prd_cleanup,prd_fidelity,prd_fidelity_verify,prd_pipeline}.py
git rm app/pipelines/{skill_improve_pipeline,skill_recommend_pipeline,skill_trigger_fill_pipeline,spec_quality,story_link_autofill}.py
```

## 3. app/clients/ — 2개만 유지

✅ 유지: `gemini_client.py`(adapt), `neo4j_client.py`(copy)
🗑️ 삭제: `git rm app/clients/{gemini_audio,github_client,notion_client}.py`

## 4. app/core/ — 인프라만 유지, 도메인/외부연동 삭제

✅ 유지(REUSE 참조): `config.py`(adapt) `security.py` `session_registry.py` `token_blacklist.py` `token_encryption.py` `limiter.py` `request_context.py` `observability.py` `body_size_limit.py` `metrics.py` `concurrency.py` `master_lock.py` `prompt_render.py` `output_language.py` `google_oauth.py`(→카카오/네이버 템플릿) `wait_guard.py` `disposable_emails.py` `name_validation.py` `email.py`(adapt)
🗑️ 삭제:
```bash
git rm app/core/{archetype,billing_notifications,billing_tax,design_to_markdown,markdown_to_notion_blocks}.py
git rm app/core/{meeting_validation,notion_client,notion_oauth,notion_to_markdown,project_scope,proration}.py
git rm app/core/{quota,subscription,github_oauth}.py   # quota=harness가격전용(간단 게이팅으로 재작성), github_oauth=가요제는 카카오/네이버
```

## 5. app/service/ — 인증/감사/알림/공지 repo만 유지

✅ 유지(adapt): `auth_service.py` `audit_repository.py` `domain_indexes.py` `session_helper.py` `user_repository.py`(→Postgres SOR) `usage_repository.py` `notification_log_repository.py` `notice_repository.py`(i18n JSON 직렬화 패턴) `notice_translate.py` `webhook_event_repository.py`(→토스 웹훅) `ownership_repository.py`(pattern)
🗑️ 삭제:
```bash
git rm app/service/{admin_repository,graph_repository,infra_cost_repository,lineage_repository,lint_repository}.py
git rm app/service/{mcp_token_repository,meeting_upload_repository,notion_export_service,paddle_subscription_repository}.py
git rm app/service/{payment_repository,pricing_repository,query_repository,quota_config_repository,repo_repository}.py
git rm app/service/{revenue_repository,skill_library_repository,skill_repository,subscription_cron_repository}.py
git rm app/service/{subscription_repository,team_repository,vibe_repo_repository}.py
```

## 6. app/api/ — 앱셋업/인증/공지만 유지, 라우터 대량 삭제

✅ 유지(adapt): `main.py`(앱·미들웨어) `auth_routes.py`(56KB — 인증부만 발라내 adapt) `notice_routes.py` `setup_routes.py` `_schemas.py` `__init__.py`
🗑️ 삭제:
```bash
git rm app/api/{admin_billing_routes,admin_routes,create_md_routes,delete_routes,eval_score_routes}.py
git rm app/api/{gateway_compat_routes,gateway_routes,github_proxy_routes,inquiry_routes,interview_routes}.py
git rm app/api/{lineage_routes,lint_routes,mcp_token_routes,notion_routes,paddle_billing_routes,paddle_webhook_routes}.py
git rm app/api/{prd_lint_routes,pricing_routes,query_routes,quota_config_routes,revenue_routes}.py
git rm app/api/{skill_library_routes,skill_routes,team_routes,trace_routes,v2_routes,_quota_helpers}.py
```

## 7. app/queue/ — 인프라 유지, 잡 본문만 비우기

✅ 유지(adapt): `client.py` `settings.py` `worker.py` `extract_cache.py` `status_guard.py`
⚠️ `jobs.py`(82KB) — **삭제하지 말고 도메인 잡 함수 본문만 제거**하고 가요제 잡(수집/정규화/알림/제출)으로 재작성. 등록 토폴로지·재시도·DLQ 골격은 유지

## 8. evals/ — 점수화 골격만 유지

✅ 유지(pattern): `scorer.py` `run_eval.py` `dry_run.py` `run_real_llm.py` `__init__.py`
🗑️ 삭제: `git rm evals/fix_targets.py evals/README.md` (+ scenarios/snapshots는 §1에서 삭제)

## 9. 기타

- `app/schemas.py` — McpToken*/구독 DTO 제거 → 가요제 DTO로 (adapt, 삭제 X)
- `README.md`(40KB harness 인수인계) → gayoje README로 교체
- `requirements.txt` — notion/paddle/github 등 미사용 의존성 정리(빌드 깨지면 되돌리기 쉽게 마지막에)

## 10. 삭제 후 검증

- [ ] `app/api/main.py`·`app/queue/jobs.py`·`app/queue/worker.py`에서 삭제된 모듈 import 전부 제거
- [ ] `python run.py` 가 **최소 부팅**(라우터 없어도 /health 200)
- [ ] `pytest` — 도메인 테스트는 `tests/`에서 같이 삭제, 인프라 테스트만 남김
- [ ] 그 후 `singaservertasklist/TASKLIST_PHASE0.md` 로 빌드 시작

---
*유지/adapt 상세는 `REUSE_FROM_HARNESS.md` 섹션 1~3, 치환 규칙은 섹션 5 참조.*
