# OCR Hansard Extractors

Utilities in this repo extract structured parliamentary data from Malaysian Hansard PDFs.

Current focus:
- `DR` (Dewan Rakyat)
- `DN` (Dewan Negara)
- `KKDR` (Kamar Khas)

## Folder Layout

- `scripts/` - extraction scripts
- `data/pdfs/` - input PDFs
- `output/` - generated outputs
  - `output/hansard/` - DR attendance/text outputs (`hansard_ocr.py`)
  - `output/dn_hansard/` - DN attendance/text outputs (`dn_hansard_ocr.py`)
  - `output/kkdr/` - KKDR proceedings outputs
  - `output/dn_proceedings/` - DN proceedings outputs
  - `output/dr_proceedings/` - DR proceedings outputs

## Requirements

Install Python dependencies:

```bash
pip install -r requirements.txt
```

For PDF-to-image fallback paths (OCR mode), install Poppler:

```bash
brew install poppler
```

If you plan to use scanned/image mode with PaddleOCR, install its runtime dependencies as needed.

## Scripts

### 1) DR Hansard Extractor (attendance-oriented)

Script: `scripts/hansard_ocr.py`

Purpose:
- Auto-detects text PDF vs scanned PDF
- Extracts attendance and plain text

Run:

```bash
python scripts/hansard_ocr.py data/pdfs/DR-03032026.pdf
```

Output default:
- `output/hansard/`

---

### 2) DN Hansard Extractor (attendance-oriented)

Script: `scripts/dn_hansard_ocr.py`

Purpose:
- DN-specific attendance parsing rules (`AHLI HADIR`, `TIDAK HADIR`, etc.)
- Auto-detects text PDF vs scanned PDF

Run with default sample:

```bash
python scripts/dn_hansard_ocr.py
```

Run with explicit input:

```bash
python scripts/dn_hansard_ocr.py data/pdfs/DN-12032026.pdf
```

Output default:
- `output/dn_hansard/`

---

### 3) KKDR Proceedings Extractor (speaker turns)

Script: `scripts/kkdr_kamar_khas_extract.py`

Purpose:
- Extracts speaker turns, full speeches, topics, timestamps, and metadata
- For text-layer KKDR proceedings format

Run:

```bash
python scripts/kkdr_kamar_khas_extract.py
```

or

```bash
python scripts/kkdr_kamar_khas_extract.py data/pdfs/KKDR-03032026-1.pdf
```

Output default:
- `output/kkdr/`

Files:
- `<stem>_proceedings.json`
- `<stem>_turns.csv`
- `<stem>_body.txt`

---

### 4) DN/DR Proceedings Extractor (speaker turns)

Script: `scripts/dn_proceedings_extract.py`

Purpose:
- Proceedings-style extraction (not attendance)
- Works for both:
  - `DN` (`DN <date> <page>`)
  - `DR` (`DR. <date> <page>`)
- Extracts speaker turns, full speech, topics, timestamps

DN example:

```bash
python scripts/dn_proceedings_extract.py data/pdfs/DN-12032026.pdf output/dn_proceedings
```

DR example:

```bash
python scripts/dn_proceedings_extract.py data/pdfs/DR-03032026.pdf output/dr_proceedings
```

Default (no args):

```bash
python scripts/dn_proceedings_extract.py
```

Uses:
- input: `data/pdfs/DN-12032026.pdf`
- output: `output/dn_proceedings`

## Output Formats

### Proceedings JSON

Top-level:
- `meta`
  - `sitting_date`
  - `document` (`dewan_rakyat` / `dewan_negara` / `kamar_khas`)
  - `series` (`DR` / `DN` / `KKDR`)
  - `turn_count`
- `turns[]`
  - `index`, `page`, `topic`, `timestamp`
  - `role` (`chair`, `minister`, `member`, `member_or_minister`)
  - `speaker_display`, `portfolio`, `constituency`, `person_name`
  - `speech`, `raw_label`, `meta`

### Proceedings CSV

Flat rows suitable for spreadsheet/manual review:
- `index,page,topic,timestamp,role,speaker_display,portfolio,constituency,person_name,speech`

## Notes

- Text PDFs use `pdfplumber` extraction first.
- Scanned PDFs rely on OCR pipeline (PaddleOCR path in relevant scripts).
- Some role classification can still be ambiguous (`member_or_minister`) for bare name labels; use context or post-processing when needed.
