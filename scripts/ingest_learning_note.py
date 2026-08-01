#!/usr/bin/env python3
"""학습 노트 수집기 — GitHub Issue(본문 + 코멘트) → `daily/` 세션 로그.

왜 이 경로인가:
러너(특히 ChatGPT Custom GPT)의 기존 쓰기 경로는 GitHub Contents API였다. 그 API는
파일 생성·수정 시 **본문 전체를 base64 한 덩어리로** 요구한다(부분 패치 없음). 즉 노트
본문이 모델의 출력 토큰을 두 번(마크다운 → base64) 통과해야 하고, 길어질수록
① 인코딩이 어긋나거나 ② 인자 문자열이 잘려 JSON이 깨지고 ③ 수정은 blob sha까지
읽어와야 해서 실패한다. 그 결과 긴 세션일수록 커밋이 안 되고, 러너는 살아남는 길이인
"3줄 요약"으로 스스로 줄였다(2026-07-25 status-delta 파일이 그 흔적).
`consolidate_mastery.py`가 mastery.md에 대해 이미 같은 문제를 조각+CI로 풀었고,
이 스크립트는 그 패턴을 **세션 로그 생성 경로 전체**로 확장한다.

새 계약: 러너는 Issue에 **평문**을 쓴다(base64 없음, sha 없음, 읽기-수정-쓰기 없음).
길면 코멘트로 이어 쓴다 — 요청 하나하나가 짧아 길이가 실패 원인이 되지 않는다.
파일을 만드는 것은 모델이 아니라 이 결정론적 스크립트다(LLM 토큰 0).

실행: CI(.github/workflows/learning-note-ingest.yml)가 Issue 종료 또는 `/기록` 코멘트에 실행.
로컬: `python3 scripts/ingest_learning_note.py --payload payload.json --today 2026-08-01`

payload.json 형식:
    {"number": 12, "title": "[학습] 2026-08-01 lda-scatter", "body": "...", "html_url": "...",
     "comments": [{"author": "youjhun", "body": "..."}]}
"""
import argparse
import datetime
import json
import os
import re
import sys

DAILY_DIR = "daily"
STATUS_PATH = "STATUS.md"
LEARNING_ROOT = "."

# 대시보드(dashboard/lib/data/learning.ts)와 음성 로더가 키로 삼는 정규 헤딩.
# 없으면 "기록됐는데 안 읽히는" 조용한 실패가 되므로, 막지 않고 채워 넣고 경고한다.
REQUIRED_HEADINGS = ["오늘 직접 학습한 지식", "취약 영역", "다음 복습 질문"]

STATUS_SECTION = "STATUS 갱신"
MASTERY_SECTION = "이해도 승급"
BOT_SUFFIX = "[bot]"
COMMAND_PREFIX = ("/기록", "/ingest", "/skip", "<!-- ingest")


# --------------------------------------------------------------------------- 유틸


def kst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date().isoformat()


def split_frontmatter(text):
    """(frontmatter dict, 본문) — frontmatter가 없으면 ({}, 원문)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return {}, text
    head = parts[0][3:]
    rest = parts[1].lstrip("-").lstrip("\n") if len(parts) == 2 else parts[2].lstrip("\n")
    fm = {}
    for line in head.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line.strip())
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm, rest


def slugify(text):
    """ASCII 슬러그. 한글만 있으면 빈 문자열(호출부가 대체값을 쓴다)."""
    text = re.sub(r"\[[^\]]*\]", " ", text)          # [학습] 같은 말머리 제거
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)   # 날짜 제거
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    return "-".join(tokens)[:60].strip("-")


def normalize_heading(text):
    """헤딩 비교용 정규화 — 이모지·괄호주석·공백·마크다운 강조 제거."""
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^\w가-힣]", "", text)
    return text


# --------------------------------------------------------------------------- 조립


def assemble(payload):
    """Issue 본문 + 코멘트를 순서대로 이어 붙인다(청크 프로토콜)."""
    chunks = [payload.get("body") or ""]
    for c in payload.get("comments") or []:
        author = (c.get("author") or "")
        body = (c.get("body") or "").strip()
        if not body or author.endswith(BOT_SUFFIX):
            continue
        if body.startswith(COMMAND_PREFIX):
            continue
        chunks.append(body)
    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())


def pop_section(body, name):
    """`## <name>` 절을 본문에서 떼어내 (남은 본문, 절 내용)으로 돌려준다."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and name in line:
            start = i
            break
    if start is None:
        return body, ""

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = "\n".join(lines[start + 1:end]).strip()
    return "\n".join(lines[:start] + lines[end:]).strip(), section


def extract_status_patch(body):
    """`## STATUS 갱신` 절을 본문에서 떼어내 {섹션: 내용} 으로 돌려준다."""
    body, section = pop_section(body, STATUS_SECTION)
    if not section:
        return body, {}

    patch, current = {}, None
    for line in section.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            patch[current] = []
        elif current is not None:
            patch[current].append(line)
    patch = {k: "\n".join(v).strip() for k, v in patch.items() if "\n".join(v).strip()}
    return body, patch


def ensure_headings(body):
    """정규 헤딩이 없으면 자리만 만들어 둔다 — 기록을 막는 대신 경고한다."""
    present = {normalize_heading(m) for m in re.findall(r"^#{1,6}\s*(.+)$", body, re.M)}
    missing = []
    for heading in REQUIRED_HEADINGS:
        key = normalize_heading(heading)
        if not any(key in p for p in present):
            missing.append(heading)
    if missing:
        extra = ["", "> ⚠️ 아래 헤딩은 수집기가 자동 보정했다 — 세션에 실제 기록이 없었다는 뜻이다."]
        for heading in missing:
            extra += ["", f"## {heading}", "- (이번 세션 기록 없음)"]
        body = body.rstrip() + "\n" + "\n".join(extra) + "\n"
    return body, missing


def build_note(payload, today):
    raw = assemble(payload)
    user_fm, body = split_frontmatter(raw)

    # 본문 맨 앞의 지시행(slug:, runner:)도 frontmatter처럼 취급하고 제거한다.
    directives = {}
    lines = body.splitlines()
    while lines and re.match(r"^(slug|runner|course|week|exam_target|tags|track)\s*:\s*\S", lines[0].strip()):
        key, value = lines[0].split(":", 1)
        directives[key.strip()] = value.strip()
        lines.pop(0)
    body = "\n".join(lines).strip()
    user_fm = {**directives, **user_fm}

    body, status_patch = extract_status_patch(body)
    body, mastery = pop_section(body, MASTERY_SECTION)
    body, missing = ensure_headings(body)

    # 제목 규약: `[학습] YYYY-MM-DD <slug> — <한 줄 제목>` (뒷부분은 선택)
    title = payload.get("title") or ""
    date = (re.search(r"\d{4}-\d{2}-\d{2}", title) or re.search(r"\d{4}-\d{2}-\d{2}", user_fm.get("created", "")))
    date = date.group(0) if date else today

    head, _, tail = title.partition("—")
    if not tail:
        head, _, tail = title.partition(" - ")
    slug = user_fm.get("slug") or slugify(head) or slugify(title) or f"session-{payload.get('number', '0')}"

    display = (tail or head).strip()
    display = re.sub(r"^\s*\[[^\]]*\]\s*", "", display).strip()
    display = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", display).strip() or slug

    tags = user_fm.get("tags") or "[learning]"
    if not tags.startswith("["):
        tags = "[" + tags + "]"

    fm = [
        "---",
        f'title: "{user_fm.get("title", display)}"',
        f"created: {date}",
        f"updated: {today}",
        f"tags: {tags}",
        f'source: "학습 세션 → Issue #{payload.get("number")} (수집기: ingest_learning_note.py)"',
        "status: active",
        "kind: mixed",
        f'runner: {user_fm.get("runner", "gpt")}',
        f"source_issue: {payload.get('number')}",
    ]
    for key in ("course", "week", "exam_target"):
        if user_fm.get(key):
            fm.append(f"{key}: {user_fm[key]}")
    fm.append("---")

    heading = f"# {user_fm.get('title', display)}"
    if not re.match(r"^#\s", body):
        body = heading + "\n\n" + body

    return {
        "date": date,
        "slug": slug,
        "content": "\n".join(fm) + "\n\n" + body.rstrip() + "\n",
        "status_patch": status_patch,
        "mastery": mastery,
        "track": user_fm.get("track", ""),
        "missing": missing,
    }


# --------------------------------------------------------------------------- 쓰기


def target_path(date, slug, issue_number):
    """같은 Issue를 다시 수집하면 같은 파일을 덮어쓴다(재실행 안전)."""
    marker = f"source_issue: {issue_number}"
    if os.path.isdir(DAILY_DIR):
        for name in sorted(os.listdir(DAILY_DIR)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(DAILY_DIR, name)
            with open(path, encoding="utf-8") as f:
                if marker in f.read(2000):
                    return path

    base = os.path.join(DAILY_DIR, f"{date}-{slug}.md")
    if not os.path.exists(base):
        return base
    for n in range(2, 20):
        candidate = os.path.join(DAILY_DIR, f"{date}-{slug}-{n}.md")
        if not os.path.exists(candidate):
            return candidate
    raise SystemExit(f"경로 충돌: {base}")


def write_mastery_fragment(section, track, date, slug, note_path):
    """`## 이해도 승급` 표를 create-only 조각으로 떨군다 → consolidate_mastery.py가 접는다.

    러너는 여기서도 큰 mastery.md를 건드리지 않는다. 판단(승급 여부)은 세션의 몫이고
    이 함수는 옮겨 적기만 한다.
    """
    if not section.strip():
        return None

    lines = section.splitlines()
    if lines and lines[0].strip().startswith("track:"):
        track = track or lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
    section = "\n".join(lines).strip()
    if not section:
        return None

    if not track:
        ledgers = []
        for root, _dirs, files in os.walk(LEARNING_ROOT):
            if "mastery.md" in files:
                ledgers.append(os.path.relpath(root, LEARNING_ROOT))
        if len(ledgers) != 1:
            return f"⚠️ 이해도 승급을 건너뛰었다 — `track:`이 없고 원장 후보가 {len(ledgers)}개다."
        track = ledgers[0]

    mdir = os.path.join(LEARNING_ROOT, track, "mastery")
    if not os.path.isdir(os.path.join(LEARNING_ROOT, track)):
        return f"⚠️ 이해도 승급을 건너뛰었다 — 트랙 경로 없음: `{track}`"
    os.makedirs(mdir, exist_ok=True)

    path = os.path.join(mdir, f"{date}-{slug}.md")
    header = [
        "---",
        f'title: "{date} 이해도 승급 — {slug}"',
        f"created: {date}",
        "tags: [learning, mastery, promotion]",
        f'source: "{note_path}"',
        "kind: fact",
        "---",
        "",
        f"> 세션 근거: [[{note_path[:-3]}]]",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header) + "\n" + section.strip() + "\n")
    return path


def apply_status_patch(patch, today):
    """STATUS.md의 `## ` 절 본문을 통째로 교체한다 — 안내용 인용(>)줄은 보존."""
    if not patch or not os.path.exists(STATUS_PATH):
        return []
    with open(STATUS_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()

    applied = []
    for section, content in patch.items():
        key = normalize_heading(section)
        idx = None
        for i, line in enumerate(lines):
            if line.startswith("## ") and key and key in normalize_heading(line[3:]):
                idx = i
                break
        if idx is None:
            continue

        end = len(lines)
        for i in range(idx + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break

        keep = []
        for line in lines[idx + 1:end]:
            if line.strip() == "" or line.lstrip().startswith(">"):
                keep.append(line)
            else:
                break
        while keep and keep[-1].strip() == "":
            keep.pop()

        lines[idx + 1:end] = (keep or [""]) + [content, ""]
        applied.append(section)

    if applied:
        for i, line in enumerate(lines[:20]):
            if line.startswith("updated:"):
                lines[i] = f"updated: {today}"
                break
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    return applied


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Issue → daily 세션 로그 수집기")
    ap.add_argument("--payload", required=True, help="Issue payload JSON 경로")
    ap.add_argument("--today", default=kst_today(), help="KST 기준 오늘 (YYYY-MM-DD)")
    ap.add_argument("--report", help="사람이 읽을 결과 보고서를 쓸 경로(Issue 코멘트용)")
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    args = ap.parse_args()

    with open(args.payload, encoding="utf-8") as f:
        payload = json.load(f)

    note = build_note(payload, args.today)
    if len(note["content"].strip().splitlines()) < 4:
        raise SystemExit("본문이 비었다 — 수집할 내용이 없다.")

    path = target_path(note["date"], note["slug"], payload.get("number"))
    if args.dry_run:
        print(f"[dry-run] {path}\n")
        print(note["content"])
        return

    os.makedirs(DAILY_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(note["content"])
    applied = apply_status_patch(note["status_patch"], args.today)
    promoted = write_mastery_fragment(note["mastery"], note["track"], note["date"], note["slug"], path)

    lines_written = len(note["content"].splitlines())
    report = [
        f"✅ 학습 노트 기록 완료 — `{path}` ({lines_written}줄)",
        "",
        f"- 수집 조각: 본문 1 + 코멘트 {len(payload.get('comments') or [])}개",
    ]
    if applied:
        report.append(f"- STATUS.md 갱신: {', '.join(applied)}")
    if promoted:
        report.append(f"- 이해도 승급 조각: `{promoted}`" if promoted.endswith(".md") else f"- {promoted}")
    if note["missing"]:
        report.append(f"- ⚠️ 정규 헤딩 자동 보정: {', '.join(note['missing'])} — 다음 세션에서 실제로 채울 것")
    report += [
        "",
        "> 이 노트는 모델이 아니라 CI가 결정론적으로 썼다(base64·sha 경로 없음). "
        "길이 때문에 커밋이 실패하지 않는다.",
    ]
    text = "\n".join(report)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"path={path}\n")


if __name__ == "__main__":
    sys.exit(main())
