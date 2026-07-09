# -*- coding: utf-8 -*-
"""
중복 카페 검수 시트 생성기 (검수 1단계 — 시트만 펼친다. 실제 병합은 merge.py 몫)

같은 카페가 spot_name(긁힌 원본 이름)으로 여러 조각으로 쪼개진 것을
사람이 눈으로 검수·병합할 수 있게 CSV 2개로 펼친다.

핵심 원칙 (하이브리드):
  - place_id 있는 중복 그룹 = 카카오가 좌표까지 검증한 번호 -> 자동 병합(확인만).
  - place_id 없는 이름변형 = 사람 검수. 이름 유사도 자동 병합 금지(가짜 병합 위험).
    후보만 제시하고 O/X 판단은 사람이.

출력:
  data/processed/중복검수_place_id.csv    (place_id 자동 그룹, 확인만)
  data/processed/중복검수_이름변형.csv     (place_id 없는 이름변형 후보, 손검수 필수)
"""
import os
import re
import csv
import json
from collections import defaultdict

# 경로 고정: 이 파일은 <ROOT>/pipeline/ 에 있다. dirname 2회로 ROOT.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")

KAKAO_PLACE = os.path.join(PROC, "카카오플레이스.jsonl")
NAVER_REFINE = os.path.join(PROC, "네이버 정제.jsonl")

OUT_PID = os.path.join(PROC, "중복검수_place_id.csv")
OUT_ALIAS = os.path.join(PROC, "중복검수_이름변형.csv")

# 이름 알맹이에서 걷어낼 일반어 (지시서 진단 로직과 동일)
GENERIC = ['카페', '커피', '베이커리', '디저트', '브런치', '제주', '제주점', '본점', '점']


def load_jsonl(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def core(s):
    """이름 알맹이: 괄호 뒤 버리고, 특수문자·공백 제거, 일반어 제거."""
    n = re.sub(r'[^\w가-힣]', '', (s or '').split('(')[0].lower())
    for g in GENERIC:
        n = n.replace(g, '')
    return n


def write_csv(path, header, rows):
    # utf-8-sig: 엑셀에서 한글 안 깨지게
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    kp = load_jsonl(KAKAO_PLACE)
    nv = load_jsonl(NAVER_REFINE)

    # spot_name -> bloggers_used (블로거 분산 표시용). 없으면 빈칸.
    name2blog = {}
    for r in nv:
        sn = r.get('spot_name')
        if sn is not None:
            name2blog[sn] = r.get('bloggers_used')

    def blog_of(sn):
        v = name2blog.get(sn)
        return v if v is not None else ''

    def blog_num(sn):
        # 정렬용: 없으면 -1 (맨 아래로)
        v = name2blog.get(sn)
        return v if isinstance(v, (int, float)) else -1

    # ---------------------------------------------------------------
    # 1) place_id 중복 그룹 (자동 병합, 확인만)
    # ---------------------------------------------------------------
    # place_id -> 그 place_id를 가진 레코드들
    pid_records = defaultdict(list)
    for r in kp:
        pid = r.get('place_id')
        sn = r.get('spot_name')
        if pid and sn:
            pid_records[pid].append(r)

    # 조각 2개 이상 = 중복 그룹
    groups = [(pid, recs) for pid, recs in pid_records.items() if len(recs) > 1]

    # 그룹 정렬: 조각수 많은 순, 동률이면 place_id
    groups.sort(key=lambda x: (-len(x[1]), x[0]))

    pid_rows = []
    surplus = 0
    group_total_blog = []  # (pid, 이름들, 블로거리스트) 블로거 분산 요약용
    for pid, recs in groups:
        surplus += len(recs) - 1
        # 그룹 내 조각 정렬: 블로거 많은 순(정본 후보가 위로), 동률이면 이름
        recs_sorted = sorted(
            recs, key=lambda r: (-blog_num(r['spot_name']), r['spot_name'])
        )
        kakao_name = recs_sorted[0].get('kakao_name') or ''
        n_frag = len(recs_sorted)
        for r in recs_sorted:
            sn = r['spot_name']
            pid_rows.append([
                pid,
                kakao_name,
                n_frag,
                sn,
                blog_of(sn),
                r.get('road_address') or '',
                '',  # 검수(비움) — 잘못 묶였으면 사람이 X
            ])
        blist = sorted(
            [blog_num(r['spot_name']) if blog_num(r['spot_name']) >= 0 else 0
             for r in recs_sorted],
            reverse=True,
        )
        group_total_blog.append((pid, [r['spot_name'] for r in recs_sorted], blist))

    write_csv(
        OUT_PID,
        ['place_id', '정본후보(kakao_name)', '조각수', 'spot_name',
         'bloggers', 'road_address', '검수(비움: 잘못묶임=X)'],
        pid_rows,
    )

    # ---------------------------------------------------------------
    # 2) place_id 없는 이름변형 후보 (손검수 필수)
    #    유형A: place_id 없음 <-> place_id 있는 카페의 core와 겹침 (지시서 원 로직)
    #    유형B: place_id 없음 <-> place_id 없음 (자기들끼리 core 겹침)
    #    ⚠ 자동 병합 금지. 후보만 제시, O/X는 사람이.
    # ---------------------------------------------------------------
    # place_id 있는 카페의 core -> 대표(place_id, kakao_name)
    pid_core_rep = {}
    for pid, recs in pid_records.items():
        # 대표 이름: 블로거 가장 많은 조각의 spot_name 기준 core
        rep = sorted(recs, key=lambda r: (-blog_num(r['spot_name']), r['spot_name']))[0]
        kakao_name = rep.get('kakao_name') or rep['spot_name']
        for r in recs:
            c = core(r['spot_name'])
            if len(c) >= 3 and c not in pid_core_rep:
                pid_core_rep[c] = (pid, kakao_name)

    no_pid = [r for r in kp if not r.get('place_id') and r.get('spot_name')]

    alias_rows = []

    # 유형A
    typeA = 0
    for r in no_pid:
        sn = r['spot_name']
        c = core(sn)
        if len(c) >= 3 and c in pid_core_rep:
            pid, kakao_name = pid_core_rep[c]
            alias_rows.append(['A(place_id매칭)', sn, pid, kakao_name, c, ''])
            typeA += 1

    # 유형B: place_id 없는 것끼리 core 그룹
    byc = defaultdict(list)
    for r in no_pid:
        sn = r['spot_name']
        c = core(sn)
        if len(c) >= 3 and c not in pid_core_rep:  # 유형A로 이미 잡힌 건 제외
            byc[c].append(sn)
    typeB_groups = {c: ns for c, ns in byc.items() if len(ns) > 1}
    # 그룹 크기 큰 순으로, 같은 core는 인접하게
    for c, ns in sorted(typeB_groups.items(), key=lambda x: (-len(x[1]), x[0])):
        # 그룹 대표 = 블로거 많은 이름
        rep_name = sorted(ns, key=lambda s: (-blog_num(s), s))[0]
        for sn in sorted(ns, key=lambda s: (-blog_num(s), s)):
            alias_rows.append(['B(place_id없음끼리)', sn, '', rep_name, c, ''])

    write_csv(
        OUT_ALIAS,
        ['유형', '누수_spot_name', '추정정본_place_id', '추정정본_이름',
         '이름알맹이', '병합여부(비움: O=합침 X=별개)'],
        alias_rows,
    )

    # ---------------------------------------------------------------
    # 요약
    # ---------------------------------------------------------------
    print("=== 중복 검수 시트 생성 완료 ===")
    print("place_id 중복 그룹:", len(groups))
    print("잉여 레코드(그룹내 조각-1 합):", surplus)
    print("이름변형 후보 - 유형A(place_id매칭):", typeA)
    print("이름변형 후보 - 유형B(place_id없음끼리) 그룹:", len(typeB_groups),
          "/ 레코드:", sum(len(ns) for ns in typeB_groups.values()))
    print()
    print("블로거 분산 상위 5 (같은 카페인데 조각마다 블로거가 갈라진 정도):")
    top = sorted(group_total_blog, key=lambda x: -sum(x[2]))[:5]
    for pid, names, blist in top:
        loss = sum(blist) - max(blist) if blist else 0
        print("  pid", pid, "합", sum(blist), "max", max(blist) if blist else 0,
              "분산", blist, "| max만쓰면 손실", loss)
        print("     이름:", names[:3], "..." if len(names) > 3 else "")
    print()
    print("출력:")
    print("  ", OUT_PID)
    print("  ", OUT_ALIAS)


if __name__ == '__main__':
    main()
