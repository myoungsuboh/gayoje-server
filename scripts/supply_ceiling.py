"""공급 상한 검증 — 표준공연(15013106)+문화축제(15013104) 2 API 전량 스윕 분석.

산출: (1)택소노미 확장 distinct + 키워드 기여 + Δ vs 21, (2)회차·연례성, (3)17시도 갭맵.
신규 API/보드 없음(2종 재사용). 정직 UA·robots 는 어댑터 http 계층에서 유지.
    실행: PYTHONPATH=. python scripts/supply_ceiling.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app.api.v1.ingestion.adapters.base import http_fetch_records, parse_date
from app.api.v1.ingestion.adapters.cultural_festival import CulturalFestivalAdapter
from app.api.v1.ingestion.adapters.standard_performance import StandardPerformanceAdapter
from app.api.v1.ingestion.taxonomy import (
    SIDO_17,
    classify,
    dedup_key,
    extract_round,
    extract_sido,
    normalize_name,
)
from app.core.config import settings

CACHE = Path("scripts/.supply_cache.json")  # 재실행 가속용(원시 레코드 캐시, gitignore 권장)
ADAPTERS = [StandardPerformanceAdapter(), CulturalFestivalAdapter()]


async def fetch_all(force: bool = False) -> list[dict]:
    """2 API 전량(title/address/host 만 추출) — 캐시 있으면 재사용."""
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    key = settings.data_go_kr_service_keys[0]
    rows: list[dict] = []
    for adapter in ADAPTERS:
        for pg in range(1, 25):
            # 어댑터 대신 http_fetch_records 직접 호출. 대용량(1000행)이 지연 시 timeout 걸려
            # 500행으로 낮추고 timeout 넉넉히(payload 작을수록 응답 빠름). 실패 페이지는 스킵.
            try:
                recs = await http_fetch_records(
                    key, adapter.DEFAULT_BASE_URL,
                    num_of_rows=500, page_no=pg,
                    extra_params={"type": "json"},
                    timeout_sec=40, max_retries=2,
                )
            except Exception as e:  # noqa: BLE001 — 느린/실패 페이지는 스킵(부분 집계 허용)
                print(f"  [fetch] {adapter.SOURCE_KEY} p{pg} 실패 {type(e).__name__} — 스킵", flush=True)
                continue
            print(f"  [fetch] {adapter.SOURCE_KEY} p{pg}: {len(recs)}건", flush=True)
            if not recs:
                break
            for r in recs:
                rows.append({
                    "title": adapter.field(r, "title"),
                    "address": adapter.field(r, "address"),
                    "host": adapter.field(r, "host"),
                    "start": adapter.field(r, "start"),
                    "end": adapter.field(r, "end"),
                    "source": adapter.SOURCE_KEY,
                })
            await asyncio.sleep(0.2)
    # 안전장치: fetch 대량 실패(예: API 지연)로 rows 가 빈약하면 기존 캐시를 덮지 않는다.
    if len(rows) < 1000 and CACHE.exists():
        print(f"  [fetch] 수집 {len(rows)}건뿐 — 기존 캐시 보존(덮어쓰기 안 함)", flush=True)
        return json.loads(CACHE.read_text(encoding="utf-8"))
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def analyze(rows: list[dict]) -> None:
    # --- 분류(고재현율) ---
    matched = []
    for r in rows:
        bucket = classify(r["title"])
        if bucket:
            matched.append({**r, "bucket": bucket, "round": extract_round(r["title"]),
                            "sido": extract_sido(r["address"])})
    total_rows = len(rows)

    # --- dedup (이름+지역+주최) 및 (이름+지역) 두 기준 ---
    by_full: dict[tuple, dict] = {}
    for m in matched:
        k = dedup_key(m["title"], m["address"], m["host"])
        # 회차 큰 레코드를 대표로(연례성 판단 유리)
        if k not in by_full or (m["round"] or 0) > (by_full[k]["round"] or 0):
            by_full[k] = m
    by_namesido: dict[tuple, dict] = {}
    for m in matched:
        k = (normalize_name(m["title"]), m["sido"] or "?")
        if k not in by_namesido or (m["round"] or 0) > (by_namesido[k]["round"] or 0):
            by_namesido[k] = m

    distinct = list(by_full.values())
    n_full, n_ns = len(by_full), len(by_namesido)

    print("=" * 72)
    print("[1] 택소노미 확장 스윕 — 공급 distinct")
    print("=" * 72)
    print(f"  스캔 원시 레코드: {total_rows} (표준공연+문화축제)")
    print(f"  키워드 매칭(중복 전): {len(matched)}")
    print(f"  distinct (이름+지역+주최): {n_full}")
    print(f"  distinct (이름+지역):     {n_ns}")
    print(f"  기존 is_gayoje distinct 21 대비 Δ: +{n_full - 21} (이름+지역+주최 기준)")

    # 장르 분리 — 대중가요/노래/동요/트로트(가요제-proper) vs 합창·가곡(장르 인접).
    def _is_choir(b: str) -> bool:
        return ("합창" in b) or ("중창" in b) or ("가곡" in b)

    core = [m for m in distinct if not _is_choir(m["bucket"])]
    choir = [m for m in distinct if _is_choir(m["bucket"])]
    print(f"  ├ 가요제-proper(대중가요·노래·동요·트로트): {len(core)}  (vs is_gayoje 21 → Δ+{len(core) - 21})")
    print(f"  └ 합창·가곡(장르 인접, 대중가요 아님):     {len(choir)}")

    print("\n  가요제-proper distinct 목록:")
    for m in sorted(core, key=lambda x: -(x["round"] or 0)):
        rd = f"제{m['round']}회 " if m["round"] else ""
        print(f"    · {rd}{(m['title'] or '')[:40]} | {m['sido']} [{m['bucket']}]")

    # 키워드 버킷별 기여(distinct 기준)
    bucket_ct = Counter(m["bucket"] for m in distinct)
    print("\n  키워드 버킷별 기여(distinct):")
    for b, c in bucket_ct.most_common():
        print(f"    {c:3d}  {b}")

    # 신규(21 밖) 표본 — is_gayoje 로는 안 걸렸을 광의 버킷
    print("\n  광의(가창+경연/합창/오디션 등) 대표 표본:")
    for m in distinct:
        if m["bucket"].startswith("가창+경연") or "합창" in m["bucket"] or "오디션" in m["bucket"]:
            print(f"    · {(m['title'] or '')[:44]} | {m['sido']}")

    print("\n" + "=" * 72)
    print("[2] 회차·연례성 분석 (제N회)")
    print("=" * 72)
    annual = [m for m in distinct if (m["round"] or 0) >= 2]
    onetime = [m for m in distinct if (m["round"] or 0) < 2]
    print(f"  연례 프랜차이즈(제2회 이상 확인): {len(annual)}개")
    print(f"  1회성/불명(제1회 또는 회차 없음): {len(onetime)}개")
    print("\n  최고 회차 TOP 10:")
    for m in sorted(annual, key=lambda x: -(x["round"] or 0))[:10]:
        print(f"    제{m['round']:>3}회  {(m['title'] or '')[:40]} | {m['sido']}")

    print("\n" + "=" * 72)
    print("[3] 17시도 커버리지 갭맵")
    print("=" * 72)
    per_sido = Counter(m["sido"] for m in distinct if m["sido"])
    unknown = sum(1 for m in distinct if not m["sido"])
    print(f"  {'시도':<6}{'distinct':>9}")
    for s in SIDO_17:
        flag = "  ← 0건" if per_sido.get(s, 0) == 0 else ""
        print(f"  {s:<6}{per_sido.get(s, 0):>9}{flag}")
    if unknown:
        print(f"  (시도미상){unknown:>7}")
    zero = [s for s in SIDO_17 if per_sido.get(s, 0) == 0]
    print(f"\n  공공데이터 0커버리지 시도({len(zero)}): {', '.join(zero) or '없음'}")

    print("\n" + "=" * 72)
    print("[4] 개최월 분포 (가요제-proper) — 21이 구조적 천장인지 7월 스냅샷 착시인지")
    print("=" * 72)

    def _md(s):
        d = parse_date(s)
        return (d.month, d.year) if d else (None, None)

    # 개최월은 '실제 개최 인스턴스'(dedup 전 core)로 계절성 파악.
    core_inst = [m for m in matched if not _is_choir(m["bucket"])]

    def _eclass(title: str) -> str:
        t = (title or "").replace(" ", "")
        if "전국노래자랑" in t:
            return "KBS전국노래자랑"
        if any(k in t for k in ("축제", "페스티벌", "양파마늘", "글로벌", "강변",
                                "서천", "마늘", "불교", "가톨릭", "윈터아트")):
            return "지역축제연계"
        return "단독개최"

    months: Counter = Counter()
    years: Counter = Counter()
    no_date = 0
    for m in core_inst:
        mo, yr = _md(m.get("start"))
        if mo:
            months[mo] += 1
            years[yr] += 1
        else:
            no_date += 1

    dated = sum(months.values())
    print(f"  대상 인스턴스: {len(core_inst)} (날짜 있음 {dated}, 날짜 없음 {no_date})")
    print("\n  개최월 분포:")
    peak = max(months.values()) if months else 0
    for mo in range(1, 13):
        c = months.get(mo, 0)
        bar = "█" * c
        mark = " ◀PEAK" if c == peak and c > 0 else ""
        print(f"    {mo:2d}월 {c:2d} {bar}{mark}")
    autumn = sum(months.get(x, 0) for x in (9, 10, 11))
    print(f"\n  가을(9~11월): {autumn}/{dated} = {round(100*autumn/dated) if dated else 0}%")
    print(f"  7월(현재): {months.get(7, 0)}")

    print("\n  개최년도 분포(단일 스냅샷 vs 다년 누적 판별):")
    for yr in sorted(years):
        print(f"    {yr}: {years[yr]}")

    print("\n  유형 분리(개최 성격):")
    cls = Counter(_eclass(m["title"]) for m in core_inst)
    for label in ("KBS전국노래자랑", "지역축제연계", "단독개최"):
        insts = [m for m in core_inst if _eclass(m["title"]) == label]
        mos = sorted({_md(m.get("start"))[0] for m in insts if _md(m.get("start"))[0]})
        print(f"    {label:14s} {cls.get(label, 0):2d}건  개최월={mos or '연중/미상'}")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    force = "--force" in sys.argv
    rows = await fetch_all(force=force)
    analyze(rows)


if __name__ == "__main__":
    asyncio.run(main())
