#!/usr/bin/env python3
"""논문 스케줄러 — `topics.yaml`의 주제로 새 논문을 찾아 `papers/`에 모은다.

왜 이 경로인가:
"반도체 OO lab" 같은 연구 주제를 걸어 두면, 매주 그 분야의 새 논문이 자동으로
학습 큐에 들어와야 한다. 그런데 서버를 두면 그 순간 운영·비용·계정이 생긴다.
그래서 **각자 repo의 GitHub Actions**가 주 1회 돌며 공개 API(OpenAlex — 키 불필요)를
조회하고, 결과를 `papers/`에 커밋한다. 서버 0, 비용 0, LLM 토큰 0.

읽는 사람: 러너(Custom GPT)가 세션 시작 시 `papers/inbox.md`를 읽어 이번 주 새 논문을
학습에 엮는다. 사람도 Issue 알림으로 본다.

실행: python3 scripts/scan_papers.py            (CI가 이렇게 부른다)
      python3 scripts/scan_papers.py --dry-run  (네트워크 없이 로직만 확인)
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request

TOPICS_PATH = "topics.yaml"
PAPERS_DIR = "papers"
SEEN_PATH = os.path.join(PAPERS_DIR, "seen.json")
INBOX_PATH = os.path.join(PAPERS_DIR, "inbox.md")
OPENALEX = "https://api.openalex.org/works"
UA = "Socralearner/1.0 (learning repo; https://github.com/topics/socralearner)"


# ---------------------------------------------------------------- 유틸 (순수)

def today_iso():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date().isoformat()


def load_yaml(path):
    """의존성 없이 쓰기 위한 아주 작은 YAML 로더 대체 — pyyaml이 있으면 그걸 쓴다."""
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    # pyyaml이 없을 때의 최소 파서 (topics.yaml 형태만 지원)
    data = {"topics": []}
    cur = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = re.match(r"^(\w+):\s*(.*)$", line)
            if m and not line.startswith(" "):
                key, val = m.group(1), m.group(2).strip()
                if val:
                    data[key] = int(val) if val.isdigit() else val.strip('"\'')
                continue
            m = re.match(r"^\s*-\s*(\w+):\s*(.*)$", line)
            if m:
                cur = {m.group(1): m.group(2).strip().strip('"\'')}
                data["topics"].append(cur)
                continue
            m = re.match(r"^\s+(\w+):\s*(.*)$", line)
            if m and cur is not None:
                cur[m.group(1)] = m.group(2).strip().strip('"\'')
    return data


def normalize_work(w):
    """OpenAlex work → 우리가 쓰는 얇은 형태. 초록은 역색인이라 복원한다."""
    inv = w.get("abstract_inverted_index") or {}
    abstract = ""
    if inv:
        positions = []
        for word, idxs in inv.items():
            for i in idxs:
                positions.append((i, word))
        abstract = " ".join(w for _, w in sorted(positions))[:400]
    authors = [
        a.get("author", {}).get("display_name", "")
        for a in (w.get("authorships") or [])[:3]
    ]
    loc = (w.get("primary_location") or {})
    return {
        "id": w.get("id", ""),
        "title": (w.get("title") or "").strip(),
        "date": w.get("publication_date", ""),
        "citations": w.get("cited_by_count", 0),
        "authors": [a for a in authors if a],
        "venue": (loc.get("source") or {}).get("display_name", "") or "",
        "url": (w.get("doi") or loc.get("landing_page_url") or w.get("id") or ""),
        "abstract": abstract,
    }


def build_inbox(date, found, window_days):
    """주제별 새 논문 → inbox 마크다운 (순수 — 테스트 가능)."""
    total = sum(len(v) for v in found.values())
    lines = [
        "---",
        f"title: \"논문 인박스 — {date}\"",
        f"updated: {date}",
        "kind: papers",
        "---",
        "",
        f"# 📬 논문 인박스 — {date}",
        "",
        f"> 최근 {window_days}일 이내에 나온 새 논문 **{total}편**. (자동 수집: `topics.yaml` 기준)",
        "> 세션에서 *\"이번 주 새 논문 같이 보자\"* 라고 하면 러너가 여기서 골라 준다.",
        "> 다 읽을 필요 없다 — **제목과 초록만 훑고 1편만 골라** 깊게 보는 편이 낫다.",
        "",
    ]
    if total == 0:
        lines += ["이번 주는 새로 걸린 논문이 없다. (주제를 넓히려면 `topics.yaml`의 query를 손보면 된다.)", ""]
        return "\n".join(lines)

    for label, papers in found.items():
        if not papers:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for p in papers:
            who = ", ".join(p["authors"]) + (" 외" if len(p["authors"]) >= 3 else "")
            meta = " · ".join(x for x in [p["date"], who, p["venue"]] if x)
            lines.append(f"### [{p['title']}]({p['url']})")
            lines.append(f"<sub>{meta}</sub>")
            lines.append("")
            if p["abstract"]:
                lines.append(f"> {p['abstract']}")
                lines.append("")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 네트워크

def fetch_topic(query, window_days, limit):
    since = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    params = {
        "search": query,
        "filter": f"from_publication_date:{since}",
        "sort": "cited_by_count:desc",
        "per-page": str(max(1, min(limit * 2, 25))),
    }
    url = f"{OPENALEX}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    return [normalize_work(w) for w in data.get("results", [])]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="topics.yaml → papers/inbox.md 논문 수집기")
    ap.add_argument("--dry-run", action="store_true", help="네트워크 없이 로직만 확인")
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    if not os.path.exists(TOPICS_PATH):
        print("topics.yaml 없음 — 건너뜀")
        return 0

    cfg = load_yaml(TOPICS_PATH)
    topics = [t for t in (cfg.get("topics") or []) if t.get("query")]
    if not topics:
        print("topics 비어 있음 — 건너뜀 (topics.yaml에 주제를 추가하세요)")
        return 0

    window = int(cfg.get("window_days", 14) or 14)
    per_topic = int(cfg.get("max_per_topic", 5) or 5)
    date = args.today or today_iso()

    os.makedirs(PAPERS_DIR, exist_ok=True)
    seen = {}
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                seen = json.load(f)
        except (json.JSONDecodeError, OSError):
            seen = {}

    found, new_ids = {}, []
    for t in topics:
        label = t.get("label") or t.get("id") or t["query"]
        try:
            papers = [] if args.dry_run else fetch_topic(t["query"], window, per_topic)
        except Exception as e:  # 한 주제가 실패해도 나머지는 계속 (조용한 전면 실패 금지)
            print(f"⚠️  '{label}' 조회 실패: {e}", file=sys.stderr)
            found[label] = []
            continue
        fresh = [p for p in papers if p["id"] and p["id"] not in seen][:per_topic]
        for p in fresh:
            new_ids.append(p["id"])
        found[label] = fresh
        print(f"- {label}: 새 논문 {len(fresh)}편")

    total = sum(len(v) for v in found.values())
    with open(INBOX_PATH, "w", encoding="utf-8") as f:
        f.write(build_inbox(date, found, window))

    for i in new_ids:
        seen[i] = date
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)

    print(f"✅ {INBOX_PATH} 갱신 — 새 논문 {total}편 (누적 확인 {len(seen)}편)")
    # 워크플로가 Issue 알림 여부를 판단할 수 있게 출력
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"new_count={total}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
