#!/usr/bin/env python3
"""개념 지도 빌더 — `mastery.md` + `daily/**` → `concepts.json`.

왜 이 파일이 필요한가:
학습 기록(mastery.md)에는 **개념과 이해 상태**가 있지만 **선수관계(edges)** 가 없다.
그래서 "아직 설명 못 하는데 다른 개념을 막고 있는" **막힌 길목**을 계산할 수 없다.
러너가 세션마다 `## 개념 지도` 절에 `A ← B, C`(A의 선수 개념은 B와 C) 한두 줄만 남기면,
이 스크립트가 그것을 모아 그래프로 만든다.

이 빌더가 daily 노트에서 뽑는 것은 셋이다:

1. **선수관계** — `## 개념 지도`의 `A ← B, C`
2. **분야(domain)** — `## 개념 지도` 안의 `### 소제목`. 그 아래 줄들의 개념이 그 분야다.
   분야는 세션의 속성이 아니라 **개념의 속성**이라서 여기서 정한다.
3. **원문 근거** — 노트 본문의 불릿 한 줄을 **그대로**. 요약하지 않는다.
   "이 개념을 안다고 말할 근거가 내 노트 어느 줄에 있는가"가 이해도의 증거다.

산출물 `concepts.json`은 Topdown 앱이 그대로 읽는 형식이다(개념 그래프·막힌 길목·원문 인용).
**생성물이다** — 고칠 곳은 `mastery.md`와 daily 노트다.

실행: python3 scripts/build_concepts.py   (CI가 세션마다 부른다)
"""
import glob
import json
import os
import re
import sys

MASTERY = "mastery.md"
DAILY_DIR = "daily"
OUT = "concepts.json"
SECTION = "개념 지도"

# mastery.md 상태 → Topdown이 아는 어휘. `설명가능`만 '통과'로 친다.
MASTERED = {"설명가능"}

UNCLASSIFIED = "미분류"

# 노트 섹션 제목(부분 일치) → 근거의 종류. Topdown의 `RawSource.kind`가 이 어휘를 쓴다.
# 앞에 오는 것이 먼저 매칭되므로 더 구체적인 제목을 위에 둔다.
SOURCE_SECTIONS = [
    ("오늘 직접 학습한 지식", "지식"),
    ("예측", "내말"),
    ("교정", "교정"),
    ("취약", "취약"),
    ("퀴즈", "퀴즈"),
    ("계산", "계산"),
    ("유도", "계산"),
]

# 한국어는 조사가 단어에 붙는다("신경망은", "역전파를"). 그래서 "뒤에 한글이 오면 다른
# 단어"라는 단순 규칙을 쓰면 정상 매칭까지 죽는다. 붙어도 같은 단어로 보는 꼬리 목록.
JOSA = (
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로", "으로",
    "에서", "에게", "부터", "까지", "보다", "처럼", "라고", "이라고", "이나", "나",
    "이란", "란", "이며", "며", "이고", "고", "인", "임", "이다", "다", "적", "적인",
)

_WORDCHAR = re.compile(r"[0-9A-Za-z가-힣]")


def is_placeholder(text):
    """`<개념>` 같은 템플릿 자리표시자인가 — 아직 안 채운 칸을 개념으로 세우면 안 된다.

    session-card 템플릿을 그대로 복사한 노트가 들어와도 유령 개념이 생기지 않게 한다.
    """
    s = (text or "").strip()
    return not s or ("<" in s and ">" in s)


def slugify(text):
    s = re.sub(r"[^\w가-힣\s-]", "", (text or "").strip()).strip()
    s = re.sub(r"\s+", "-", s)
    return s.lower()[:60] or "concept"


def parse_mastery(path=MASTERY):
    """mastery.md 표 → {label: {state, importance, verified, evidence}} (마지막 줄이 이김)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2:
                continue
            label = cells[0]
            if not label or label == "개념" or set(label) <= set("-: "):
                continue  # 헤더·구분선
            out[label] = {
                "state": cells[1] if len(cells) > 1 else "",
                "importance": cells[2] if len(cells) > 2 else "",
                "verified": cells[3] if len(cells) > 3 else "",
                "evidence": cells[4] if len(cells) > 4 else "",
            }
    return out


def _section_body(text, title):
    """`## <title>` 절의 본문. 제목은 부분 일치(이모지·괄호 주석이 붙어도 찾도록)."""
    m = re.search(r"^##\s*.*%s.*$" % re.escape(title), text, re.M)
    if not m:
        return ""
    body = text[m.end():]
    nxt = re.search(r"^##\s", body, re.M)
    return body[: nxt.start()] if nxt else body


def parse_concept_map(daily_dir=DAILY_DIR):
    """`## 개념 지도` → (선수관계 목록, {개념: 분야}).

    분야는 절 안의 `### 소제목`이 정한다:

        ## 개념 지도
        ### 선형대수
        - 선형변환 ← 행렬
        ### ML
        - 딥러닝 ← 선형대수, 미분

    화살표 없이 이름만 적은 줄은 **분류만** 한다(선수관계는 안 만든다):

        ### 선형대수
        - 선형대수
        - 선형변환 ← 선형대수

    이 길이 필요한 이유: 분야는 화살표 **왼쪽**에만 붙는다. 선수 개념에까지 물려주면
    `### ML` 아래의 `딥러닝 ← 선형대수`가 선형대수를 ML로 잘못 분류한다. 그래서 뿌리
    개념(남의 선수이기만 하고 자신은 타깃이 안 되는 개념)은 이름만 적어 분류한다.

    같은 개념이 나중 세션에서 다시 분류되면 **마지막 명시가 이긴다**(mastery.md 관례와 동일).
    소제목 없이 적힌 개념은 분야를 남기지 않는다 → 나중에 `미분류`가 된다.
    """
    edges = []
    domain_of = {}
    for path in sorted(glob.glob(os.path.join(daily_dir, "*.md"))):
        with open(path, encoding="utf-8") as f:
            body = _section_body(f.read(), SECTION)
        if not body:
            continue
        current_domain = ""
        for line in body.splitlines():
            head = re.match(r"^\s*#{3,}\s*(.+?)\s*$", line)
            if head:
                name = head.group(1).strip().strip("`")
                current_domain = "" if is_placeholder(name) else name
                continue
            line = line.strip().lstrip("-*").strip()
            if not line:
                continue
            # `A ← B, C`  (화살표는 ←, <-, <= 를 허용)
            parts = re.split(r"←|<-|<=", line, maxsplit=1)
            if len(parts) != 2:
                # 화살표 없는 줄 = 분류만. 소제목 아래일 때만 뜻이 있다.
                name = line.strip("`").strip()
                if current_domain and not is_placeholder(name):
                    domain_of[name] = current_domain
                continue
            target = parts[0].strip().strip("`")
            if is_placeholder(target):
                continue
            if current_domain:
                domain_of[target] = current_domain
            for prereq in parts[1].split(","):
                prereq = prereq.strip().strip("`")
                if not is_placeholder(prereq) and target != prereq:
                    edges.append((target, prereq))
    return edges, domain_of


def _bullets(body):
    """절 본문 → 불릿 한 줄씩. 인용문(`>`)은 설명이지 기록이 아니라 버린다."""
    out = []
    for line in body.splitlines():
        if re.match(r"^\s*>", line):
            continue
        if not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line):
            continue
        out.append(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).strip())
    return [b for b in out if b]


def _mentions(text, label):
    """`text` 안에 `label`이 **낱말로** 나오는가.

    앞은 낱말 문자면 안 되고(“비선형대수”의 “선형대수”는 다른 말), 뒤는 낱말 문자가
    아니거나 조사여야 한다(“신경망은”은 “신경망”이 맞다). 이 두 검사가 없으면
    "미분"이 "미분방정식"에 걸리는 식의 오탐이 쏟아진다.
    """
    if len(label) < 2:
        return False  # 한 글자 라벨은 오탐만 만든다
    start = 0
    while True:
        i = text.find(label, start)
        if i == -1:
            return False
        j = i + len(label)
        before_ok = i == 0 or not _WORDCHAR.match(text[i - 1])
        tail = text[j:]
        after_ok = (not tail) or (not _WORDCHAR.match(tail[0])) or tail.startswith(JOSA)
        if before_ok and after_ok:
            return True
        start = i + 1


def collect_sources(labels, daily_dir=DAILY_DIR):
    """개념 → 그 개념을 언급한 노트 줄들(원문 그대로).

    **긴 라벨 우선**으로 본다: 한 줄이 "선형대수"를 담고 있으면 "선형"은 그 줄을 가져가지
    못한다. 포함 관계로 생기는 오탐을 라벨 길이만으로 막는 값싼 방법이다.
    """
    sources = {lb: [] for lb in labels}
    by_len = sorted(labels, key=len, reverse=True)

    for path in sorted(glob.glob(os.path.join(daily_dir, "*.md"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for title, kind in SOURCE_SECTIONS:
            body = _section_body(text, title)
            if not body:
                continue
            for line in _bullets(body):
                claimed = []  # 이 줄을 이미 가져간 (더 긴) 라벨들
                for lb in by_len:
                    if any(lb in c for c in claimed):
                        continue  # 더 긴 라벨 안에 들어가는 말 — 그 라벨의 근거로 족하다
                    if _mentions(line, lb):
                        claimed.append(lb)
                        sources[lb].append({"file": path, "kind": kind, "match": line})
    return sources


def build(mastery, edges, domain_of, sources):
    """노드(개념) + 선수관계 + 분야 + 원문 근거 → concepts.json 구조."""
    labels = list(mastery.keys())
    for target, prereq in edges:  # 원장에 아직 없는 개념도 노드로 세운다
        for lb in (target, prereq):
            if lb not in labels:
                labels.append(lb)
    for lb in domain_of:  # 분류만 된 개념(화살표 없는 줄)도 개념이다
        if lb not in labels:
            labels.append(lb)

    id_of = {lb: slugify(lb) for lb in labels}
    prereq_of = {}
    for target, prereq in edges:
        prereq_of.setdefault(id_of[target], [])
        pid = id_of[prereq]
        if pid not in prereq_of[id_of[target]]:
            prereq_of[id_of[target]].append(pid)

    concepts = []
    for lb in labels:
        info = mastery.get(lb, {})
        cid = id_of[lb]
        # 원장의 증거 칸에 적힌 daily 링크도 근거로 친다(인용문 없이 파일만).
        collected = list(sources.get(lb, []))
        seen = {(s["file"], s["match"]) for s in collected}
        for m in re.finditer(r"[\w./-]*daily/[\w.\-가-힣]+\.md", info.get("evidence", "")):
            if (m.group(0), "") not in seen:
                collected.append({"file": m.group(0), "kind": "원장", "match": ""})
        concepts.append({
            "id": cid,
            "label": lb,
            "domain": domain_of.get(lb) or UNCLASSIFIED,
            "state": info.get("state", "미학습") or "미학습",
            "importance": (info.get("importance") or "M")[:1].upper() if info.get("importance") else "M",
            "prereq": prereq_of.get(cid, []),
            "sources": collected,
            "note": info.get("verified", ""),
        })
    return {"concepts": concepts}


def main():
    mastery = parse_mastery()
    edges, domain_of = parse_concept_map()

    labels = set(mastery.keys())
    for t, p in edges:
        labels.add(t)
        labels.add(p)
    sources = collect_sources(sorted(labels))

    data = build(mastery, edges, domain_of, sources)

    n = len(data["concepts"])
    n_edges = sum(len(c["prereq"]) for c in data["concepts"])
    n_mastered = sum(1 for c in data["concepts"] if c["state"] in MASTERED)
    n_sources = sum(len(c["sources"]) for c in data["concepts"])
    n_domains = len({c["domain"] for c in data["concepts"]} - {UNCLASSIFIED})

    if n == 0:
        print("개념 없음 — concepts.json 생성 건너뜀 (mastery.md가 비어 있고 개념 지도도 없음)")
        return 0

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
    print(
        f"✅ {OUT} — 개념 {n} · 선수관계 {n_edges} · 설명가능 {n_mastered} · "
        f"원문 근거 {n_sources}조각 · 분야 {n_domains}개"
    )
    if n_edges == 0:
        print("   ℹ️  선수관계가 아직 없다. 세션에서 러너가 `## 개념 지도`에 `A ← B` 를 남기면 쌓인다.")
    if n_domains == 0:
        print("   ℹ️  분야가 아직 없다. `## 개념 지도` 안에 `### 선형대수` 같은 소제목을 두면 분류된다.")
    if n_sources == 0:
        print("   ℹ️  원문 근거가 아직 없다. 노트 본문에서 개념 이름이 그대로 언급되면 자동으로 걸린다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
