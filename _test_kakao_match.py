# -*- coding: utf-8 -*-
"""트랙 2 사전 실증: 카카오 keyword 조회 매칭율 (표본 30, random.sample 관례).
매칭 기준 = 이름 유사 + 좌표 근접(네이버 지역검색 좌표 기준 500m).
판정: MATCH(둘 다) / NEAR(좌표만) / NAME(이름만) / MISS(후보 없음·둘 다 불일치)"""
import json, os, re, random, math, csv, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8-sig"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def norm(s):
    s = (s or "").split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def dist_m(a, b, c, d):
    R = 6371000
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

# 표본: 판정 유지 + 네이버 좌표 보유
keep = set()
for r in csv.DictReader(open(os.path.join(ROOT, "data/processed/review_master.csv"), encoding="utf-8-sig")):
    if r.get("판정") == "유지":
        keep.add(r["카페명"])
extra = json.load(open(os.path.join(ROOT, "data/processed/카페부가v2.json"), encoding="utf-8"))
cand = [(n, e["lat"], e["lng"]) for n, e in extra.items() if n in keep and e.get("lat")]
random.seed(42)
sample = random.sample(cand, 30)

def kakao(q):
    url = ("https://dapi.kakao.com/v2/local/search/keyword.json?size=5&query="
           + urllib.parse.quote(q))
    req = urllib.request.Request(url, headers={"Authorization": "KakaoAK " + env["KAKAO_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=10)).get("documents", [])

res = {"MATCH": 0, "NEAR": 0, "NAME": 0, "MISS": 0}
for name, lat, lng in sample:
    docs = kakao(name + " 제주")
    if not docs:
        docs = kakao(name.split("(")[0].strip() + " 제주 카페")
    best, tag = None, "MISS"
    for d in docs:
        nm = norm(d["place_name"]) ; qn = norm(name)
        name_ok = qn and nm and (qn in nm or nm in qn)
        try:
            dd = dist_m(lat, lng, float(d["y"]), float(d["x"]))
        except ValueError:
            dd = 9e9
        coord_ok = dd < 500
        t = "MATCH" if (name_ok and coord_ok) else ("NEAR" if coord_ok else ("NAME" if name_ok else None))
        rank = {"MATCH": 3, "NEAR": 2, "NAME": 1}.get(t, 0)
        if rank > {"MATCH": 3, "NEAR": 2, "NAME": 1}.get(tag, 0):
            tag, best = t, d
    res[tag] += 1
    b = f"{best['place_name']} ({best['id']})" if best else "-"
    print(f"  [{tag:5}] {name[:20]:20} -> {b}")

n = len(sample)
print(f"\n표본 {n}: MATCH {res['MATCH']} ({res['MATCH']/n*100:.0f}%) / NEAR {res['NEAR']} / NAME {res['NAME']} / MISS {res['MISS']}")
print("MATCH+NAME(=place_id 확보 가능) 합계:", res["MATCH"] + res["NAME"])
