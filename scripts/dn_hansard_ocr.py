"""
Dewan Negara Hansard Extractor (DN-12032026 and same-series PDFs)
==================================================================
Template: hansard_ocr.py — adapted for Malaysian Senate (Dewan Negara)
official Hansard layout.

Differences from Dewan Rakyat (DR) handled here:
  • Body page header: ``DN 12.3.2026 1`` (not ``DR.``); attendance preamble
    uses roman page tails (``i``, ``ii``, …) before Arabic body pages.
  • Attendance blocks: ``AHLI HADIR`` / ``AHLI HADIR (samb/-)``, ``TIDAK HADIR``,
    ``TIDAK HADIR (DI BAWAH PERATURAN MESYUARAT 83)``, ``HADIR BERSAMA``.
  • Roman-numeral staff page (e.g. ``DN … iv`` with Pentadbir/Petugas list) is
    skipped so it does not pollute attendance parsing.
  • Numbered rows may omit the dot after the index (e.g. ``32 Yang Berhormat``).

Outputs (stem = PDF filename without extension):
  <stem>_attendance.csv — columns include ``job_title`` when a portfolio / chair
    line was split from the member ``name``; ``plain.txt`` / OCR outputs unchanged.
  (+ <stem>_speakers.csv, <stem>_ocr.json in scanned mode, same as hansard_ocr)

Install: same as hansard_ocr.py (pdfplumber, paddle*, pdf2image, …).
"""

import os
import re
import csv
import json
import sys
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PDFS = REPO_ROOT / "data" / "pdfs"
OUTPUT_DN = REPO_ROOT / "output" / "dn_hansard"


# ─── Patterns (shared + DN-specific) ─────────────────────────────────────────

CONSTITUENCY = re.compile(r"\(([^)]+)\)\s*$")
SENATOR_PREFIX = re.compile(r"^Senator\s+", re.IGNORECASE)
MINISTER_SUFFIX = re.compile(
    r"\(\s*(?:Timbalan\s+)?Menteri[^)]*\)", re.IGNORECASE
)

# Text after a comma that we treat as a government job / portfolio (not a person name).
_JOB_AFTER_COMMA = re.compile(
    r"^(?:Menteri\b|Timbalan\s+Menteri\b|Perdana\s+Menteri\b|"
    r"Timbalan\s+Perdana\s+Menteri\b|Menteri\s+)",
    re.IGNORECASE,
)

# "Yang di-Pertua Dewan Negara, Dato' …" style — job is the left segment before the comma.
_CHAIR_THEN_NAME = re.compile(
    r"^(?P<job>(?:Yang\s+Berhormat\s+)?"
    r"(?:Timbalan\s+)?Yang\s+di-Pertua\s+Dewan\s+Negara)\s*,\s*(?P<name>.+)$",
    re.IGNORECASE,
)

# Trailing ministry in square brackets (common in Hadir Bersama lines).
_BRACKET_JOB = re.compile(r"^(?P<name>.+?)\s*\[\s*(?P<job>[^\]]+)\s*\]\s*$", re.DOTALL)
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
SPEAKER_RE = re.compile(
    r"^(tuan yang di-pertua|yb|yang berhormat|dato|datuk|dr\.?\s+\w+)",
    re.IGNORECASE,
)
TIMESTAMP_RE = re.compile(
    r"\b(\d{1,2}[.:]\d{2}\s*(pagi|petang|am|pm)?)\b", re.IGNORECASE
)
MOTION_RE = re.compile(
    r"(rang undang-undang|usul|motion|akta|bill)", re.IGNORECASE
)
QUESTION_RE = re.compile(
    r"\b(soalan\s*(lisan|bertulis)?\s*no\.?\s*\d+|oral\s+question\s*\d+)",
    re.IGNORECASE,
)

# First line of a DN proceedings page: ``DN 12.3.2026 1`` (Arabic only — not ``i``/``iv``).
DN_FIRST_LINE_BODY = re.compile(
    r"^DN\s+\d{1,2}\.\d{1,2}\.\d{4}\s+(\d+)\s*$", re.IGNORECASE
)


def clean(s):
    return re.sub(r"\s+", " ", s).strip()


def strip_minister_suffix(s):
    return clean(MINISTER_SUFFIX.sub("", s))


def normalize_name_spacing(name):
    fixed = clean(name)
    PARTICLES = re.compile(
        r"^(bin|binti|bte|bt|a/l|a/p|haji|hajah|dato|datuk|datu|tan|sri|seri|"
        r"tuan|puan|dr|ir|kapten|komander|senator)$",
        re.IGNORECASE,
    )
    prev = None
    while prev != fixed:
        prev = fixed
        fixed = re.sub(r"\b([A-Za-z]{1,2})\s+([a-z]{2,})\b", r"\1\2", fixed)

        def _join_title(m):
            letter, word = m.group(1), m.group(2)
            if PARTICLES.match(word):
                return m.group(0)
            return letter + word

        fixed = re.sub(r"\b([A-Z])\s+([A-Z][a-z]{1,})\b", _join_title, fixed)
    return fixed


def extract_constituency(s):
    m = CONSTITUENCY.search(s)
    return (m.group(1).strip(), CONSTITUENCY.sub("", s).strip()) if m else ("", s)


# Only treat trailing (…) as Dewan Negara “negeri / WP” when it matches a real state label,
# so ministry clauses like “(Hal Ehwal Agama)” are not split off as constituency.
_STATE_LABELS = re.compile(
    r"^(?:"
    r"Johor|Kedah|Kelantan|Melaka|Negeri\s+Sembilan|Pahang|Perak|Perlis|"
    r"Pulau\s+Pinang|Penang|Sabah|Sarawak|Selangor|Terengganu|"
    r"Wilayah\s+Persekutuan\s+(?:Kuala\s+Lumpur|Labuan|Putrajaya)|"
    r"Kuala\s+Selangor|Ledang|Batu\s+Sapi|Tawau|Miri|Alor\s+Gajah|Kota\s+Kinabalu|Tanjong"
    r")$",
    re.IGNORECASE,
)


def extract_constituency_dn(s):
    m = CONSTITUENCY.search(s)
    if not m:
        return "", s
    inner = m.group(1).strip()
    if _STATE_LABELS.match(inner):
        return inner, CONSTITUENCY.sub("", s).strip()
    return "", s


def _strip_address_prefix(name):
    """Remove leading ``Yang Berhormat`` from the person field once jawatan is split out."""
    return re.sub(r"^Yang\s+Berhormat\s+", "", name, flags=re.IGNORECASE).strip()


def split_name_and_job_title(s):
    """
    Split a single attendance display string into (person_name, job_title).

    Handles:
      • ``… [Timbalan Menteri …]`` (Hadir Bersama)
      • ``Yang di-Pertua Dewan Negara, Dato' …``
      • ``… , Menteri …`` / ``… , Timbalan Menteri …`` (comma before portfolio)
    """
    s = clean(s)
    s = re.sub(r'^[\s"“”\'’]+', "", s)
    if not s:
        return "", ""

    if (m := _BRACKET_JOB.match(s)):
        nm, jt = m.group("name").strip(), clean(m.group("job"))
        nm = normalize_name_spacing(_strip_address_prefix(nm))
        return nm, jt

    if (m := _CHAIR_THEN_NAME.match(s)):
        jt = clean(m.group("job"))
        nm = normalize_name_spacing(m.group("name").strip())
        return nm, jt

    if "," in s:
        left, right = s.rsplit(",", 1)
        left, right = left.strip(), right.strip()
        if _JOB_AFTER_COMMA.match(right):
            nm = normalize_name_spacing(_strip_address_prefix(left))
            return nm, clean(right)

    return normalize_name_spacing(_strip_address_prefix(s)), ""


def detect_sitting_date(text):
    m = DATE_PATTERN.search(text[:4000])
    if m:
        day = m.group(2).zfill(2)
        month = MONTH_MAP.get(m.group(3).lower(), "00")
        year = m.group(4)
        return f"{year}-{month}-{day}"
    return datetime.today().strftime("%Y-%m-%d")


def dn_first_nonblank_line(text):
    for ln in (text or "").split("\n"):
        ls = ln.strip()
        if ls:
            return ls
    return ""


def is_dn_staff_page(text):
    t = text or ""
    if "PETUGAS-PETUGAS CAWANGAN PENYATA RASMI" in t:
        return True
    if "Ketua Pentadbir Parlimen" in t and "AHLI HADIR" not in t and "HADIR BERSAMA" not in t:
        return True
    return False


def dn_match_numbered_line(line):
    """
    Dewan Negara attendance lines use ``N. `` or occasionally ``N `` without dot.
    Reject ``10.04 pg``-style fragments.
    """
    line = line.strip()
    m = re.match(r"^(\d+)\.\s*(.+)$", line)
    if m:
        return m.group(2)
    m = re.match(r"^(\d+)\s+(.+)$", line)
    if not m:
        return None
    rest = m.group(2).lstrip("“”\"'‘’ \t")
    if re.match(r"^\d+\.\d+", rest):
        return None
    return rest


def parse_numbered_section_dn(lines, status, category):
    """Parse DN numbered attendance / hadir bersama rows."""
    rows, buf = [], ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        cont = dn_match_numbered_line(line)
        if cont is not None:
            if buf:
                rows.append(buf)
            buf = cont
        elif buf:
            buf += " " + line
    if buf:
        rows.append(buf)

    result = []
    for i, raw in enumerate(rows, 1):
        raw = clean(SENATOR_PREFIX.sub("", raw))
        raw = strip_minister_suffix(raw)
        constituency, display = extract_constituency_dn(raw)
        name, job_title = split_name_and_job_title(display)
        result.append(
            {
                "sitting_date": "",
                "no": i,
                "name": name,
                "job_title": job_title,
                "constituency": constituency,
                "category": category,
                "status": status,
            }
        )
    return result


def split_dn_attendance_sections(lines):
    """
    DN-specific section headers (reference: DN-12032026.pdf).
    Order matters: match Peraturan 83 block before plain TIDAK HADIR.
    """
    hadir, tidak_hadir, pm83, hadir_bersama = [], [], [], []
    current = None

    for line in lines:
        ls = line.strip()
        if re.search(r"TIDAK\s+HADIR\s*\(?\s*DI\s+BAWAH\s+PERATURAN\s+MESYUARAT\s+83", ls, re.I):
            current = "pm83"
            continue
        if re.search(r"^TIDAK\s+HADIR\s*$", ls, re.I) or (
            re.search(r"TIDAK\s+HADIR\b", ls, re.I)
            and "PERATURAN" not in ls.upper()
            and "83" not in ls
        ):
            current = "tidak_hadir"
            continue
        if re.search(r"HADIR\s+BERSAMA", ls, re.I):
            current = "hadir_bersama"
            continue
        if re.search(r"AHLI\s+HADIR", ls, re.I):
            current = "hadir"
            continue
        if re.search(r"KEHADIRAN|DN\s+\d+\.\d+\.\d{4}\s+[ivxlcdm]+\s*$", ls, re.I):
            continue

        if current == "hadir":
            hadir.append(line)
        elif current == "tidak_hadir":
            tidak_hadir.append(line)
        elif current == "pm83":
            pm83.append(line)
        elif current == "hadir_bersama":
            hadir_bersama.append(line)

    return hadir, tidak_hadir, pm83, hadir_bersama


def collect_dn_attendance_text(pdf, max_scan=15):
    """
    Concatenate text from DN attendance-related pages, skipping the staff
    roster page before Arabic body numbering begins.
    """
    import pdfplumber

    n = len(pdf.pages)
    att_start = None
    for i in range(min(max_scan, n)):
        t = pdf.pages[i].extract_text() or ""
        if re.search(r"AHLI\s+HADIR", t, re.I):
            att_start = i
            break
    if att_start is None:
        att_start = 0

    chunks = []
    for i in range(att_start, min(max_scan, n)):
        t = pdf.pages[i].extract_text() or ""
        if is_dn_staff_page(t):
            continue
        fl = dn_first_nonblank_line(t)
        if DN_FIRST_LINE_BODY.match(fl):
            break
        chunks.append(t)
    return "\n".join(chunks)


def parse_body_blocks(blocks):
    result = {
        "speaker": [],
        "timestamp": [],
        "motion": [],
        "question_number": [],
        "body_text": [],
    }
    for block in blocks:
        t = block["text"]
        if SPEAKER_RE.match(t):
            result["speaker"].append(t)
        elif TIMESTAMP_RE.search(t):
            result["timestamp"].append(t)
        elif MOTION_RE.search(t):
            result["motion"].append(t)
        elif QUESTION_RE.search(t):
            result["question_number"].append(t)
        else:
            result["body_text"].append(t)
    return result


def save_attendance_csv(attendance, output_path):
    fields = [
        "sitting_date",
        "no",
        "name",
        "job_title",
        "constituency",
        "category",
        "status",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(attendance)
    hadir = sum(1 for r in attendance if r["status"] == "Hadir")
    absent = sum(1 for r in attendance if "Tidak Hadir" in r["status"])
    hb = sum(1 for r in attendance if r["status"] == "Hadir Bersama")
    print(f"  Attendance CSV → {output_path}")
    print(
        f"    Hadir: {hadir}  |  Tidak Hadir: {absent}  |  Hadir Bersama: {hb}  |  Jumlah: {len(attendance)}"
    )


def save_speakers_csv(pages, output_path):
    rows = [{"page": p["page"], "speaker": s} for p in pages for s in p["parsed"]["speaker"]]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["page", "speaker"])
        w.writeheader()
        w.writerows(rows)
    print(f"  Speakers CSV   → {output_path}  ({len(rows)} turns)")


def save_plain_text(pages, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        for page in pages:
            f.write(f"\n{'='*60}\n  PAGE {page['page']}\n{'='*60}\n\n")
            for block in page["raw_blocks"]:
                f.write(block["text"] + "\n")
    print(f"  Plain text     → {output_path}")


def extract_text_pdf_dn(pdf_path, output_dir):
    import pdfplumber

    stem = Path(pdf_path).stem
    print("[Mode] Text-based PDF — pdfplumber (Dewan Negara)")

    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        print(f"[1/3] Reading {n_pages} pages...")
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        att_text = collect_dn_attendance_text(pdf)

    sitting_date = detect_sitting_date(full_text)
    print(f"[2/3] Sitting date: {sitting_date}")

    print("[3/3] Parsing attendance (DN sections)...")
    lines = att_text.split("\n")
    hadir_l, tidak_l, pm83_l, hb_l = split_dn_attendance_sections(lines)

    cat_member = "Ahli Dewan Negara"
    cat_bersama = "Ahli Dewan Rakyat (Hadir Bersama)"

    all_rows = (
        parse_numbered_section_dn(hadir_l, "Hadir", cat_member)
        + parse_numbered_section_dn(tidak_l, "Tidak Hadir", cat_member)
        + parse_numbered_section_dn(pm83_l, "Tidak Hadir (Peraturan Mesyuarat 83)", cat_member)
        + parse_numbered_section_dn(hb_l, "Hadir Bersama", cat_bersama)
    )
    for row in all_rows:
        row["sitting_date"] = sitting_date

    att_csv = os.path.join(output_dir, f"{stem}_attendance.csv")
    txt_out = os.path.join(output_dir, f"{stem}_plain.txt")
    save_attendance_csv(all_rows, att_csv)
    with open(txt_out, "w", encoding="utf-8") as f:
        f.write(full_text)
    print(f"  Plain text     → {txt_out}")
    return all_rows


def get_ocr_engine():
    from paddleocr import PaddleOCR
    import inspect

    sig = inspect.signature(PaddleOCR.__init__).parameters
    if "use_textline_orientation" in sig:
        return PaddleOCR(use_textline_orientation=True, lang="en")
    return PaddleOCR(use_angle_cls=True, lang="en", show_log=False)


def pdf_to_images(pdf_path, dpi=200, output_dir="/tmp/dn_hansard_pages"):
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
    try:
        results = ocr_engine.predict(image_path)
        blocks = []
        for res in results:
            for box, text, score in zip(res.boxes, res.texts, res.scores):
                blocks.append(
                    {
                        "text": text.strip(),
                        "conf": round(float(score), 4),
                        "bbox": box,
                    }
                )
        return blocks
    except AttributeError:
        result = ocr_engine.ocr(image_path)
        blocks = []
        if result and result[0]:
            for line in result[0]:
                bbox, (text, conf) = line
                blocks.append({"text": text.strip(), "conf": round(conf, 4), "bbox": bbox})
        return blocks


def extract_scanned_pdf_dn(input_path, output_dir, dpi=200):
    stem = Path(input_path).stem
    ext = Path(input_path).suffix.lower()

    print("[Mode] Scanned / image — PaddleOCR (Dewan Negara)")
    ocr = get_ocr_engine()

    if ext == ".pdf":
        print("[1/5] Rasterising PDF pages...")
        image_paths = pdf_to_images(input_path, dpi=dpi)
    else:
        image_paths = [input_path]

    print(f"[2/5] Running OCR on {len(image_paths)} page(s)...")
    all_pages = []
    for i, img_path in enumerate(image_paths, 1):
        print(f"      Page {i}/{len(image_paths)}")
        blocks = ocr_image(ocr, img_path)
        parsed = parse_body_blocks(blocks)
        all_pages.append({"page": i, "raw_blocks": blocks, "parsed": parsed})

    full_text = "\n".join(b["text"] for b in chain_blocks(all_pages))
    print("[3/5] Detecting sitting date...")
    sitting_date = detect_sitting_date(full_text)
    print(f"      Sitting date: {sitting_date}")

    print("[4/5] Extracting attendance from OCR text (DN rules)...")
    att_limit = min(len(all_pages), 14)
    att_text = "\n".join(
        "\n".join(b["text"] for b in all_pages[i]["raw_blocks"]) for i in range(att_limit)
    )
    lines = att_text.split("\n")
    hadir_l, tidak_l, pm83_l, hb_l = split_dn_attendance_sections(lines)
    cat_member = "Ahli Dewan Negara"
    cat_bersama = "Ahli Dewan Rakyat (Hadir Bersama)"
    all_rows = (
        parse_numbered_section_dn(hadir_l, "Hadir", cat_member)
        + parse_numbered_section_dn(tidak_l, "Tidak Hadir", cat_member)
        + parse_numbered_section_dn(pm83_l, "Tidak Hadir (Peraturan Mesyuarat 83)", cat_member)
        + parse_numbered_section_dn(hb_l, "Hadir Bersama", cat_bersama)
    )
    for row in all_rows:
        row["sitting_date"] = sitting_date

    print("[5/5] Saving outputs...")
    att_csv = os.path.join(output_dir, f"{stem}_attendance.csv")
    spk_csv = os.path.join(output_dir, f"{stem}_speakers.csv")
    txt_out = os.path.join(output_dir, f"{stem}_plain.txt")
    json_out = os.path.join(output_dir, f"{stem}_ocr.json")
    save_attendance_csv(all_rows, att_csv)
    save_speakers_csv(all_pages, spk_csv)
    save_plain_text(all_pages, txt_out)
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump({"sitting_date": sitting_date, "pages": all_pages}, f, ensure_ascii=False, indent=2)
    print(f"  OCR JSON       → {json_out}")
    return all_rows


def chain_blocks(all_pages):
    for p in all_pages:
        for b in p["raw_blocks"]:
            yield b


def is_text_pdf(pdf_path, sample_pages=3):
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            for i in range(min(sample_pages, len(pdf.pages))):
                if pdf.pages[i].extract_text():
                    return True
    except Exception:
        pass
    return False


def extract_dn_hansard(input_path, output_dir=None, dpi=200):
    if output_dir is None:
        output_dir = str(OUTPUT_DN)
    os.makedirs(output_dir, exist_ok=True)
    ext = Path(input_path).suffix.lower()

    if ext == ".pdf" and is_text_pdf(input_path):
        rows = extract_text_pdf_dn(input_path, output_dir)
    else:
        rows = extract_scanned_pdf_dn(input_path, output_dir, dpi=dpi)

    print("\n── Done ─────────────────────────────────────────────")
    return rows


# Default sample PDF (repo ``data/pdfs/``)
DEFAULT_DN_PDF = DATA_PDFS / "DN-12032026.pdf"


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        pdf_path = sys.argv[1]
        out_dir = sys.argv[2] if len(sys.argv) > 2 else str(OUTPUT_DN)
    else:
        pdf_path = str(DEFAULT_DN_PDF)
        out_dir = str(OUTPUT_DN)
        print(f"No input path given — using default: {pdf_path}")

    if not os.path.isfile(pdf_path):
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        print(
            "Usage: python scripts/dn_hansard_ocr.py [<path/to/DN-….pdf>] [output_dir]",
            file=sys.stderr,
        )
        sys.exit(1)

    extract_dn_hansard(pdf_path, out_dir)
