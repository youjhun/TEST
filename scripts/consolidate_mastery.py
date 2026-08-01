#!/usr/bin/env python3
"""이해도 원장(mastery.md) 통합기 — create-only 승급 조각 → 파생 스냅샷.

왜: 러너(GPT/Claude)는 큰 mastery.md를 직접 수정할 수 없다 — GitHub Contents API가
파일 수정 시 **전체 파일 재전송**을 요구하고(부분 패치 없음), ChatGPT는 getContent
응답을 Code Interpreter로 넘기지 못해 안전한 재인코딩 경로가 없다(GPT는 특히 불가).
그래서 러너는 세션마다 작은 승급 파일 `<track>/mastery/YYYY-MM-DD-<slug>.md`를
**생성만** 한다. 이 스크립트가 그 조각들을 접어 mastery.md의 AUTO 표(마커 사이)를
재생성한다. 판단은 세션에 있던 러너의 몫이고, 이 스크립트는 재판단하지 않는다.

실행: CI(.github/workflows/mastery-consolidate.yml)가 mastery/ push 시 자동 실행.
로컬: `python3 scripts/consolidate_mastery.py` (repo 루트에서).

행 형식(6열): | 개념 | 상태 | 중요도 | 최근 검증일 | 증거 | 변화 메모 |
개념(1열) 기준 dedup, 검증일(4열) 최신 우선. 마커 밖(서사·상태정의)은 그대로 보존.
"""
import glob
import os
import re

START = "<!-- MASTERY-TABLE:START -->"
END = "<!-- MASTERY-TABLE:END -->"
HEADER = "| 개념 | 상태 | 중요도 | 최근 검증일 | 증거(daily 세션) | 변화 메모(무엇이·왜 바뀌었나) |"
SEP = "|---|---|---|---|---|---|"


def parse_rows(text):
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if first == "개념" or (first and set(first) <= set("-: ")):
            continue  # 헤더/구분선
        if not first:
            continue
        while len(cells) < 6:
            cells.append("")
        rows.append(cells[:6])
    return rows


def date_key(cells):
    m = re.search(r"\d{4}-\d{2}-\d{2}", cells[3] if len(cells) > 3 else "")
    return m.group(0) if m else "0000-00-00"


def consolidate(mastery_path):
    with open(mastery_path, encoding="utf-8") as f:
        content = f.read()
    if START not in content or END not in content:
        return False  # 마커 없는 파일은 건드리지 않음

    pre, rest = content.split(START, 1)
    table_part, post = rest.split(END, 1)

    order = []
    merged = {}

    def apply(rows):
        for row in rows:
            c = row[0]
            if c not in merged:
                order.append(c)
                merged[c] = row
            elif date_key(row) >= date_key(merged[c]):
                merged[c] = row

    apply(parse_rows(table_part))  # 기존 표 = baseline

    mdir = os.path.join(os.path.dirname(mastery_path), "mastery")
    for pf in sorted(glob.glob(os.path.join(mdir, "*.md"))):
        if os.path.basename(pf).lower() == "readme.md":
            continue
        with open(pf, encoding="utf-8") as f:
            apply(parse_rows(f.read()))

    table = [HEADER, SEP] + ["| " + " | ".join(merged[c]) + " |" for c in order]
    new = pre + START + "\n" + "\n".join(table) + "\n" + END + post
    if new != content:
        with open(mastery_path, "w", encoding="utf-8") as f:
            f.write(new)
        return True
    return False


def main():
    changed = [mp for mp in glob.glob("**/mastery.md", recursive=True) + glob.glob("mastery.md") if consolidate(mp)]
    for c in changed:
        print(f"updated: {c}")
    print(f"done ({len(changed)} changed)")


if __name__ == "__main__":
    main()
