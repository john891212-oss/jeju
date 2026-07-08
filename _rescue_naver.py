# -*- coding: utf-8 -*-
"""
실존의심/제외후보 재검색 — 이름 정리 후 네이버 재크롤.
대상: review_master.csv에서 판정=제외후보 OR 플래그에 실존의심 포함.
정리 규칙: 괄호부 제거, " in XX" 제거, 공백 정돈.
결과: data/raw/네이버 재검색.jsonl (원본과 분리, append)
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "data", "processed", "review_master.csv")
OUT = os.path.join(ROOT, "data", "raw", "네이버 재검색 크롤링.jsonl")
SLEEP = 0.15

env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
CID, CSECRET = env.get("NAVER_CLIENT_ID", ""), env.get("NAVER_CLIENT_SECRET", "")
if not CID or not CSECRET:
    sys.exit("[중단] 네이버 키 없음")

def naver_get(endpoint, query, display, sort=None, retries=3):
    params = {"query": query, "display": display}
    if sort:
        params["sort"] = sort
    url = f"https://openapi.naver.com/v1/search/{endpoint}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": CID, "X-Naver-Client-Secret": CSECRET})
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

TAG = re.compile(r"<[^>]+>")
def norm(s):
    s = TAG.sub("", s or "").split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def clean_name(n):
    n = re.sub(r"[\(\[（].*?[\)\]）]", " ", n)   # 괄호부 제거
    n = re.sub(r"\s+in\s+\S+", " ", n)          # " in 성산" 제거
    n = re.sub(r"\s+", " ", n).strip()
    return n

# ---- 대상 추출 ----
targets = []
for r in csv.DictReader(open(SRC, encoding="utf-8-sig")):
    if r["판정"] == "제외후보" or "실존의심" in r.get("플래그", ""):
        targets.append(r["카페명"])
targets = list(dict.fromkeys(targets))
print(f"재검색 대상: {len(targets)}곳")

done = set()
if os.path.exists(OUT):
    for line in open(OUT, encoding="utf-8", errors="replace"):
        try:
            done.add(json.loads(line)["spot_name"])
        except Exception:
            pass

fout = open(OUT, "a", encoding="utf-8")
rescued = 0
for i, name in enumerate(targets):
    if name in done:
        continue
    cleaned = clean_name(name)
    rec = {"spot_name": name, "cleaned_name": cleaned}
    try:
        q = f"제주 {cleaned}"
        blog = naver_get("blog", q, display=100, sort="sim")
        time.sleep(SLEEP)
        # 유효 스니펫 0이면 '제주' 빼고 한 번 더
        key = norm(cleaned)
        def n_valid(b):
            return sum(1 for it in b.get("items", [])
                       if key and key in norm(it.get("title", "") + it.get("description", "")))
        if n_valid(blog) == 0:
            q = cleaned
            blog = naver_get("blog", q, display=100, sort="sim")
            time.sleep(SLEEP)
        rec["query"] = q
        rec["blog"] = blog
        rec["local"] = naver_get("local", f"제주 {cleaned}", display=5)
        time.sleep(SLEEP)
        if n_valid(blog) > 0 or rec["local"].get("items"):
            rescued += 1
    except Exception as e:
        rec["error"] = repr(e)
        print(f"  [실패] {name}: {e!r}", flush=True)
    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fout.flush()
    if (i + 1) % 20 == 0:
        print(f"  {i+1}/{len(targets)} | 신호 살아남 {rescued}", flush=True)
fout.close()
print(f"[완료] {len(targets)}곳 재검색, 신호 확인 {rescued}곳")
print(f"저장: {OUT}")
