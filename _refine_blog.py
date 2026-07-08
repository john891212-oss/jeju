# -*- coding: utf-8 -*-
"""
블로그 정제 패스 — 네이버 스니펫 → 카페별 구조화 정제 (Pass 1 관례 계승).

입력: 카페당 유효스니펫(카페명 포함 + 2024-01 이후) 상위 30개, postdate 최신순
출력: data/processed/카페-블로그정제.jsonl (JSONL append — 중단 안전)
  { spot_name, summary_blog, tags_blog, category_hint, closed_hint,
    info_richness_blog, n_snippets_used, bloggers_used }  ← 숫자는 코드가 운반

사용:
  python _refine_blog.py 20   # 관통 (random 20)
  python _refine_blog.py      # 전량
"""
import json
import os
import random
import re
import sys
import time

from openai import OpenAI, RateLimitError

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw", "네이버 크롤링.jsonl")
RESCUE = os.path.join(ROOT, "data", "raw", "네이버 재검색 크롤링.jsonl")
OUT = os.path.join(ROOT, "data", "processed", "네이버 정제.jsonl")

# ---- .env ----
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])

CAFE_TAGS = "오션뷰, 산방산뷰, 숲뷰, 노을, 감성, 조용함, 대형, 베이커리, 브런치, 디저트, 애견동반, 노키즈존, 키즈친화, 통창, 야외석, 루프탑, 주차편함, 웨이팅, 신상, 로컬"
CATEGORIES = "카페, 베이커리, 디저트, 브런치, 소품샵겸업, 펍겸업, 음식점겸업, 기타"

SYSTEM = f"""제주 카페에 대한 블로그 후기 스니펫 묶음을 읽고 json으로만 답해.

## 규칙
- summary_blog: 검색될 자연어 1~2문장. 다음 슬롯 중 스니펫에 실제로 있는 것만:
  뷰, 분위기, 시그니처 메뉴와 가격대, 웨이팅/혼잡도, 주차, 좌석 특성, 영업 특이사항.
  지어내지 마. 스니펫이 광고 문구뿐이거나 정보가 없으면 빈 문자열 ""
- tags_blog: 다음 사전에서만 선택, 스니펫에 근거 있는 것만 0~5개: {CAFE_TAGS}
- category_hint: 다음 중 하나 — {CATEGORIES}
  (밤에 클럽이 되거나 식당 겸업이면 겸업으로. 확실치 않으면 "카페")
- closed_hint: 폐업·영업종료·철거·이전 언급이 명시적으로 있으면 true, 아니면 false
- info_richness_blog: 슬롯 2개 이상="high", 1개="mid", 이름뿐/광고뿐="low"
- 스니펫은 검색 결과 요약이라 문장이 잘려 있음 — 잘린 문장에서 추측하지 마

## 출력 (json만)
{{"summary_blog": "", "tags_blog": [], "category_hint": "",
"closed_hint": false, "info_richness_blog": ""}}"""

TAG = re.compile(r"<[^>]+>")
def clean(s):
    return TAG.sub("", s or "").replace("&quot;", '"').replace("&amp;", "&").strip()

def norm(s):
    s = clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

# ---- 카페별 유효스니펫 구성 (재검색 결과가 있으면 교체) ----
def load_jsonl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

rescue = {r["spot_name"]: r for r in load_jsonl(RESCUE) if "blog" in r}
cafes = {}
for r in load_jsonl(RAW):
    name = r["spot_name"]
    key = norm(name)
    if name in rescue:
        rr = rescue[name]
        r = {**r, "blog": rr["blog"], "local": rr.get("local", {})}
        key = norm(rr.get("cleaned_name") or name)
    items = r.get("blog", {}).get("items", [])
    valid = [it for it in items
             if key and key in norm(it.get("title", "") + it.get("description", ""))
             and it.get("postdate", "") >= "20240101"]
    if not valid:
        continue
    valid.sort(key=lambda it: it.get("postdate", ""), reverse=True)  # 최신 우선
    cafes[name] = {"valid": valid[:30],
                   "bloggers": len({it.get("bloggername") for it in valid}),
                   "local": (r.get("local", {}).get("items") or [{}])[0]}

print(f"정제 대상: {len(cafes)}곳 (유효스니펫 1개 이상)")

def build_input(name, c):
    loc = c["local"]
    head = f"카페명: {name}"
    if loc.get("title"):
        head += f"\n네이버 등록 상호: {clean(loc['title'])} | 업종: {loc.get('category','')}"
    lines = [f"- [{it.get('postdate','')}] {clean(it.get('title',''))} — {clean(it.get('description',''))}"
             for it in c["valid"]]
    return head + "\n\n## 블로그 후기 스니펫\n" + "\n".join(lines)

def refine(name, c, max_retry=5):
    for i in range(max_retry):
        try:
            resp = client.chat.completions.create(
                model="gpt-5-mini",
                response_format={"type": "json_object"},
                max_completion_tokens=4000,
                reasoning_effort="minimal",
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": build_input(name, c)}],
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                print(f"  ⚠ 빈 응답 [{name}] finish={resp.choices[0].finish_reason}", flush=True)
                return None
            d = json.loads(content)
            # 태그 사전 강제는 코드가 (프롬프트는 못 믿음) — 이탈분은 tags_extra로 보존
            ALLOWED = {t.strip() for t in CAFE_TAGS.split(",")}
            raw_tags = d.get("tags_blog", []) or []
            # 식별자·숫자는 코드가 운반
            return {"spot_name": name,
                    "summary_blog": d.get("summary_blog", ""),
                    "tags_blog": [t for t in raw_tags if t in ALLOWED],
                    "tags_extra": [t for t in raw_tags if t not in ALLOWED],
                    "category_hint": d.get("category_hint", ""),
                    "closed_hint": bool(d.get("closed_hint")),
                    "info_richness_blog": d.get("info_richness_blog", ""),
                    "n_snippets_used": len(c["valid"]),
                    "bloggers_used": c["bloggers"]}
        except RateLimitError:
            time.sleep(2 ** i)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠ 파싱 실패 [{name}]: {e}", flush=True)
            return None
    print(f"  ⚠ 재시도 초과 [{name}]", flush=True)
    return None

# ---- 실행 ----
targets = list(cafes.items())
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if limit:
    random.seed(42)
    targets = random.sample(targets, min(limit, len(targets)))
    print(f"[관통 모드] random {len(targets)}건")

done = {r["spot_name"] for r in load_jsonl(OUT)}
if done:
    print(f"[재개] 기존 {len(done)}건 스킵")

fout = open(OUT, "a", encoding="utf-8")
t0 = time.time()
n_new = n_fail = 0
for i, (name, c) in enumerate(targets):
    if name in done:
        continue
    rec = refine(name, c)
    if rec is None:
        n_fail += 1
        continue
    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fout.flush()
    n_new += 1
    if n_new % 10 == 0:
        el = time.time() - t0
        eta = el / n_new * (len(targets) - len(done) - n_new)
        print(f"  {n_new}건 | {el:.0f}s | 실패 {n_fail} | 남은 예상 {eta/60:.0f}분", flush=True)
fout.close()
print(f"[완료] 신규 {n_new} / 실패 {n_fail} / {time.time()-t0:.0f}s → {OUT}")
