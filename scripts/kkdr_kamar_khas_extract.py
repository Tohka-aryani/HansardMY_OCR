#!/usr/bin/env python3
"""
Kamar Khas (Special Chamber) Hansard — proceedings extraction (KKDR series)
===========================================================================
Targets official PDFs like ``KKDR-03032026-1.pdf``: text layer (not attendance
scraping). Produces structured speaker turns, topics, timestamps, and metadata.

Layout (from Parliamentary Debates Special Chamber):
  • Body page header: ``KKDR.D.M.YYYY`` + page no.
  • Chair: ``Timbalan Yang di-Pertua [Name]: …``
  • MPs: ``<Name> [<constituency>]: …``
  • Ministers: ``Menteri … [<name>]:`` / ``Timbalan Menteri … [<name>]:``
  • Some replies omit portfolio and use ``<Name>:`` only; we pick those up with
    a conservative “titled name at line start” rule after the body begins.

Install (same family as ``hansard_ocr.py``):
    pip install pdfplumber
Fallback: ``pdftotext`` from poppler-utils if pdfplumber is missing.

Usage (from repo root):

    python scripts/kkdr_kamar_khas_extract.py [path/to/KKDR-….pdf] [output_dir]

Defaults: ``data/pdfs/KKDR-03032026-1.pdf`` → ``output/kkdr/``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PDFS = REPO_ROOT / "data" / "pdfs"
OUTPUT_KKDR = REPO_ROOT / "output" / "kkdr"


# ─── Shared date helper (aligned with hansard_ocr.py) ─────────────────────────

DATE_PATTERN = re.compile(
    r"(isnin|selasa|rabu|khamis|jumaat|sabtu|ahad|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"[,\s]+(\d{1,2})\s+"
    r"(januari|februari|mac|april|mei|jun|julai|ogos|september|oktober|november|disember|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)
MONTH_MAP = {
    "januari": "01",
    "january": "01",
    "februari": "02",
    "february": "02",
    "mac": "03",
    "march": "03",
    "april": "04",
    "mei": "05",
    "may": "05",
    "jun": "06",
    "june": "06",
    "julai": "07",
    "july": "07",
    "ogos": "08",
    "august": "08",
    "september": "09",
    "oktober": "10",
    "october": "10",
    "november": "11",
    "disember": "12",
    "december": "12",
}


def detect_sitting_date(text: str) -> str:
    m = DATE_PATTERN.search(text[:6000])
    if m:
        day = m.group(2).zfill(2)
        month = MONTH_MAP.get(m.group(3).lower(), "00")
        year = m.group(4)
        return f"{year}-{month}-{day}"
    return datetime.today().strftime("%Y-%m-%d")


def clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).replace("\u00ad", "").strip()


# ─── Page text ───────────────────────────────────────────────────────────────

def extract_pages_pdfplumber(pdf_path: str) -> list[tuple[int, str]]:
    import pdfplumber

    out: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            out.append((i + 1, page.extract_text() or ""))
    return out


def extract_pages_pdftotext(pdf_path: str) -> list[tuple[int, str]]:
    raw = subprocess.check_output(["pdftotext", pdf_path, "-"], stderr=subprocess.DEVNULL)
    text = raw.decode("utf-8", errors="replace")
    chunks = text.split("\f")
    pages: list[tuple[int, str]] = []
    for i, chunk in enumerate(chunks):
        c = chunk.strip()
        if c:
            pages.append((i + 1, c))
    return pages


def extract_pages(pdf_path: str) -> list[tuple[int, str]]:
    try:
        return extract_pages_pdfplumber(pdf_path)
    except ImportError:
        return extract_pages_pdftotext(pdf_path)


# ─── Body detection & noise stripping ──────────────────────────────────────────

KKDR_HEADER_LINE = re.compile(r"^KKDR\.\s*\d{1,2}\.\d{1,2}\.\d{4}\s*$", re.I)
PAGE_NUMBER_ONLY = re.compile(r"^\d{1,3}\s*$")
MALAYSIA_BLOCK = re.compile(r"^MALAYSIA\s*$", re.M)


def strip_page_boilerplate(page_text: str) -> str:
    """Remove repeated masthead lines; keep substance."""
    lines = page_text.splitlines()
    out: list[str] = []
    skip_next_blank = False
    for ln in lines:
        s = ln.strip()
        if KKDR_HEADER_LINE.match(s) or PAGE_NUMBER_ONLY.match(s):
            continue
        if s in {
            "MALAYSIA",
            "KAMAR KHAS",
            "Special Chamber",
            "PARLIMEN KELIMA BELAS",
            "PENGGAL KELIMA",
            "MESYUARAT PERTAMA",
            "PENYATA RASMI PARLIMEN",
            "Parliamentary Debates",
        }:
            continue
        out.append(ln)
    return "\n".join(out).strip()


def first_body_page_index(pages: list[tuple[int, str]]) -> int:
    for idx, (_, t) in enumerate(pages):
        for ln in t.splitlines():
            ls = ln.strip()
            if KKDR_HEADER_LINE.match(ls):
                return idx
    return 0


# ─── Topic & timestamp ───────────────────────────────────────────────────────

TIMESTAMP_LINE = re.compile(
    r"^\s*(\d{1,2}\.\d{2})\s*(ptg|pg|pagi|malam|tengahari)\.?\s*$",
    re.IGNORECASE,
)

PROCEDURAL_BRACKET = re.compile(r"^\[[^\]]+\]\s*$")

_SPEAKER_LEAD_LINE = re.compile(
    r"^(?:Timbalan\s+)?(?:Yang\s+di-Pertua|Menteri|Datuk|Dato\s*\'?|Dato\.?|"
    r"Tuan|Puan|Dr\.|Prof\.|YB\.?|Senator)\b",
    re.IGNORECASE,
)


def is_topic_primary_line(line: str) -> bool:
    """
    First line of a printed motion heading: mostly capitals, long enough to avoid
    matching normal prose (but short enough to allow two-word tails like
    ``KAWASAN TUARAN`` when they appear alone — those are handled via
    ``is_topic_continuation_line`` after a block has started).
    """
    s = line.strip()
    if len(s) < 14:
        return False
    letters = re.sub(r"[^A-Za-z]", "", s)
    if len(letters) < 12:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.85


def is_topic_continuation_line(line: str) -> bool:
    """
    Extra lines of the same block-caps heading (e.g. ``KAWASAN TUARAN``) or
    mid-title fragments below the 14-character primary threshold.
    """
    s = line.strip()
    if len(s) < 5 or len(s) > 140:
        return False
    if TIMESTAMP_LINE.match(s):
        return False
    if _SPEAKER_LEAD_LINE.match(s):
        return False
    if PROCEDURAL_BRACKET.match(s):
        return False
    letters = re.sub(r"[^A-Za-z]", "", s)
    if len(letters) < 4:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.72


# ─── Speaker turn detection ───────────────────────────────────────────────────

@dataclass
class RawMatch:
    kind: str  # chair | minister | member | bare_titled
    start: int
    end: int
    portfolio_or_role: str
    person_or_constituency: str
    full_label: str


# Chair (must be before generic bracket patterns)
_RE_CHAIR = re.compile(
    r"(?:^|\n)\s*(Timbalan\s+Yang\s+di-Pertua)\s*\[([^\]\n]+)\]\s*:\s*",
    re.IGNORECASE,
)
# Portfolio + minister name in brackets
_RE_MINISTER = re.compile(
    r"(?:^|\n)\s*((?:Timbalan\s+)?Menteri\s[^\n\[]+?)\s*\[([^\]\n]+)\]\s*:\s*",
    re.IGNORECASE,
)
# MP / guest: titled name + [constituency] — exclude lines already matched as Menteri
_RE_MEMBER = re.compile(
    r"(?:^|\n)\s*("
    r"(?:YB\.?\s+)?"
    r"(?:Datuk\s+Seri\s+Panglima|Datuk|Dato\s*\'?|Dato\.?|"
    r"Tuan|Puan|Dr\.|Prof\.|Senator)\s+"
    r"[^\n\[]+?"
    r")\s*\[([^\]\n]+)\]\s*:\s*",
    re.IGNORECASE,
)
# Second speech line without constituency, e.g. ``Datuk Hajah Rubiah binti Haji Wang:``
_RE_BARE_TITLED = re.compile(
    r"(?:^|\n)\s*("
    r"(?:Dato\.?\s*|Datuk\s+|Dr\.?\s+|Tuan\s+|Puan\s+|Prof\.?\s+|Senator\s+)"
    r"(?:[A-Za-z\.'\-]+\s+){1,6}[A-Za-z\.'\-]+"
    r")\s*:\s*",
    re.IGNORECASE,
)


def iter_speaker_matches(text: str) -> Iterator[RawMatch]:
    found: list[RawMatch] = []

    for m in _RE_CHAIR.finditer(text):
        found.append(
            RawMatch(
                kind="chair",
                start=m.start(0),
                end=m.end(0),
                portfolio_or_role=m.group(1).strip(),
                person_or_constituency=m.group(2).strip(),
                full_label=f"{m.group(1).strip()} [{m.group(2).strip()}]:",
            )
        )
    for m in _RE_MINISTER.finditer(text):
        found.append(
            RawMatch(
                kind="minister",
                start=m.start(0),
                end=m.end(0),
                portfolio_or_role=m.group(1).strip(),
                person_or_constituency=m.group(2).strip(),
                full_label=f"{m.group(1).strip()} [{m.group(2).strip()}]:",
            )
        )
    for m in _RE_MEMBER.finditer(text):
        found.append(
            RawMatch(
                kind="member",
                start=m.start(0),
                end=m.end(0),
                portfolio_or_role="",
                person_or_constituency=m.group(2).strip(),
                full_label=f"{m.group(1).strip()} [{m.group(2).strip()}]:",
            )
        )
    for m in _RE_BARE_TITLED.finditer(text):
        label = m.group(1).strip()
        low = label.lower()
        if any(
            x in low
            for x in (
                "untuk ",
                "bagi ",
                "manakala ",
                "berdasarkan ",
                "seterusnya ",
                "akhirnya ",
                "sehubungan ",
            )
        ):
            continue
        if len(label) > 120:
            continue
        found.append(
            RawMatch(
                kind="bare_titled",
                start=m.start(0),
                end=m.end(0),
                portfolio_or_role="",
                person_or_constituency=label,
                full_label=label + ":",
            )
        )

    # Prefer earlier start, then longer span (more specific match at same anchor).
    found.sort(key=lambda x: (x.start, -(x.end - x.start)))
    kept: list[RawMatch] = []
    last_end = -1
    for r in found:
        if r.start < last_end:
            continue
        kept.append(r)
        last_end = r.end
    for r in kept:
        yield r


@dataclass
class Turn:
    index: int
    page: int
    topic: str
    timestamp: str
    role: str
    speaker_display: str
    portfolio: str
    constituency: str
    person_name: str
    speech: str
    raw_label: str
    meta: dict = field(default_factory=dict)


def role_for_match(m: RawMatch) -> str:
    if m.kind == "chair":
        return "chair"
    if m.kind == "minister":
        return "minister"
    if m.kind == "member":
        return "member"
    return "member_or_minister"


def merge_wrapped_bracket_lines(text: str) -> str:
    """
    pdfplumber / pdftotext often breaks ``[Name`` and ``binti …]:`` across lines.
    Glue those continuations so speaker regexes match.
    """
    lines = text.splitlines()
    out: list[str] = []
    buf = ""
    for raw in lines:
        line = raw.rstrip()
        if not buf:
            buf = line
            continue
        if re.search(r"\[[^\]]*$", buf.rstrip()) and line:
            buf = buf.rstrip() + " " + line
            continue
        out.append(buf)
        buf = line
    if buf:
        out.append(buf)
    return "\n".join(out)


def _merge_body_with_page_map(
    pages: list[tuple[int, str]], body_start_idx: int
) -> tuple[str, list[tuple[int, int, int]]]:
    """
    Concatenate body pages into one string. ``spans`` lists (char_lo, char_hi, page_no)
    so any offset in ``text`` maps to a PDF page (using the turn's match start).

    Bracket wraps are fixed **per page** before join so character offsets stay valid.
    """
    spans: list[tuple[int, int, int]] = []
    parts: list[str] = []
    pos = 0
    sep = "\n\n"
    for page_no, raw in pages[body_start_idx:]:
        body = strip_page_boilerplate(raw)
        if not body:
            continue
        body = merge_wrapped_bracket_lines(body)
        lo = pos
        parts.append(body)
        pos += len(body) + len(sep)
        spans.append((lo, lo + len(body), page_no))
    text = sep.join(parts)
    return text, spans


def _page_for_offset(spans: list[tuple[int, int, int]], offset: int) -> int:
    for lo, hi, pno in spans:
        if lo <= offset < hi:
            return pno
    if spans:
        return spans[-1][2]
    return 1


def _last_topic_before(text: str, pos: int) -> str:
    """
    Return the most recent topic heading before ``pos``.

    Headings are printed as several consecutive block-capital lines; we join
    those lines so the stored topic is the full title (not only the longest
    single line, and not dropping short final lines like ``KAWASAN TUARAN``).
    """
    head = text[:pos]
    last_topic = ""
    buf: list[str] = []
    for ln in head.splitlines():
        ls = ln.strip()
        if is_topic_primary_line(ls) or (buf and is_topic_continuation_line(ls)):
            buf.append(ls)
            continue
        if buf:
            last_topic = clean(" ".join(buf))
            buf = []
    if buf:
        last_topic = clean(" ".join(buf))
    return last_topic


def _last_timestamp_before(text: str, pos: int) -> str:
    """Scan a short window before ``pos`` for a standalone time-of-day line."""
    win = text[max(0, pos - 120) : pos]
    for piece in reversed(win.splitlines()):
        ps = piece.strip()
        if not ps:
            continue
        tm = TIMESTAMP_LINE.match(ps)
        if tm:
            return ps
        # stop scanning if we hit non-trivial prose after leaving blank gap
        if len(ps) > 30:
            break
    return ""


def build_turns(
    merged: str, spans: list[tuple[int, int, int]], sitting_date: str
) -> tuple[list[Turn], dict]:
    """Parse merged body text into ordered speaker turns (cross-page safe)."""
    if not merged.strip():
        return [], {
            "sitting_date": sitting_date,
            "document": "kamar_khas",
            "series": "KKDR",
            "turn_count": 0,
        }

    matches = list(iter_speaker_matches(merged))
    turns: list[Turn] = []

    for i, m in enumerate(matches):
        t_start = m.end
        t_end = matches[i + 1].start if i + 1 < len(matches) else len(merged)
        speech = clean(merged[t_start:t_end])
        ts = _last_timestamp_before(merged, m.start)
        sm = TIMESTAMP_LINE.match(speech)
        if sm:
            ts = sm.group(0).strip()
            speech = clean(speech[sm.end :])

        topic = _last_topic_before(merged, m.start)
        page_no = _page_for_offset(spans, m.start)

        role = role_for_match(m)
        portfolio = m.portfolio_or_role if m.kind == "minister" else ""
        constituency = m.person_or_constituency if m.kind == "member" else ""

        if m.kind == "chair":
            person = m.person_or_constituency
            display = f"{m.portfolio_or_role} [{m.person_or_constituency}]"
        elif m.kind == "minister":
            person = m.person_or_constituency
            display = f"{m.portfolio_or_role} [{m.person_or_constituency}]"
        elif m.kind == "member":
            person = m.full_label.split("[")[0].strip()
            display = m.full_label.rstrip(":")
        else:
            person = m.person_or_constituency
            display = m.person_or_constituency

        turns.append(
            Turn(
                index=len(turns) + 1,
                page=page_no,
                topic=topic,
                timestamp=ts,
                role=role,
                speaker_display=clean(display),
                portfolio=clean(portfolio) if portfolio else "",
                constituency=clean(constituency) if constituency else "",
                person_name=clean(person) if person else "",
                speech=speech,
                raw_label=m.full_label,
                meta={"kind": m.kind, "sitting_date": sitting_date},
            )
        )

    meta = {
        "sitting_date": sitting_date,
        "document": "kamar_khas",
        "series": "KKDR",
        "turn_count": len(turns),
    }
    return turns, meta


def turns_to_jsonable(turns: list[Turn]) -> list[dict]:
    return [asdict(t) for t in turns]


def save_turns_csv(turns: list[Turn], path: str) -> None:
    fields = [
        "index",
        "page",
        "topic",
        "timestamp",
        "role",
        "speaker_display",
        "portfolio",
        "constituency",
        "person_name",
        "speech",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in turns:
            row = {k: getattr(t, k) for k in fields}
            if row.get("speech"):
                row["speech"] = re.sub(r"\s+", " ", row["speech"]).strip()
            w.writerow(row)


def save_plain_transcript(
    pages: list[tuple[int, str]],
    path: str,
    body_start: int,
    merged: str | None = None,
    spans: list[tuple[int, int, int]] | None = None,
) -> None:
    """Write body text; prefer ``merged`` + ``spans`` for page markers (parser view)."""
    with open(path, "w", encoding="utf-8") as f:
        if merged is not None and spans:
            for lo, hi, page_no in spans:
                chunk = merged[lo:hi]
                f.write(f"\n{'='*60}\nPAGE {page_no}\n{'='*60}\n\n")
                f.write(chunk)
                f.write("\n")
            return
        if merged is not None:
            f.write(merged)
            f.write("\n")
            return
        for page_no, raw in pages[body_start:]:
            f.write(f"\n{'='*60}\nPAGE {page_no}\n{'='*60}\n\n")
            f.write(merge_wrapped_bracket_lines(strip_page_boilerplate(raw)))
            f.write("\n")


def extract_kkdr_proceedings(pdf_path: str, output_dir: str) -> list[Turn]:
    os.makedirs(output_dir, exist_ok=True)
    stem = Path(pdf_path).stem

    pages = extract_pages(pdf_path)
    full_text = "\n".join(t for _, t in pages)
    sitting_date = detect_sitting_date(full_text)
    body_idx = first_body_page_index(pages)

    merged, spans = _merge_body_with_page_map(pages, body_idx)
    turns, meta = build_turns(merged, spans, sitting_date)

    json_path = os.path.join(output_dir, f"{stem}_proceedings.json")
    csv_path = os.path.join(output_dir, f"{stem}_turns.csv")
    txt_path = os.path.join(output_dir, f"{stem}_body.txt")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "turns": turns_to_jsonable(turns)}, f, ensure_ascii=False, indent=2)

    save_turns_csv(turns, csv_path)
    save_plain_transcript(pages, txt_path, body_idx, merged=merged, spans=spans)

    print(f"[KKDR] sitting_date={sitting_date}  body starts page={pages[body_idx][0] if pages else 0}")
    print(f"  JSON  → {json_path}")
    print(f"  CSV   → {csv_path}")
    print(f"  Body  → {txt_path}")
    print(f"  Turns: {len(turns)}")
    return turns


DEFAULT_PDF = DATA_PDFS / "KKDR-03032026-1.pdf"


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        pdf = sys.argv[1]
        out = sys.argv[2] if len(sys.argv) > 2 else str(OUTPUT_KKDR)
    else:
        pdf = str(DEFAULT_PDF)
        out = str(OUTPUT_KKDR)
        print(f"No input path — using default: {pdf}")

    if not os.path.isfile(pdf):
        print(f"Error: file not found: {pdf}", file=sys.stderr)
        print(
            "Usage: python scripts/kkdr_kamar_khas_extract.py <KKDR-….pdf> [output_dir]",
            file=sys.stderr,
        )
        sys.exit(1)

    extract_kkdr_proceedings(pdf, out)
