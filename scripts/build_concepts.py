#!/usr/bin/env python3
"""개념 지도 빌더 — `mastery.md` + `daily/**`의 `## 개념 지도` → `concepts.json`.

왜 이 파일이 필요한가:
학습 기록(mastery.md)에는 **개념과 이해 상태**가 있지만 **선수관계(edges)** 가 없다.
그래서 "아직 설명 못 하는데 다른 개념을 막고 있는" **막힌 길목**을 계산할 수 없다.
러너가 세션마다 `## 개념 지도` 절에 `A ← B, C`(A의 선수 개념은 B와 C) 한두 줄만 남기면,
이 스크립트가 그것을 모아 그래프로 만든다.

산출물 `concepts.json`은 Topdown 앱이 그대로 읽는 형식이다(개념 그래프·막힌 길목 시각화).
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


def parse_edges(daily_dir=DAILY_DIR):
    """daily 노트의 `## 개념 지도` 절에서 `A ← B, C` 형태를 모은다 → [(대상, 선수)]."""
    edges = []
    for path in sorted(glob.glob(os.path.join(daily_dir, "*.md"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^##\s*.*%s.*$" % re.escape(SECTION), text, re.M)
        if not m:
            continue
        body = text[m.end():]
        nxt = re.search(r"^##\s", body, re.M)
        if nxt:
            body = body[: nxt.start()]
        for line in body.splitlines():
            line = line.strip().lstrip("-*").strip()
            if not line:
                continue
            # `A ← B, C`  (화살표는 ←, <-, <= 를 허용)
            parts = re.split(r"←|<-|<=", line, maxsplit=1)
            if len(parts) != 2:
                continue
            target = parts[0].strip().strip("`")
            for prereq in parts[1].split(","):
                prereq = prereq.strip().strip("`")
                if target and prereq and target != prereq:
                    edges.append((target, prereq))
    return edges


def build(mastery, edges):
    """노드(개념) + 선수관계 → concepts.json 구조 (Topdown importer가 읽는 형식)."""
    labels = list(mastery.keys())
    for target, prereq in edges:  # 원장에 아직 없는 개념도 노드로 세운다
        for lb in (target, prereq):
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
        sources = []
        ev = info.get("evidence", "")
        for m in re.finditer(r"[\w./-]*daily/[\w.\-가-힣]+\.md", ev):
            sources.append({"file": m.group(0), "kind": "증거", "match": ""})
        concepts.append({
            "id": cid,
            "label": lb,
            "domain": "learning",
            "state": info.get("state", "미학습") or "미학습",
            "importance": (info.get("importance") or "M")[:1].upper() if info.get("importance") else "M",
            "prereq": prereq_of.get(cid, []),
            "sources": sources,
            "note": info.get("verified", ""),
        })
    return {"concepts": concepts}


def main():
    mastery = parse_mastery()
    edges = parse_edges()
    data = build(mastery, edges)

    n = len(data["concepts"])
    n_edges = sum(len(c["prereq"]) for c in data["concepts"])
    n_mastered = sum(1 for c in data["concepts"] if c["state"] in MASTERED)

    if n == 0:
        print("개념 없음 — concepts.json 생성 건너뜀 (mastery.md가 비어 있고 개념 지도도 없음)")
        return 0

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
    print(f"✅ {OUT} — 개념 {n} · 선수관계 {n_edges} · 설명가능 {n_mastered}")
    if n_edges == 0:
        print("   ℹ️  선수관계가 아직 없다. 세션에서 러너가 `## 개념 지도`에 `A ← B` 를 남기면 쌓인다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
