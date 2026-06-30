#!/usr/bin/env python3
"""
E2E 스모크 — 실서버에 "사용자 한 사이클"을 돌려 무음 실패를 잡는다 (P0-2).

[왜]
단위 테스트 3,096개가 그린이어도 운영에선 PRD 가 비어 있었다(2026-06 무음 누락 사고).
FakeNeo4j/단위 테스트는 LLM·Cypher·배치 체인의 무음 실패를 구조적으로 못 잡는다.
이 스크립트는 **FE 와 동일한 계약**(gateway postMeeting/getJobStatus/getCPS/getPRD/
createSpack/deleteProject)으로 실서버를 호출해 다음을 단언한다:

  1. 로그인
  2. V1 회의록 처리 → CPS master + PRD master 생성 (prd.mode='error' 강등 감지 포함)
  3. getCPS/getPRD 조회 경로 — 본문 비어있지 않음
  4. V2 회의록(명백한 신규 에픽) 처리 → **누적**: mode=incremental + PRD 본문 성장
     (= 'CPS 가득/PRD 빈'·'frozen 누적(D)' 증상의 직접 감지기)
  5. (선택) createSpack → SPACK/DDD/Architecture 생성·조회
  6. 정리: deleteProject (항상, finally)

[안전]
- 프로젝트명은 항상 `__smoke_<run>` — prefix 가 다르면 삭제 자체를 거부(assert_smoke_project).
- 시크릿(비밀번호/토큰)은 어떤 경로로도 출력하지 않는다.
- 토큰 소비: 1회 ≈ 70K (회의 2건 + design). 야간 1회 × 30일 ≈ 2.1M/월 → **Pro 등급
  스모크 전용 계정** 사용 권장 (Free 는 월 5건 한도라 불가).

[실행]
  SMOKE_BASE_URL=https://api.example.com \\
  SMOKE_EMAIL=smoke@example.com SMOKE_PASSWORD=... \\
  python scripts/e2e_smoke.py [--skip-design] [--keep-project]

종료코드: 0=전부 통과, 1=실패 있음(원인 표 출력), 2=설정 오류.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date as _date
from typing import Any, Dict, List, Optional

# ─── 안전 상수 ──────────────────────────────────────────────────
SMOKE_PROJECT_PREFIX = "__smoke"

# 폴링 — FE(asyncJob.js)와 동일 철학: 워밍업 짧게, 이후 일정 간격, 상한.
POLL_WARMUP_SEC = 2
POLL_INTERVAL_SEC = 5
JOB_MAX_WAIT_SEC = 15 * 60          # post_meeting 보통 2~6분 / design 비슷
HTTP_TIMEOUT_SEC = 30

# 본문 최소 길이 — 빈/스텁 결과를 "통과"로 오인하지 않게.
MIN_CPS_LEN = 500
MIN_PRD_LEN = 300
MIN_GROWTH = 200                    # V2 후 PRD 최소 성장(문자)


# ─── 골든 회의록 — 스펙이 명확히 뽑히도록 설계 (짧지만 Problem/Solution 농도 높게) ───
GOLDEN_MEETINGS: List[Dict[str, str]] = [
    {
        "version": "V1",
        "content": (
            "### [미팅 로그 V1] - 킥오프: 사내 재고관리 시스템 구축 범위 확정\n"
            "* **일시:** 2026-06-10 (10:00 ~ 11:00)\n"
            "* **참석자:** PO, PM, 물류팀장, 개발 리드\n"
            "* **물류팀장:** \"현재 재고를 엑셀 수기로 관리해서 입출고 누락이 월 평균 40건 발생하고, "
            "실사 때마다 이틀씩 걸립니다. 오류로 인한 결품/과잉 재고 비용이 분기당 약 1,200만 원입니다.\"\n"
            "* **PO:** \"이번 프로젝트의 목표는 바코드 기반 입출고로 수기 오류를 제거하고, 실시간 재고 "
            "현황을 누구나 볼 수 있게 하는 것입니다.\"\n"
            "* **개발 리드:** \"1차 범위를 확정합시다.\"\n\n"
            "#### 결정사항\n"
            "- [기능1] 품목 마스터 등록/수정 화면: 품목코드·품명·규격·안전재고 입력.\n"
            "- [기능2] 바코드 스캔 입고 처리: 스캐너로 품목 식별 후 수량 입력, 입고 이력 저장.\n"
            "- [기능3] 바코드 스캔 출고 처리: 출고 시 재고 차감, 마이너스 재고 차단.\n"
            "- [기능4] 실시간 재고 현황 대시보드: 품목별 현재고/안전재고 비교, 부족 품목 강조.\n"
            "- 로그인은 사내 SSO 연동, 권한은 조회/입출고/관리자 3단계.\n\n"
            "#### Action Items\n"
            "- [ ] PM: 품목 마스터 필드 정의서 작성 (6/12까지)\n"
            "- [ ] 개발 리드: 바코드 스캐너 기종 선정 (6/13까지)\n"
        ),
    },
    {
        "version": "V2",
        "content": (
            "### [미팅 로그 V2] - 2차: 재고 알림과 발주 워크플로 추가 결정\n"
            "* **일시:** 2026-06-11 (14:00 ~ 15:00)\n"
            "* **참석자:** PO, PM, 물류팀장, 개발 리드\n"
            "* **물류팀장:** \"대시보드만으로는 부족합니다. 안전재고 밑으로 떨어지는 걸 사람이 들어와서 "
            "봐야만 아는 구조면 결품은 계속 납니다.\"\n"
            "* **PO:** \"동의합니다. 이번 회의에서 신규 기능 두 가지를 확정합니다.\"\n\n"
            "#### 결정사항 (신규)\n"
            "- [신규 에픽 A] 재고 부족 알림: 품목 재고가 안전재고 이하로 떨어지면 담당자에게 "
            "이메일·슬랙으로 즉시 알림 발송. 품목별 알림 수신자 지정 화면 제공.\n"
            "- [신규 에픽 B] 발주 요청 워크플로: 부족 품목에서 바로 발주 요청 생성 → 팀장 승인 → "
            "발주 확정 → 입고 시 자동 매칭. 승인 대기 목록 화면과 발주 이력 화면 제공.\n"
            "- 알림 발송 실패 시 재시도 3회 후 관리자에게 별도 통지.\n\n"
            "#### Action Items\n"
            "- [ ] PM: 알림 수신자 매핑 정책 정리 (6/13까지)\n"
            "- [ ] 개발 리드: 슬랙 웹훅 연동 방식 검토 (6/14까지)\n"
        ),
    },
]


# ─── 순수 검증기 (tests/test_e2e_smoke_validators.py 로 고정) ───────────


def extract_task_id(data: Any) -> Optional[str]:
    """gateway 응답에서 task_id — FE asyncJob.extractTaskId 와 동일 계약."""
    if not data:
        return None
    inner = data.get("result", data) if isinstance(data, dict) else None
    obj = inner[0] if isinstance(inner, list) and inner else inner
    if not isinstance(obj, dict):
        return None
    return obj.get("task_id") or obj.get("taskId") or None


def read_status_info(data: Any) -> Optional[Dict[str, Any]]:
    """getJobStatus 응답 정규화 — FE asyncJob._readStatusInfo 와 동일 계약."""
    if not data:
        return None
    inner = data.get("result", data) if isinstance(data, dict) else None
    return inner[0] if isinstance(inner, list) and inner else inner


def judge_post_meeting(
    result: Any, expect_modes: Optional[set] = None
) -> List[str]:
    """postMeeting job result 판정 — 실패 사유 목록 반환(빈 목록=통과).

    잡는 것: (a) prd.mode='error' 강등(diagnostic 동봉), (b) CPS/PRD master id 누락
    (과거 무음 누락 그 자체), (c) 기대 mode 불일치(골든 입력 기준).
    """
    reasons: List[str] = []
    if not isinstance(result, dict):
        return [f"job result 가 dict 아님: {type(result).__name__}"]
    cps = result.get("cps") or {}
    prd = result.get("prd") or {}
    if not cps.get("master_cps_id"):
        reasons.append("cps.master_cps_id 비어있음 — CPS master 미생성")
    mode = prd.get("mode")
    if mode == "error":
        diag = (prd.get("diagnostic") or {}).get("error", "(원인 없음)")
        reasons.append(f"prd.mode=error 강등 — {diag}")
    elif mode not in ("first_run", "incremental", "no_changes"):
        reasons.append(f"prd.mode 알 수 없음: {mode!r}")
    elif mode != "no_changes" and not prd.get("master_prd_id"):
        reasons.append(f"prd.mode={mode} 인데 master_prd_id 비어있음 — PRD master 미생성")
    if expect_modes and mode not in expect_modes and mode != "error":
        reasons.append(f"기대 mode {sorted(expect_modes)} ≠ 실제 {mode!r}")
    return reasons


def content_of_row(row: Any) -> str:
    """getCPS/getPRD 행에서 본문 — FE 의 필드 fallback 과 동일 + \\n 복원."""
    if not isinstance(row, dict):
        return ""
    for f in ("prd_content", "cps_content", "output", "content", "full_markdown"):
        v = row.get(f)
        if isinstance(v, str) and v.strip():
            return v.replace("\\n", "\n")
    return ""


def accumulation_ok(prd_v1: str, prd_v2: str, v2_mode: str) -> List[str]:
    """V2 처리 후 누적 판정 — frozen 누적(D)·침식 감지. 빈 목록=통과."""
    reasons: List[str] = []
    if v2_mode != "incremental":
        reasons.append(
            f"V2 prd.mode={v2_mode!r} (기대 incremental) — 골든 V2 는 명백한 신규 에픽 포함, "
            "no_changes 면 누적 정지(D) 의심"
        )
    if prd_v2 == prd_v1:
        reasons.append("V2 후 PRD 본문이 V1 과 동일 — 누적 정지(frozen)")
    elif len(prd_v2) < len(prd_v1) - MIN_GROWTH:
        reasons.append(f"V2 후 PRD 가 크게 줄어듦({len(prd_v1)}→{len(prd_v2)}) — 침식 의심")
    elif len(prd_v2) < len(prd_v1) + MIN_GROWTH and v2_mode == "incremental":
        reasons.append(
            f"V2 후 PRD 성장 미미(+{len(prd_v2) - len(prd_v1)}자 < {MIN_GROWTH}) — 신규 에픽 미반영 의심"
        )
    return reasons


def assert_smoke_project(name: str) -> None:
    """스모크는 자기 prefix 프로젝트만 만들고 지운다 — 실프로젝트 오삭제 구조적 차단."""
    if not str(name).startswith(SMOKE_PROJECT_PREFIX):
        raise ValueError(
            f"스모크 프로젝트명은 반드시 {SMOKE_PROJECT_PREFIX!r} 로 시작해야 함: {name!r}"
        )


# ─── HTTP 러너 ─────────────────────────────────────────────────


class SmokeFailure(Exception):
    pass


class SmokeRunner:
    def __init__(self, base_url: str, email: str, password: str):
        import httpx  # 지연 import — 검증기 단위테스트는 httpx 불필요

        self.base = base_url.rstrip("/")
        self.gw = f"{self.base}/api/gateway"
        self._email = email
        self._password = password
        self.client = httpx.Client(timeout=HTTP_TIMEOUT_SEC)
        self.failures: List[str] = []

    # — 출력 (시크릿 절대 미포함) —
    @staticmethod
    def _ok(step: str, detail: str = "") -> None:
        print(f"  ✅ {step}" + (f" — {detail}" if detail else ""), flush=True)

    def _fail(self, step: str, reason: str) -> None:
        self.failures.append(f"{step}: {reason}")
        print(f"  ❌ {step} — {reason}", flush=True)

    @staticmethod
    def _body_snippet(resp) -> str:
        try:
            return resp.text[:300]
        except Exception:  # noqa: BLE001
            return "(본문 읽기 실패)"

    # — 단계 —
    def login(self) -> None:
        r = self.client.post(
            f"{self.base}/auth/login",
            json={"email": self._email, "password": self._password},
        )
        if r.status_code != 200:
            raise SmokeFailure(f"로그인 실패 HTTP {r.status_code}: {self._body_snippet(r)}")
        token = (r.json() or {}).get("access_token")
        if not token:
            raise SmokeFailure("로그인 응답에 access_token 없음")
        self.client.headers["Authorization"] = f"Bearer {token}"
        self._ok("로그인")

    def post_meeting(self, project: str, version: str, content: str) -> str:
        """FE plan.vue enqueueMeetingPost 와 동일 payload 로 enqueue → task_id."""
        payload = {
            "version": version,
            "date": _date.today().isoformat(),
            "meeting_content": content,
            "project_name": project,
            "previous_cps_id": f"doc_cps_{project}_{version.replace('.', '_')}",
        }
        r = self.client.post(f"{self.gw}/postMeeting", json=payload)
        if r.status_code == 402:
            raise SmokeFailure(
                "402 — 스모크 계정 쿼터 소진(토큰/미팅 한도). Pro 등급 전용 계정인지, "
                "월 사용량을 확인하세요."
            )
        if r.status_code == 409:
            raise SmokeFailure(
                f"409 — {project} {version} 이미 존재. 이전 스모크의 cleanup 실패 잔재 — "
                f"`{SMOKE_PROJECT_PREFIX}_*` 프로젝트를 수동 삭제 후 재실행."
            )
        if r.status_code != 200:
            raise SmokeFailure(f"postMeeting HTTP {r.status_code}: {self._body_snippet(r)}")
        task_id = extract_task_id(r.json())
        if not task_id:
            raise SmokeFailure(f"task_id 추출 실패: {self._body_snippet(r)}")
        return task_id

    def poll_job(self, task_id: str, label: str) -> Dict[str, Any]:
        """getJobStatus 폴링 — complete 시 result 반환, error/timeout 은 SmokeFailure."""
        deadline = time.monotonic() + JOB_MAX_WAIT_SEC
        started = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(POLL_WARMUP_SEC if time.monotonic() - started < 10 else POLL_INTERVAL_SEC)
            r = self.client.get(f"{self.gw}/getJobStatus", params={"task_id": task_id})
            if r.status_code in (401, 403):
                # 영구 오류 — 타임아웃까지 헛돌지 않고 즉시 실패 (토큰 만료/권한).
                raise SmokeFailure(f"{label} 폴링 HTTP {r.status_code} — 인증 만료/권한 문제")
            if r.status_code != 200:
                continue  # 일시 오류(5xx 등) — 다음 폴링
            info = read_status_info(r.json()) or {}
            status = info.get("status")
            if info.get("error"):
                raise SmokeFailure(f"{label} job error: {str(info['error'])[:300]}")
            if status == "complete":
                result = info.get("result")
                if isinstance(result, dict) and result.get("error"):
                    raise SmokeFailure(f"{label} result.error: {str(result['error'])[:300]}")
                elapsed = int(time.monotonic() - started)
                self._ok(f"{label} 완료", f"{elapsed}s")
                return result if isinstance(result, dict) else {}
            if status == "not_found":
                raise SmokeFailure(f"{label} task not_found — worker 미기동/큐 유실 의심")
        raise SmokeFailure(f"{label} 타임아웃({JOB_MAX_WAIT_SEC}s) — worker 정체/장애 의심")

    def fetch_rows(self, action: str, project: str) -> List[Dict[str, Any]]:
        r = self.client.get(f"{self.gw}/{action}", params={"projectName": project})
        if r.status_code != 200:
            raise SmokeFailure(f"{action} HTTP {r.status_code}: {self._body_snippet(r)}")
        data = r.json() or {}
        inner = data.get("result", data)
        rows = inner if isinstance(inner, list) else [inner]
        return [x for x in rows if isinstance(x, dict)]

    def fetch_content(self, action: str, project: str, min_len: int, step: str) -> str:
        rows = self.fetch_rows(action, project)
        text = content_of_row(rows[0]) if rows else ""
        if len(text) < min_len:
            self._fail(step, f"본문 {len(text)}자 < 최소 {min_len}자 (rows={len(rows)})")
        else:
            self._ok(step, f"{len(text)}자")
        return text

    def create_design(self, project: str) -> None:
        r = self.client.post(f"{self.gw}/createSpack", params={"projectName": project})
        if r.status_code != 200:
            raise SmokeFailure(f"createSpack HTTP {r.status_code}: {self._body_snippet(r)}")
        task_id = extract_task_id(r.json())
        if not task_id:
            raise SmokeFailure("createSpack task_id 추출 실패")
        self.poll_job(task_id, "Design 생성")
        for action in ("getSpack", "getDDD", "getArchitecture"):
            rows = self.fetch_rows(action, project)
            if not rows or len(json.dumps(rows[0], ensure_ascii=False)) < 100:
                self._fail(f"{action} 조회", f"rows={len(rows)} — Design 산출물 비어있음")
            else:
                self._ok(f"{action} 조회", f"rows={len(rows)}")

    def delete_project(self, project: str) -> None:
        assert_smoke_project(project)
        r = self.client.request(
            "DELETE", f"{self.gw}/deleteProject", json={"projectName": project}
        )
        if r.status_code != 200:
            raise SmokeFailure(f"deleteProject HTTP {r.status_code}: {self._body_snippet(r)}")
        self._ok("정리(deleteProject)")


# ─── 메인 시나리오 ──────────────────────────────────────────────


def run_smoke(base_url: str, email: str, password: str, *, skip_design: bool, keep_project: bool) -> int:
    # run_id + attempt — Actions 의 're-run failed jobs' 가 같은 RUN_ID 로 재실행돼도
    # (이전 cleanup 실패 잔재와) 409 충돌하지 않도록 attempt 를 포함.
    run_id = os.environ.get("GITHUB_RUN_ID") or str(int(time.time()))
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    project = f"{SMOKE_PROJECT_PREFIX}_{run_id}" + (f"_{attempt}" if attempt else "")
    assert_smoke_project(project)
    s = SmokeRunner(base_url, email, password)
    print(f"E2E SMOKE — base={base_url} project={project} design={'skip' if skip_design else 'on'}")

    try:
        # [1] 로그인
        s.login()

        # [2] V1 — CPS+PRD 생성
        t1 = s.post_meeting(project, "V1", GOLDEN_MEETINGS[0]["content"])
        r1 = s.poll_job(t1, "V1 postMeeting")
        reasons1 = judge_post_meeting(r1, expect_modes={"first_run"})
        for reason in reasons1:
            s._fail("V1 결과 판정", reason)
        if not reasons1:
            s._ok("V1 결과 판정", f"cps={r1.get('cps', {}).get('mode')} prd={r1.get('prd', {}).get('mode')}")

        # [3] 조회 경로 — 생성됐다는 주장과 읽히는 현실이 일치하는지
        s.fetch_content("getCPS", project, MIN_CPS_LEN, "getCPS 조회(V1)")
        prd_v1 = s.fetch_content("getPRD", project, MIN_PRD_LEN, "getPRD 조회(V1)")

        # [4] V2 — 누적 (frozen/D 감지)
        t2 = s.post_meeting(project, "V2", GOLDEN_MEETINGS[1]["content"])
        r2 = s.poll_job(t2, "V2 postMeeting")
        for reason in judge_post_meeting(r2):
            s._fail("V2 결과 판정", reason)
        prd_v2 = s.fetch_content("getPRD", project, MIN_PRD_LEN, "getPRD 조회(V2)")
        v2_mode = (r2.get("prd") or {}).get("mode") or ""
        acc = accumulation_ok(prd_v1, prd_v2, v2_mode)
        if acc:
            for reason in acc:
                s._fail("누적 판정", reason)
        else:
            s._ok("누적 판정", f"PRD {len(prd_v1)}→{len(prd_v2)}자, mode={v2_mode}")

        # [5] Design (선택)
        if not skip_design:
            s.create_design(project)

    except SmokeFailure as e:
        s._fail("중단", str(e))
    except Exception as e:  # noqa: BLE001 — 예기치 못한 오류도 FAIL 로 수렴
        s._fail("예기치 못한 오류", f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        # [6] 정리 — 어떤 경우에도 시도. 실패 시 수동 삭제 안내 + 실패 처리.
        if keep_project:
            print(f"  ⚠ --keep-project — {project} 보존됨 (수동 삭제 필요)")
        else:
            try:
                s.delete_project(project)
            except Exception as e:  # noqa: BLE001
                s._fail("정리 실패", f"{str(e)[:200]} — 수동으로 {project} 삭제 필요")

    print()
    if s.failures:
        print(f"SMOKE FAIL — {len(s.failures)}건:")
        for f in s.failures:
            print(f"  • {f}")
        return 1
    print("SMOKE PASS — 사용자 한 사이클(CPS→PRD→누적" + ("" if skip_design else "→Design") + ") 정상.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Harness E2E 스모크 (실서버 한 사이클)")
    p.add_argument("--skip-design", action="store_true", help="Design(createSpack) 단계 생략")
    p.add_argument("--keep-project", action="store_true", help="종료 후 스모크 프로젝트 보존(디버깅)")
    args = p.parse_args(argv)

    base = os.environ.get("SMOKE_BASE_URL", "").strip()
    email = os.environ.get("SMOKE_EMAIL", "").strip()
    password = os.environ.get("SMOKE_PASSWORD", "")
    if not base or not email or not password:
        print("설정 오류: SMOKE_BASE_URL / SMOKE_EMAIL / SMOKE_PASSWORD 환경변수가 필요합니다.", file=sys.stderr)
        return 2
    return run_smoke(base, email, password, skip_design=args.skip_design, keep_project=args.keep_project)


if __name__ == "__main__":
    sys.exit(main())
