# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 발굴 신규 카페 편입 — 발굴검증.json 편입후보 → 크롤 → 정제 → 임베딩 (소량 원샷 파이프라인).

입력:  data/processed/발굴검증.json     편입후보 (place_id·카카오 정식명·주소 보유, 카카오명 중복은 1회만 편입)
출력:  data/raw/발굴 크롤링.jsonl         (crawl — naver_crawl.py 방식 그대로: 블로그 100 + 지역검색 5, sleep 0.15, append/이어달리기)
       data/processed/발굴 정제.jsonl     (refine — naver_refine.py SYSTEM/호출 파라미터 재사용, 스키마 동일)
       chroma_smoke/ 컬렉션 "smoke"에 add (embed — embed.py 방식 재사용, id="blog::{spot_name}")
키:    .env NAVER_CLIENT_ID / NAVER_CLIENT_SECRET / OPENAI_KEY
소비자: app/server.py (/search) — chroma_smoke "smoke" 컬렉션 공유 (기존 네이버/유튜브 문서와 동일 컬렉션)

원칙: spot_name은 카카오 정식명(place_name) 기준으로 통일 — 이름 조인 흔들림 방지.
      region은 카카오 주소의 읍/면 단위로 유도(그 외 제주시→제주시내, 서귀포시→서귀포시내).
      reasoning_effort=minimal + max_completion_tokens=4000 (빈 응답 방지, naver_refine.py 계승).

사용:
  python pipeline/ingest_discovered.py           # crawl → refine → embed 전체
  python pipeline/ingest_discovered.py crawl
  python pipeline/ingest_discovered.py refine
  python pipeline/ingest_discovered.py embed
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "발굴검증.json")
OUT_CRAWL = os.path.join(ROOT, "data", "raw", "발굴 크롤링.jsonl")
OUT_REFINE = os.path.join(ROOT, "data", "processed", "발굴 정제.jsonl")
CHROMA_DIR = os.path.join(ROOT, "chroma_smoke")
SLEEP = 0.15


def load_env():
    env = {}
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


ENV = load_env()

# ---------- 지역 유도 (카카오 주소 → region) ----------
_EUP_MYEON = [
    ("애월읍", "애월"), ("한림읍", "한림"), ("한경면", "한경"), ("구좌읍", "구좌"),
    ("조천읍", "조천"), ("성산읍", "성산"), ("표선면", "표선"), ("남원읍", "남원"),
    ("안덕면", "안덕"), ("대정읍", "대정"), ("우도면", "우도"),
]


def region_from_address(addr):
    addr = addr or ""
    for key, region in _EUP_MYEON:
        if key in addr:
            return region
    if "제주시" in addr:
        return "제주시내"
    if "서귀포시" in addr:
        return "서귀포시내"
    return "기타"


# ---------- 편입후보 로드 (중복 카카오명은 1회만) ----------
def load_candidates():
    data = json.load(open(SRC, encoding="utf-8"))
    cands = data.get("편입후보", [])
    uniq = {}
    for c in cands:
        uniq.setdefault(c["kakao_name"], c)  # 먼저 나온 항목 기준 1회만
    return list(uniq.values())


# ================= 단계 A: 크롤 (naver_crawl.py 방식) =================
TAG = re.compile(r"<[^>]+>")


def clean(s):
    return TAG.sub("", s or "").strip()


def norm(s):
    s = clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())


def naver_get(endpoint, query, display, sort=None, retries=3):
    cid, csecret = ENV.get("NAVER_CLIENT_ID", ""), ENV.get("NAVER_CLIENT_SECRET", "")
    if not cid or not csecret:
        sys.exit("[중단] .env에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 없음")
    params = {"query": query, "display": display}
    if sort:
        params["sort"] = sort
    url = f"https://openapi.naver.com/v1/search/{endpoint}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csecret})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    raise RuntimeError("retries exhausted")


def load_done_crawl():
    done = set()
    if os.path.exists(OUT_CRAWL):
        for line in open(OUT_CRAWL, encoding="utf-8", errors="replace"):
            try:
                done.add(json.loads(line)["spot_name"])
            except Exception:
                pass
    return done


def cmd_crawl():
    cands = load_candidates()
    print(f"편입후보(dedup): {len(cands)}곳")
    done = load_done_crawl()
    if done:
        print(f"[재개] 기존 {len(done)}건 스킵")
    fout = open(OUT_CRAWL, "a", encoding="utf-8")
    new, fails = 0, 0
    for c in cands:
        name = c["kakao_name"]
        if name in done:
            continue
        q = f"제주 {name}"
        rec = {"spot_name": name, "place_id": c.get("place_id"),
               "address": c.get("address"), "region": region_from_address(c.get("address")),
               "query": q}
        try:
            rec["blog"] = naver_get("blog", q, display=100, sort="sim")
            time.sleep(SLEEP)
            rec["local"] = naver_get("local", q, display=5)
            time.sleep(SLEEP)
        except Exception as e:
            rec["error"] = repr(e)
            fails += 1
            print(f"  [실패] {name}: {e!r}", flush=True)
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        new += 1
    fout.close()
    print(f"[완료-크롤] 신규 {new} / 실패 {fails} → {OUT_CRAWL}")
    return new


# ================= 단계 B: 정제 (naver_refine.py SYSTEM/파라미터 재사용) =================
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

ALLOWED = {t.strip() for t in CAFE_TAGS.split(",")}


def load_jsonl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def build_cafes():
    cafes = {}
    for r in load_jsonl(OUT_CRAWL):
        name = r["spot_name"]
        key = norm(name)
        items = r.get("blog", {}).get("items", [])
        valid = [it for it in items
                 if key and key in norm(it.get("title", "") + it.get("description", ""))
                 and it.get("postdate", "") >= "20240101"]
        if not valid:
            continue
        valid.sort(key=lambda it: it.get("postdate", ""), reverse=True)
        cafes[name] = {"valid": valid[:30],
                       "bloggers": len({it.get("bloggername") for it in valid}),
                       "local": (r.get("local", {}).get("items") or [{}])[0]}
    return cafes


def build_input(name, c):
    loc = c["local"]
    head = f"카페명: {name}"
    if loc.get("title"):
        head += f"\n네이버 등록 상호: {clean(loc['title'])} | 업종: {loc.get('category', '')}"
    lines = [f"- [{it.get('postdate', '')}] {clean(it.get('title', ''))} — {clean(it.get('description', ''))}"
             for it in c["valid"]]
    return head + "\n\n## 블로그 후기 스니펫\n" + "\n".join(lines)


def get_openai_client():
    from openai import OpenAI
    return OpenAI(api_key=ENV["OPENAI_KEY"])


def refine(client, name, c, max_retry=5):
    from openai import RateLimitError
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
            raw_tags = d.get("tags_blog", []) or []
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


def cmd_refine():
    cafes = build_cafes()
    print(f"정제 대상: {len(cafes)}곳 (유효스니펫 1개 이상)")
    done = {r["spot_name"] for r in load_jsonl(OUT_REFINE)}
    if done:
        print(f"[재개] 기존 {len(done)}건 스킵")
    client = get_openai_client()
    fout = open(OUT_REFINE, "a", encoding="utf-8")
    n_new, n_fail = 0, 0
    for name, c in cafes.items():
        if name in done:
            continue
        rec = refine(client, name, c)
        if rec is None:
            n_fail += 1
            continue
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        n_new += 1
    fout.close()
    print(f"[완료-정제] 신규 {n_new} / 실패 {n_fail} → {OUT_REFINE}")
    return n_new


# ================= 단계 C: 임베딩 (embed.py 방식) =================
def cmd_embed():
    import chromadb
    cands = load_candidates()
    addr_map = {c["kakao_name"]: c.get("address") for c in cands}
    refined = load_jsonl(OUT_REFINE)
    docs = []
    n_skip = 0
    for r in refined:
        if not (r.get("summary_blog") or "").strip() or r.get("closed_hint"):
            n_skip += 1
            continue
        name = r["spot_name"]
        docs.append((f"blog::{name}", r["summary_blog"],
                     {"spot_name": name, "source": "blog",
                      "richness": r.get("info_richness_blog") or "",
                      "region": region_from_address(addr_map.get(name))}))
    print(f"임베딩 후보: {len(docs)}건 (스킵 {n_skip}건 — 빈 요약/폐업)")

    cdb = chromadb.PersistentClient(path=CHROMA_DIR)
    col = cdb.get_or_create_collection("smoke", metadata={"hnsw:space": "cosine"})
    before = col.count()

    existing = set()
    if docs:
        got = col.get(ids=[d[0] for d in docs])
        existing = set(got.get("ids") or [])
    new_docs = [d for d in docs if d[0] not in existing]
    if existing:
        print(f"[스킵] 이미 적재된 {len(existing)}건")

    n_added = 0
    if new_docs:
        client = get_openai_client()
        texts = [d[1] for d in new_docs]
        resp = client.embeddings.create(model="text-embedding-3-large", input=texts)
        embs = [item.embedding for item in resp.data]
        col.add(ids=[d[0] for d in new_docs],
                documents=[d[1] for d in new_docs],
                metadatas=[d[2] for d in new_docs],
                embeddings=embs)
        n_added = len(new_docs)
    after = col.count()
    print(f"[완료-임베딩] 신규 {n_added}건 → smoke 총 {after}건 (이전 {before}건)")
    return n_added, after


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "crawl":
        cmd_crawl()
    elif mode == "refine":
        cmd_refine()
    elif mode == "embed":
        cmd_embed()
    elif mode == "all":
        n_crawl = cmd_crawl()
        n_refine = cmd_refine()
        n_added, total = cmd_embed()
        print(f"\n=== 요약 ===\n크롤 신규: {n_crawl}\n정제 신규: {n_refine}\n임베딩 신규: {n_added} (smoke 총 {total}건)")
    else:
        sys.exit("사용법: ingest_discovered.py [all | crawl | refine | embed]")
