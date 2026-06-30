#!/usr/bin/env python3
"""
부하 테스트 — 동시 N명이 무거운 생성(createDesign/postMeeting 등)을 칠 때
백엔드/워커/Neo4j/LLM 이 견디는지 측정.

⚠️ 운영(production) 에 쏘지 마세요. 반드시 staging + 테스트 Gemini 키로.
   실제 LLM 토큰을 소비합니다(비용 발생).

준비물:
  - 테스트 사용자 JWT 토큰들(한 줄에 하나) 파일. 동시성 = 서로 다른 사용자/프로젝트라야
    PROJECT_BUSY 직렬화 가드에 안 걸립니다. (사용자별 1프로젝트 시나리오와 일치)
  - 각 사용자 프로젝트에 회의록/CPS 가 이미 있어야 createDesign 이 의미 있음.
    (없으면 postMeeting 부터 체인하거나, 시드 스크립트로 미리 준비)

사용 예:
  BASE=https://staging-api.example.com TOKENS=tokens.txt \
  python scripts/loadtest_generate.py --concurrency 75 \
      --endpoint /api/gateway/createDesign \
      --payload '{"projectName":"{project}"}' \
      --poll /api/gateway/getJobStatus --poll-param taskId \
      --project-prefix loadtest_

측정 출력: enqueue 성공/429/에러, job 완료/실패, end-to-end p50/p95/max, 처리량.
"""
from __future__ import annotations
import argparse, asyncio, json, os, time, statistics
import httpx

def percentile(xs, p):
    if not xs: return 0.0
    xs = sorted(xs); k = (len(xs)-1)*p/100
    f = int(k); c = min(f+1, len(xs)-1)
    return xs[f] + (xs[c]-xs[f])*(k-f)

async def one_user(client, base, token, endpoint, payload_tmpl, poll, poll_param,
                   project, poll_interval, timeout_s, stats):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = json.loads(payload_tmpl.replace("{project}", project))
    t0 = time.monotonic()
    # 1) enqueue
    try:
        r = await client.post(base+endpoint, headers=headers, json=payload)
    except Exception as e:
        stats["enqueue_error"].append(str(e)[:80]); return
    if r.status_code == 429:
        stats["enqueue_429"] += 1; return
    if r.status_code >= 400:
        stats["enqueue_error"].append(f"{r.status_code}:{r.text[:80]}"); return
    body = r.json()
    task_id = (body.get("result") or body).get("task_id")
    if not task_id:
        stats["enqueue_error"].append(f"no task_id: {str(body)[:80]}"); return
    stats["enqueue_ok"] += 1
    # 2) poll
    deadline = t0 + timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            pr = await client.get(base+poll, headers=headers, params={poll_param: task_id})
        except Exception:
            continue
        if pr.status_code == 429:
            stats["poll_429"] += 1; continue
        if pr.status_code >= 400:
            continue
        st = (pr.json().get("result") or pr.json())
        status = (st.get("status") or "").lower()
        if status in ("complete", "completed", "done", "success", "succeeded"):
            stats["latency"].append(time.monotonic()-t0); stats["job_ok"] += 1; return
        if status in ("failed", "error"):
            stats["job_fail"].append(str(st.get("error") or st.get("reason"))[:80]); return
    stats["job_timeout"] += 1

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=75)
    ap.add_argument("--endpoint", default="/api/gateway/createDesign")
    ap.add_argument("--payload", default='{"projectName":"{project}"}')
    ap.add_argument("--poll", default="/api/gateway/getJobStatus")
    ap.add_argument("--poll-param", default="taskId")
    ap.add_argument("--project-prefix", default="loadtest_")
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=1200.0)
    args = ap.parse_args()

    base = os.environ["BASE"].rstrip("/")
    tokens = [l.strip() for l in open(os.environ["TOKENS"]) if l.strip()]
    if not tokens:
        raise SystemExit("TOKENS 파일에 JWT 가 없습니다")
    stats = {"enqueue_ok":0,"enqueue_429":0,"enqueue_error":[],"poll_429":0,
             "job_ok":0,"job_fail":[],"job_timeout":0,"latency":[]}
    print(f"발사: 동시 {args.concurrency} → {args.endpoint} (토큰 {len(tokens)}개 round-robin)")
    t_start = time.monotonic()
    limits = httpx.Limits(max_connections=args.concurrency+20, max_keepalive_connections=50)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        tasks = [
            one_user(client, base, tokens[i % len(tokens)], args.endpoint, args.payload,
                     args.poll, args.poll_param, f"{args.project_prefix}{i}",
                     args.poll_interval, args.timeout, stats)
            for i in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)
    elapsed = time.monotonic()-t_start
    lat = stats["latency"]
    print("\n===== 결과 =====")
    print(f"총 소요         : {elapsed:.0f}s")
    print(f"enqueue 성공/429/에러: {stats['enqueue_ok']} / {stats['enqueue_429']} / {len(stats['enqueue_error'])}")
    print(f"poll 429        : {stats['poll_429']}")
    print(f"job 완료/실패/타임아웃: {stats['job_ok']} / {len(stats['job_fail'])} / {stats['job_timeout']}")
    if lat:
        print(f"완료 지연(s) p50/p95/max: {percentile(lat,50):.0f} / {percentile(lat,95):.0f} / {max(lat):.0f}")
        print(f"처리량          : {stats['job_ok']/elapsed*60:.1f} 완료/분")
    if stats["enqueue_error"][:3]: print("enqueue 에러 샘플:", stats["enqueue_error"][:3])
    if stats["job_fail"][:5]:      print("job 실패 샘플   :", stats["job_fail"][:5])
    print("\n※ 429(enqueue/poll) 또는 job 실패에 'rate'·'429'·'quota' 가 보이면 Gemini tier 한도입니다.")

if __name__ == "__main__":
    asyncio.run(main())
