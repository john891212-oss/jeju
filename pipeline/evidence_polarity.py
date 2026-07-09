# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 근거 극성 분리 — 블로그/카카오리뷰/유튜브댓글 → 카페별 pro/con 인용 (gpt-5-mini)

입력:  data/raw/네이버 크롤링.jsonl (+ 재검색)   — 블로그 스니펫 원문
       data/processed/카카오리뷰.jsonl           — 실방문 리뷰 (별점 = 극성 프리셋)
       data/raw/유튜브 댓글 크롤링.jsonl          — 쇼츠 댓글 (보수 귀속)
       data/processed/유튜브 정제.json            — video→카페 귀속 판단용
출력:  data/processed/근거극성.jsonl
       { spot_name, source, polarity, aspect, quote, full_text, ref, likes, verified }
       data/processed/근거극성_집계.json          — 카페×aspect별 pro/con 카운트 + 태그 감사
키:    .env OPENAI_KEY

확정 결정 (7/9 커맨드 센터 논의):
  1. 혼합 문장은 절 스팬 분리(B안) — 스팬은 원문의 "연속 부분문자열", 코드가 검증
  2. aspect = 태그 사전 어휘 + {맛, 가격, 서비스, 청결} (aspect=태그 통일 → 태그 감사 겸용)
  3. 카카오 별점 프리셋: star<=2 → con 우선 / star>=4 → pro 우선 (LLM은 스팬·aspect 담당)
  4. 댓글 보수 귀속: 단일 카페 영상이거나 댓글 본문에 카페명 명시 — 애매하면 폐기
     (con 오귀속은 무고한 카페를 때린다. 신뢰가 컨셉이므로 버리는 쪽이 정답)
  5. 검증 실패 스팬은 폐기+카운트, 실패 카페는 스킵+로그, 배치 중단 금지

사용:
  python pipeline/evidence_polarity.py blog --pilot 10       # 블로그 파일럿
  python pipeline/evidence_polarity.py kakao --pilot 10      # 카카오 파일럿
  python pipeline/evidence_polarity.py comments --pilot 30   # 댓글 파일럿 = 수율 실증(HANDOFF) 겸용
  python pipeline/evidence_polarity.py all                   # 전량 (이어달리기 지원)

원칙 (기존 계승): reasoning_effort=minimal + max_completion_tokens=4000 / except는 반드시 출력
표본은 random.sample / 근거는 원문 인용(결정 26) / 반응 텍스트 임베딩 금지(결정 20) — 이 산출물은 표시층 전용
"""
import argparse
import json
import os
import random
import re
import sys
import time
from collections import defaultdict

from openai import OpenAI, RateLimitError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
RAW_BLOG = P("data", "raw", "네이버 크롤링.jsonl")
RAW_RESCUE = P("data", "raw", "네이버 재검색 크롤링.jsonl")
KAKAO = P("data", "processed", "카카오리뷰.jsonl")
RAW_COMMENTS = P("data", "raw", "유튜브 댓글 크롤링.jsonl")
YT_REFINED = P("data", "processed", "유튜브 정제.json")
NAVER_REFINED = P("data", "processed", "네이버 정제.jsonl")
OUT = P("data", "processed", "근거극성.jsonl")
AGG = P("data", "processed", "근거극성_집계.json")

env = {}
for line in open(P(".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])

CAFE_TAGS = "오션뷰, 산방산뷰, 숲뷰, 노을, 감성, 조용함, 대형, 베이커리, 브런치, 디저트, 애견동반, 노키즈존, 키즈친화, 통창, 야외석, 루프탑, 주차편함, 웨이팅, 신상, 로컬"
EXTRA_ASPECTS = "맛, 가격, 서비스, 청결"
ASPECTS = {t.strip() for t in (CAFE_TAGS + ", " + EXTRA_ASPECTS).split(",")}

TAG = re.compile(r"<[^>]+>")


def clean(s):
    return TAG.sub("", s or "").replace("&quot;", '"').replace("&amp;", "&").strip()


def norm(s):
    s = clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())


def load_jsonl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


SYSTEM = f"""카페에 대한 텍스트 조각(스니펫/리뷰/댓글)을 읽고, 좋았던 점(pro)·아쉬운 점(con)·중립 정보(info)를
"원문 그대로의 연속 구간(스팬)"으로 잘라 json으로만 답해.

## 절대 규칙
- quote는 반드시 해당 조각 원문에 "그대로 이어져서" 존재하는 부분문자열이어야 한다.
  요약·의역·오탈자 수정 금지. 이모지·기호도 원문 그대로. (코드가 검증해서 어긋나면 폐기됨)
- 혼합 문장("뷰는 좋은데 주차는 헬")은 절 단위 스팬으로 나눠 각각 분류한다.
- polarity: pro=긍정 경험/장점, con=아쉬움·불편·부정 경험, info=중립 사실(가격, 영업시간 등)
- aspect: 다음 중 하나 — {CAFE_TAGS}, {EXTRA_ASPECTS}. 정말 없으면 "기타"
- 카페와 무관한 내용은 제외: 영상 자체 감상, 유튜버 칭찬, 여행 일반론, 광고 문구
- 다음도 반드시 제외 (파일럿에서 실측된 오염 유형):
  · 동행·타인에 대한 불평 ("같이 가자더니 말도 없이") — 카페 얘기가 아님
  · 소유주·연예인 가십 ("OO은 얼굴마담", "연예인 걱정 할 필요 없음")
  · 반어 밈·시기 농담 ("가게 차리지 말라고 경고했다" = 맛있다는 농담일 수 있음)
  · 문맥 없는 파편 ("화가남" 단독)
- 폐업·휴업·존속 추측/목격("폐건물인 줄", "일년 뒤 어찌될지", "짓다 만 듯")은
  items에 넣지 말고 closure_hints로 분리하라. 이것은 극성이 아니라 실존 신호다.
- 확실하지 않으면 제외. 조각 하나에서 스팬 최대 3개.

## 출력 (json만)
{{"items": [{{"i": 1, "quote": "원문 스팬", "polarity": "pro|con|info", "aspect": "..."}}],
"closure_hints": [{{"i": 2, "quote": "원문 스팬"}}]}}
i는 입력 조각 번호. closure_hints는 없으면 빈 배열."""


def call_llm(user_msg, label, max_retry=5):
    for attempt in range(max_retry):
        try:
            resp = client.chat.completions.create(
                model="gpt-5-mini",
                response_format={"type": "json_object"},
                max_completion_tokens=4000,
                reasoning_effort="minimal",
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user_msg}],
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                print(f"  ⚠ 빈 응답 [{label}] finish={resp.choices[0].finish_reason}", flush=True)
                return None
            return json.loads(content)
        except RateLimitError:
            wait = 2 ** (attempt + 1)
            print(f"  ⏳ rate limit [{label}] {wait}s 대기", flush=True)
            time.sleep(wait)
        except Exception as e:
            print(f"  ⚠ 호출 실패 [{label}] {type(e).__name__}: {e}", flush=True)
            time.sleep(2)
    return None


def verify_and_emit(spot_name, source, ref, pieces, result, likes_map=None, preset_map=None, stats=None):
    """LLM 결과를 원문 대조 검증 후 레코드로. pieces: {i: full_text}"""
    records = []
    for it in (result or {}).get("items", []):
        try:
            i = int(it.get("i", -1))
            quote = (it.get("quote") or "").strip()
            polarity = it.get("polarity", "")
            aspect = it.get("aspect", "기타")
        except Exception:
            stats["malformed"] += 1
            continue
        full = pieces.get(i)
        if not full or not quote or polarity not in ("pro", "con", "info"):
            stats["malformed"] += 1
            continue
        if quote not in full:            # 결정 26: 부분문자열 검증 — 실패는 폐기
            stats["unverified"] += 1
            continue
        if aspect not in ASPECTS:
            aspect = "기타"
            stats["aspect_etc"] += 1
        if preset_map and i in preset_map and polarity != "info":
            # 카카오 별점 프리셋과 LLM 판정 충돌 시: 별점이 명백하면(1~2/4~5) 프리셋 우선, 로그
            preset = preset_map[i]
            if preset and polarity != preset:
                stats["preset_override"] += 1
                polarity = preset
        records.append({
            "spot_name": spot_name, "source": source, "polarity": polarity,
            "aspect": aspect, "aspect_raw": (it.get("aspect") or "").strip(),  # 어휘 확장 근거 보존
            "quote": quote, "full_text": full,
            "ref": ref.get(i) if isinstance(ref, dict) else ref,
            "likes": (likes_map or {}).get(i, 0), "verified": True,
        })
    stats["emitted"] += len(records)
    return records


CLOSURE_Q = P("data", "processed", "폐업의심큐.jsonl")


def emit_closure(spot_name, source, ref, pieces, result, stats):
    """폐업·존속 추측은 evidence가 아니라 실존 신호 — 별도 큐로 라우팅 (검수 후 하드코딩 재료)"""
    rows = []
    for it in (result or {}).get("closure_hints", []):
        try:
            i = int(it.get("i", -1))
            quote = (it.get("quote") or "").strip()
        except Exception:
            continue
        full = pieces.get(i)
        if not full or not quote or quote not in full:   # 여기도 원문 검증
            continue
        rows.append({"spot_name": spot_name, "source": source, "quote": quote,
                     "full_text": full, "ref": ref.get(i) if isinstance(ref, dict) else ref})
    if rows:
        with open(CLOSURE_Q, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        stats["closure_hints"] += len(rows)
    return rows


# ---------- 소스 1: 블로그 스니펫 ----------

def build_blog_cafes():
    rescue = {r["spot_name"]: r for r in load_jsonl(RAW_RESCUE) if "blog" in r}
    cafes = {}
    for r in load_jsonl(RAW_BLOG):
        name = r["spot_name"]
        key = norm(name)
        if name in rescue:
            rr = rescue[name]
            r = {**r, "blog": rr["blog"]}
            key = norm(rr.get("cleaned_name") or name)
        items = r.get("blog", {}).get("items", [])
        valid = [it for it in items
                 if key and key in norm(it.get("title", "") + it.get("description", ""))
                 and it.get("postdate", "") >= "20240101"]
        if not valid:
            continue
        valid.sort(key=lambda it: it.get("postdate", ""), reverse=True)
        cafes[name] = valid[:15]
    return cafes


def run_blog(names, done, fout, stats):
    cafes = build_blog_cafes()
    targets = [n for n in (names or cafes.keys()) if n in cafes and (n, "blog") not in done]
    print(f"[blog] 대상 {len(targets)}곳", flush=True)
    for idx, name in enumerate(targets, 1):
        pieces, refs = {}, {}
        for j, it in enumerate(cafes[name], 1):
            pieces[j] = clean(it.get("description", ""))
            refs[j] = it.get("link", "")
        body = "\n".join(f"[{j}] {t}" for j, t in pieces.items() if t)
        msg = f"카페명: {name}\n출처: 네이버 블로그 검색 스니펫 (문장이 잘려 있을 수 있음 — 잘린 부분 추측 금지)\n\n{body}"
        result = call_llm(msg, f"blog:{name}")
        if result is None:
            stats["failed"] += 1
        emit_closure(name, "blog", refs, pieces, result, stats)
        for rec in verify_and_emit(name, "blog", refs, pieces, result, stats=stats):
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        if idx % 20 == 0:
            print(f"  … {idx}/{len(targets)}", flush=True)


# ---------- 소스 2: 카카오 리뷰 (별점 프리셋) ----------

def run_kakao(names, done, fout, stats):
    rows = [r for r in load_jsonl(KAKAO) if r.get("reviews")]
    targets = [r for r in rows if (not names or r["spot_name"] in names)
               and (r["spot_name"], "kakao") not in done]
    print(f"[kakao] 대상 {len(targets)}곳", flush=True)
    for idx, r in enumerate(targets, 1):
        name = r["spot_name"]
        pieces, likes, preset = {}, {}, {}
        lines = []
        for j, rv in enumerate(r["reviews"][:8], 1):
            text = (rv.get("text") or "").strip()
            if len(text) < 5:
                continue
            star = rv.get("star")
            pieces[j] = text
            preset[j] = "con" if (star is not None and star <= 2) else ("pro" if (star is not None and star >= 4) else None)
            lines.append(f"[{j}] (별점 {star}) {text}")
        if not pieces:
            continue
        msg = (f"카페명: {name}\n출처: 카카오맵 실방문 리뷰 (별점 참고: 1~2점 리뷰는 con, 4~5점은 pro 성향)\n\n"
               + "\n".join(lines))
        result = call_llm(msg, f"kakao:{name}")
        if result is None:
            stats["failed"] += 1
        kref = {j: f"kakao:{r.get('place_id','')}" for j in pieces}
        emit_closure(name, "kakao", kref, pieces, result, stats)
        for rec in verify_and_emit(name, "kakao", kref,
                                   pieces, result, preset_map=preset, stats=stats):
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        if idx % 20 == 0:
            print(f"  … {idx}/{len(targets)}", flush=True)


# ---------- 소스 3: 유튜브 댓글 (보수 귀속) ----------

def build_video_spots():
    """video_id → 그 영상에 언급된 카페명 집합 (유튜브 정제.json)"""
    v2s = defaultdict(set)
    try:
        for r in json.load(open(YT_REFINED, encoding="utf-8")):
            if r.get("video_id") and r.get("spot_name"):
                v2s[r["video_id"]].add(r["spot_name"])
    except Exception as e:
        print(f"⚠ 유튜브 정제 로드 실패: {e}", flush=True)
    return v2s


def run_comments(pilot_n, done, fout, stats, seed):
    v2s = build_video_spots()
    videos = [r for r in load_jsonl(RAW_COMMENTS)
              if r.get("n_comments", 0) > 0 and r["video_id"] in v2s]
    if pilot_n:
        random.seed(seed)
        videos = random.sample(videos, min(pilot_n, len(videos)))
    print(f"[comments] 대상 영상 {len(videos)}개 (귀속 가능 영상 기준)", flush=True)
    yield_stat = {"videos": 0, "attributed": 0, "dropped_ambiguous": 0}
    for idx, v in enumerate(videos, 1):
        vid = v["video_id"]
        spots = v2s[vid]
        single = list(spots)[0] if len(spots) == 1 else None
        # 보수 귀속: 단일 카페 영상 → 그 카페 / 복수 영상 → 본문에 카페명 명시된 댓글만
        buckets = defaultdict(list)   # spot_name → [comment]
        for c in v.get("comments", []):
            text = (c.get("text") or "").strip()
            if not (5 <= len(text) <= 400):
                continue
            if single:
                buckets[single].append(c)
            else:
                hit = [s for s in spots if norm(s) and norm(s) in norm(text)]
                if len(hit) == 1:
                    buckets[hit[0]].append(c)
                else:
                    yield_stat["dropped_ambiguous"] += 1
        yield_stat["videos"] += 1
        for name, comments in buckets.items():
            if (name, f"comment:{vid}") in done:
                continue
            comments.sort(key=lambda c: c.get("like_count", 0), reverse=True)
            pieces, likes = {}, {}
            lines = []
            for j, c in enumerate(comments[:8], 1):
                pieces[j] = c["text"].strip()
                likes[j] = c.get("like_count", 0)
                lines.append(f"[{j}] (공감 {likes[j]}) {pieces[j]}")
            msg = (f"카페명: {name}\n출처: 유튜브 쇼츠 댓글 (시청자 반응 — 실방문 경험·정보만 채택, "
                   f"영상 감상은 제외)\n\n" + "\n".join(lines))
            result = call_llm(msg, f"comment:{name}@{vid}")
            if result is None:
                stats["failed"] += 1
            emit_closure(name, "comment", {j: vid for j in pieces}, pieces, result, stats)
            recs = verify_and_emit(name, "comment", {j: vid for j in pieces}, pieces,
                                   result, likes_map=likes, stats=stats)
            yield_stat["attributed"] += len(recs)
            for rec in recs:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
        if idx % 10 == 0:
            print(f"  … {idx}/{len(videos)}", flush=True)
    print(f"[comments] 수율: 영상 {yield_stat['videos']}개 → 채택 인용 {yield_stat['attributed']}건 "
          f"(영상당 {yield_stat['attributed'] / max(1, yield_stat['videos']):.1f}건), "
          f"모호 귀속 폐기 {yield_stat['dropped_ambiguous']}건", flush=True)


# ---------- 집계 + 태그 감사 ----------

def aggregate():
    per = defaultdict(lambda: {"pro": 0, "con": 0, "info": 0, "by_aspect": defaultdict(lambda: {"pro": 0, "con": 0})})
    for r in load_jsonl(OUT):
        s = per[r["spot_name"]]
        s[r["polarity"]] += 1
        if r["polarity"] in ("pro", "con"):
            s["by_aspect"][r["aspect"]][r["polarity"]] += 1
    tags = {r["spot_name"]: set(r.get("tags_blog") or []) for r in load_jsonl(NAVER_REFINED)}
    audit = []  # 태그 감사: 태그는 달려 있는데 같은 aspect의 con 증거가 있는 카페
    for name, s in per.items():
        for aspect, cnt in s["by_aspect"].items():
            if cnt["con"] >= 1 and aspect in tags.get(name, set()):
                audit.append({"spot_name": name, "tag": aspect,
                              "con_count": cnt["con"], "pro_count": cnt["pro"]})
    out = {"spots": {k: {"pro": v["pro"], "con": v["con"], "info": v["info"],
                         "by_aspect": {a: dict(c) for a, c in v["by_aspect"].items()}}
                     for k, v in per.items()},
           "tag_audit": sorted(audit, key=lambda x: -x["con_count"])}
    json.dump(out, open(AGG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"집계 완료 → {AGG}")
    print(f"  카페 {len(per)}곳 / 태그 감사 대상 {len(audit)}건 (태그 있는데 con 증거 존재)")
    top = out["tag_audit"][:10]
    for a in top:
        print(f"  ⚠ {a['spot_name']}: '{a['tag']}' 태그 vs con {a['con_count']}건 (pro {a['pro_count']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["blog", "kakao", "comments", "all", "aggregate"])
    ap.add_argument("--pilot", type=int, default=0, help="파일럿 표본 수 (blog/kakao=카페, comments=영상)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.source == "aggregate":
        aggregate()
        return

    done = {(r["spot_name"], r["source"] if r["source"] != "comment" else f"comment:{r['ref']}")
            for r in load_jsonl(OUT)}   # 이어달리기
    stats = defaultdict(int)

    pilot_names = None
    if args.pilot and args.source in ("blog", "kakao", "all"):
        random.seed(args.seed)
        pool = sorted(build_blog_cafes().keys())
        pilot_names = set(random.sample(pool, min(args.pilot, len(pool))))

    with open(OUT, "a", encoding="utf-8") as fout:
        if args.source in ("blog", "all"):
            run_blog(pilot_names, done, fout, stats)
        if args.source in ("kakao", "all"):
            run_kakao(pilot_names, done, fout, stats)
        if args.source in ("comments", "all"):
            run_comments(args.pilot or 30, done, fout, stats, args.seed)

    print("\n== 통계 ==")
    for k in ("emitted", "unverified", "malformed", "aspect_etc", "preset_override", "failed", "closure_hints"):
        print(f"  {k}: {stats[k]}")
    if stats["closure_hints"]:
        print(f"  (closure_hints → {CLOSURE_Q} 에 적재 — 검수 후 폐업제보 하드코딩 재료)")
    if stats["unverified"]:
        print("  (unverified = 원문 부분문자열 검증 실패로 폐기 — LLM이 의역한 스팬)")
    aggregate()


if __name__ == "__main__":
    main()
