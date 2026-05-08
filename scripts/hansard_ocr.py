"""
Malaysian Hansard Extractor — Attendance & CSV Export
=====================================================
Supports two modes automatically:
  • Text-based PDF  → direct extraction via pdfplumber (fast, accurate)
  • Scanned PDF/image → PaddleOCR (new API, no deprecated params)

Outputs per document:
  <stem>_attendance.csv   — all members (hadir / tidak hadir / senator)
  <stem>_speakers.csv     — speaker turn log
  <stem>_plain.txt        — flat readable text
  <stem>_ocr.json         — full raw OCR data (OCR mode only)

Fixes applied (v2):
  1. Attendance page range is now detected dynamically instead of being
     hardcoded to 6 pages.  This ensures the "Tidak Hadir" and
     "Tidak Hadir (Peraturan 91)" sections are never truncated, regardless
     of how many pages the attendance preamble occupies.
  2. normalize_name_spacing() now also handles title-case column-split
     artefacts produced by pdfplumber on two-column layouts, e.g.
     "H enry" → "Henry", "W illiam" → "William", "L im" → "Lim".
     Known name particles (bin, binti, Haji, Sri, …) are preserved.

Install:
    pip install pdfplumber paddlepaddle paddleocr pdf2image Pillow
    sudo apt-get install poppler-utils
"""

import os
import re
import csv
import json
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DR = REPO_ROOT / "output" / "hansard"


# ─── Shared patterns ──────────────────────────────────────────────────────────

NUMBER_LINE     = re.compile(r'^\d+\.\s+(.+)$')
CONSTITUENCY    = re.compile(r'\(([^)]+)\)\s*$')
SENATOR_PREFIX  = re.compile(r'^Senator\s+', re.IGNORECASE)
MINISTER_SUFFIX = re.compile(r'\(\s*(?:Timbalan\s+)?Menteri[^)]*\)', re.IGNORECASE)

ROLE_PATTERN = re.compile(
    r'^(Perdana Menteri|Timbalan Perdana Menteri|Menteri\s+(?!Yang)|Timbalan Menteri\s+|'
    r'Timbalan Yang di-Pertua|Yang di-Pertua)',
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(
    r'(isnin|selasa|rabu|khamis|jumaat|sabtu|ahad|'
    r'monday|tuesday|wednesday|thursday|friday|saturday|sunday)'
    r'[,\s]+(\d{1,2})\s+'
    r'(januari|februari|mac|april|mei|jun|julai|ogos|september|oktober|november|disember|'
    r'january|february|march|april|may|june|july|august|september|october|november|december)'
    r'\s+(\d{4})',
    re.IGNORECASE,
)
MONTH_MAP = {
    'januari':'01','january':'01','februari':'02','february':'02',
    'mac':'03','march':'03','april':'04','mei':'05','may':'05',
    'jun':'06','june':'06','julai':'07','july':'07','ogos':'08','august':'08',
    'september':'09','oktober':'10','october':'10','november':'11',
    'disember':'12','december':'12',
}
SPEAKER_RE   = re.compile(r'^(tuan yang di-pertua|yb|yang berhormat|dato|datuk|dr\.?\s+\w+)', re.IGNORECASE)
TIMESTAMP_RE = re.compile(r'\b(\d{1,2}[.:]\d{2}\s*(pagi|petang|am|pm)?)\b', re.IGNORECASE)
MOTION_RE    = re.compile(r'(rang undang-undang|usul|motion|akta|bill)', re.IGNORECASE)
QUESTION_RE  = re.compile(r'\b(soalan\s*(lisan|bertulis)?\s*no\.?\s*\d+|oral\s+question\s*\d+)', re.IGNORECASE)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def clean(s):
    return re.sub(r'\s+', ' ', s).strip()

def strip_minister_suffix(s):
    """Remove inline ministry suffixes from names, e.g. '(Menteri ...)'."""
    return clean(MINISTER_SUFFIX.sub('', s))

def extract_name_only(s):
    """
    Keep only person name for lines formatted as 'role, person name'.
    Example: 'Menteri ..., Dato' Seri X' -> 'Dato' Seri X'
    """
    if "," in s:
        return clean(s.rsplit(",", 1)[1])
    return s

def normalize_name_spacing(name):
    """
    Fix OCR spacing splits inside a single name token that arise when pdfplumber
    misreads a two-column layout and inserts a space inside a word.

    Handles two classes of artefact:
      1. Lowercase continuation  – 'F ong' → 'Fong', 'Is naraissah' → 'Isnaraissah'
      2. Title-case continuation – 'H enry' → 'Henry', 'W illiam' → 'William',
                                   'L im' → 'Lim', 'N geh' → 'Ngeh'
         (single uppercase letter followed by a space then an uppercase-led word
          that is NOT a standalone name particle such as bin/binti/Haji etc.)

    Normal multi-token names ('bin Haji', 'Dato Sri', 'a/l') are preserved because
    their leading fragments are longer than one character or are known particles.
    """
    fixed = clean(name)

    # Known standalone particles that must never be glued to the previous token.
    PARTICLES = re.compile(
        r'^(bin|binti|bte|bt|a/l|a/p|haji|hajah|dato|datuk|datu|tan|sri|seri|'
        r'tuan|puan|dr|ir|kapten|komander|senator)$',
        re.IGNORECASE,
    )

    prev = None
    while prev != fixed:
        prev = fixed
        # Rule 1 (existing): short fragment + lowercase continuation
        fixed = re.sub(r'\b([A-Za-z]{1,2})\s+([a-z]{2,})\b', r'\1\2', fixed)
        # Rule 2 (new): single uppercase letter + space + Title-case word
        # Only join when the second word is NOT a known standalone particle.
        def _join_title(m):
            letter, word = m.group(1), m.group(2)
            if PARTICLES.match(word):
                return m.group(0)   # keep as-is
            return letter + word
        fixed = re.sub(r'\b([A-Z])\s+([A-Z][a-z]{1,})\b', _join_title, fixed)

    return fixed

def extract_constituency(s):
    m = CONSTITUENCY.search(s)
    return (m.group(1).strip(), CONSTITUENCY.sub('', s).strip()) if m else ("", s)

def extract_role(s):
    s = clean(s)
    if "," in s:
        role_candidate = clean(s.rsplit(",", 1)[0])
        if ROLE_PATTERN.match(role_candidate):
            return role_candidate
    m = ROLE_PATTERN.match(s)
    return m.group(0).strip() if m else ""

def detect_sitting_date(text):
    m = DATE_PATTERN.search(text[:2000])
    if m:
        day   = m.group(2).zfill(2)
        month = MONTH_MAP.get(m.group(3).lower(), '00')
        year  = m.group(4)
        return f"{year}-{month}-{day}"
    return datetime.today().strftime("%Y-%m-%d")

def parse_numbered_section(lines, status, senator=False):
    """Parse a numbered attendance list into structured rows."""
    rows, buf = [], ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = NUMBER_LINE.match(line)
        if m:
            if buf:
                rows.append(buf)
            buf = m.group(1)
        elif buf:
            buf += " " + line
    if buf:
        rows.append(buf)

    result = []
    for i, raw in enumerate(rows, 1):
        raw = clean(SENATOR_PREFIX.sub('', raw))
        raw = strip_minister_suffix(raw)
        role = extract_role(raw)
        constituency, name = extract_constituency(raw)
        name = extract_name_only(name)
        name = normalize_name_spacing(name)
        result.append({
            "sitting_date": "",          # filled in by caller
            "no":           i,
            "name":         name,
            "role":         role,
            "constituency": constituency,
            "category":     "Senator" if senator else "Ahli Parlimen",
            "status":       status,
        })
    return result

def split_attendance_sections(lines):
    """Split raw lines into hadir / senator / tidak_hadir / pm91 buckets."""
    hadir, senator, tidak_hadir, pm91 = [], [], [], []
    current = None

    for line in lines:
        ls = line.strip()
        if re.search(r'Ahli-Ahli Yang Tidak Hadir Di Bawah Peraturan', ls, re.I):
            current = "pm91"; continue
        if re.search(r'Ahli-Ahli Yang Tidak Hadir', ls, re.I):
            current = "tidak_hadir"; continue
        if re.search(r'Senator Yang Turut Hadir', ls, re.I):
            current = "senator"; continue
        if re.search(r'Ahli-Ahli Yang Hadir|^Ahli Yang Hadir', ls, re.I):
            current = "hadir"; continue
        if re.search(r'KEHADIRAN AHLI-AHLI|^DR\.\s+\d+\.\d+', ls):
            continue

        if   current == "hadir":       hadir.append(line)
        elif current == "senator":     senator.append(line)
        elif current == "tidak_hadir": tidak_hadir.append(line)
        elif current == "pm91":        pm91.append(line)

    return hadir, senator, tidak_hadir, pm91

def parse_body_blocks(blocks):
    """Categorise OCR text blocks into speaker/timestamp/motion/question/body."""
    result = {"speaker":[], "timestamp":[], "motion":[], "question_number":[], "body_text":[]}
    for block in blocks:
        t = block["text"]
        if SPEAKER_RE.match(t):        result["speaker"].append(t)
        elif TIMESTAMP_RE.search(t):   result["timestamp"].append(t)
        elif MOTION_RE.search(t):      result["motion"].append(t)
        elif QUESTION_RE.search(t):    result["question_number"].append(t)
        else:                          result["body_text"].append(t)
    return result


# ─── CSV writers ──────────────────────────────────────────────────────────────

def save_attendance_csv(attendance, output_path):
    fields = ["sitting_date","no","name","role","constituency","category","status"]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(attendance)
    hadir   = sum(1 for r in attendance if r["status"] == "Hadir")
    absent  = sum(1 for r in attendance if "Tidak Hadir" in r["status"])
    print(f"  Attendance CSV → {output_path}")
    print(f"    Hadir: {hadir}  |  Tidak Hadir: {absent}  |  Jumlah: {len(attendance)}")

def save_speakers_csv(pages, output_path):
    rows = [{"page": p["page"], "speaker": s}
            for p in pages for s in p["parsed"]["speaker"]]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["page","speaker"])
        w.writeheader(); w.writerows(rows)
    print(f"  Speakers CSV   → {output_path}  ({len(rows)} turns)")

def save_plain_text(pages, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        for page in pages:
            f.write(f"\n{'='*60}\n  PAGE {page['page']}\n{'='*60}\n\n")
            for block in page["raw_blocks"]:
                f.write(block["text"] + "\n")
    print(f"  Plain text     → {output_path}")


# ─── Mode A: Text-based PDF (pdfplumber) ──────────────────────────────────────

def find_attendance_page_range(pdf, max_scan=20):
    """
    Dynamically detect which pages contain the attendance section.

    Strategy:
      - Start: first page containing an attendance header keyword.
      - End  : first subsequent page whose header matches the Arabic-numbered
               body format  "DR. DD.MM.YYYY  <arabic digit(s)>"
               (roman-numeral pages like "DR. 26.2.2026 i" are still attendance).

    Falls back to pages 0–9 if detection fails, which is generous enough for
    any realistic Malaysian Hansard layout.
    """
    # Matches the Hansard body header, e.g. "DR. 26.2.2026 1"
    BODY_HEADER = re.compile(r'DR\.\s+\d+\.\d+\.\d{4}\s+\d+', re.IGNORECASE)
    ATT_HEADER  = re.compile(
        r'(KEHADIRAN AHLI|Ahli-Ahli Yang Hadir|Ahli Yang Hadir)', re.IGNORECASE
    )

    n = len(pdf.pages)
    att_start = None
    att_end   = None

    for i in range(min(max_scan, n)):
        text = pdf.pages[i].extract_text() or ""
        if att_start is None and ATT_HEADER.search(text):
            att_start = i
        elif att_start is not None and BODY_HEADER.search(text):
            att_end = i
            break

    if att_start is None:
        att_start = 0
    if att_end is None:
        att_end = min(att_start + 10, n)   # generous fallback

    return att_start, att_end


def extract_text_pdf(pdf_path, output_dir):
    import pdfplumber

    stem = Path(pdf_path).stem
    print(f"[Mode] Text-based PDF detected — using pdfplumber")

    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        print(f"[1/3] Reading {n_pages} pages...")
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        # Dynamically locate the attendance section instead of assuming 6 pages
        att_start, att_end = find_attendance_page_range(pdf)
        print(f"      Attendance section detected on pages {att_start + 1}–{att_end} "
              f"(of {n_pages})")
        att_text  = "\n".join(pdf.pages[i].extract_text() or ""
                              for i in range(att_start, att_end))

    sitting_date = detect_sitting_date(full_text)
    print(f"[2/3] Sitting date: {sitting_date}")

    print(f"[3/3] Parsing attendance...")
    lines = att_text.split("\n")
    hadir_lines, senator_lines, tidak_hadir_lines, pm91_lines = split_attendance_sections(lines)

    all_rows = (
        parse_numbered_section(hadir_lines,       "Hadir") +
        parse_numbered_section(senator_lines,     "Hadir", senator=True) +
        parse_numbered_section(tidak_hadir_lines, "Tidak Hadir") +
        parse_numbered_section(pm91_lines,        "Tidak Hadir (Peraturan 91)")
    )
    for row in all_rows:
        row["sitting_date"] = sitting_date

    att_csv = os.path.join(output_dir, f"{stem}_attendance.csv")
    txt_out = os.path.join(output_dir, f"{stem}_plain.txt")

    save_attendance_csv(all_rows, att_csv)

    # Simple plain text dump
    with open(txt_out, "w", encoding="utf-8") as f:
        f.write(full_text)
    print(f"  Plain text     → {txt_out}")

    return all_rows


# ─── Mode B: Scanned PDF / image (PaddleOCR) ──────────────────────────────────

def get_ocr_engine():
    """
    Initialise PaddleOCR compatible with old and new API versions.

    New API (paddleocr >= 3.x):
        - use_textline_orientation replaces use_angle_cls
        - show_log removed; ocr() deprecated in favour of predict()
    Legacy API (paddleocr < 2.8):
        - use_angle_cls=True, show_log=False
    """
    from paddleocr import PaddleOCR
    import inspect
    sig = inspect.signature(PaddleOCR.__init__).parameters
    if "use_textline_orientation" in sig:
        return PaddleOCR(use_textline_orientation=True, lang='en')
    else:
        return PaddleOCR(use_angle_cls=True, lang='en', show_log=False)

def pdf_to_images(pdf_path, dpi=200, output_dir="/tmp/hansard_pages"):
    from pdf2image import convert_from_path
    os.makedirs(output_dir, exist_ok=True)
    pages = convert_from_path(pdf_path, dpi=dpi)
    paths = []
    for i, page in enumerate(pages, 1):
        p = os.path.join(output_dir, f"page_{i:04d}.jpg")
        page.save(p, "JPEG")
        paths.append(p)
    return paths

def ocr_image(ocr_engine, image_path):
    """
    Compatible with both old API (ocr + cls=True) and new API (predict).
    New PaddleOCR >= 2.8 uses predict() and returns a list of Result objects.
    """
    try:
        # New API: predict() returns a list of result objects
        results = ocr_engine.predict(image_path)
        blocks = []
        for res in results:
            # Each res has .boxes, .texts, .scores attributes
            for box, text, score in zip(res.boxes, res.texts, res.scores):
                blocks.append({
                    "text": text.strip(),
                    "conf": round(float(score), 4),
                    "bbox": box,
                })
        return blocks
    except AttributeError:
        # Legacy API fallback
        result = ocr_engine.ocr(image_path)
        blocks = []
        if result and result[0]:
            for line in result[0]:
                bbox, (text, conf) = line
                blocks.append({"text": text.strip(), "conf": round(conf, 4), "bbox": bbox})
        return blocks

def extract_scanned_pdf(input_path, output_dir, dpi=200):
    stem = Path(input_path).stem
    ext  = Path(input_path).suffix.lower()

    print(f"[Mode] Scanned / image — using PaddleOCR")
    ocr = get_ocr_engine()

    if ext == ".pdf":
        print("[1/5] Rasterising PDF pages...")
        image_paths = pdf_to_images(input_path, dpi=dpi)
    else:
        image_paths = [input_path]

    print(f"[2/5] Running OCR on {len(image_paths)} page(s)...")
    all_blocks, all_pages = [], []
    for i, img_path in enumerate(image_paths, 1):
        print(f"      Page {i}/{len(image_paths)}")
        blocks = ocr_image(ocr, img_path)
        parsed = parse_body_blocks(blocks)
        all_blocks.extend(blocks)
        all_pages.append({"page": i, "raw_blocks": blocks, "parsed": parsed})

    full_text = "\n".join(b["text"] for b in all_blocks)
    print("[3/5] Detecting sitting date...")
    sitting_date = detect_sitting_date(full_text)
    print(f"      Sitting date: {sitting_date}")

    print("[4/5] Extracting attendance from OCR text...")
    # Limit attendance parsing to the first pages that are likely to contain it
    # (avoids false-positive matches of section headers in the Hansard body).
    att_page_limit = min(len(all_pages), 12)
    att_pages_text = "\n".join(
        "\n".join(b["text"] for b in all_pages[i]["raw_blocks"])
        for i in range(att_page_limit)
    )
    lines = att_pages_text.split("\n")
    hadir_lines, senator_lines, tidak_hadir_lines, pm91_lines = split_attendance_sections(lines)
    all_rows = (
        parse_numbered_section(hadir_lines,       "Hadir") +
        parse_numbered_section(senator_lines,     "Hadir", senator=True) +
        parse_numbered_section(tidak_hadir_lines, "Tidak Hadir") +
        parse_numbered_section(pm91_lines,        "Tidak Hadir (Peraturan 91)")
    )
    for row in all_rows:
        row["sitting_date"] = sitting_date

    print("[5/5] Saving outputs...")
    att_csv  = os.path.join(output_dir, f"{stem}_attendance.csv")
    spk_csv  = os.path.join(output_dir, f"{stem}_speakers.csv")
    txt_out  = os.path.join(output_dir, f"{stem}_plain.txt")
    json_out = os.path.join(output_dir, f"{stem}_ocr.json")

    save_attendance_csv(all_rows, att_csv)
    save_speakers_csv(all_pages, spk_csv)
    save_plain_text(all_pages, txt_out)
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump({"sitting_date": sitting_date, "pages": all_pages}, f,
                  ensure_ascii=False, indent=2)
    print(f"  OCR JSON       → {json_out}")

    return all_rows


# ─── Auto-detect mode ─────────────────────────────────────────────────────────

def is_text_pdf(pdf_path, sample_pages=3):
    """Return True if the PDF has extractable text (not a scan)."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for i in range(min(sample_pages, len(pdf.pages))):
                if pdf.pages[i].extract_text():
                    return True
    except Exception:
        pass
    return False


# ─── Main entry ───────────────────────────────────────────────────────────────

def extract_hansard(input_path, output_dir=None, dpi=200):
    """
    Auto-detect text vs scanned PDF and run the appropriate pipeline.

    Args:
        input_path : Path to a .pdf or image file
        output_dir : Folder to save all output files (default: ``output/hansard`` under repo root)
        dpi        : Rasterisation DPI for scanned PDFs (200 recommended)
    """
    if output_dir is None:
        output_dir = str(OUTPUT_DR)
    os.makedirs(output_dir, exist_ok=True)
    ext = Path(input_path).suffix.lower()

    if ext == ".pdf" and is_text_pdf(input_path):
        rows = extract_text_pdf(input_path, output_dir)
    else:
        rows = extract_scanned_pdf(input_path, output_dir, dpi=dpi)

    print("\n── Done ─────────────────────────────────────────────")
    return rows


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scripts/hansard_ocr.py <path/to/hansard.pdf> [output_dir]")
        sys.exit(1)
    extract_hansard(
        sys.argv[1],
        sys.argv[2] if len(sys.argv) > 2 else str(OUTPUT_DR),
    )
