# auto_qa.py
# EN translation QA – pairing CZ/EN PDFs, extracting text, running QA checks, and exporting HTML/CSV reports.
# All client-specific rules live in a JSON rules file (default: rules.example.json).
# Dependencies: PyMuPDF (fitz), pandas, requests, python-dotenv

import os
import re
import json
import argparse
import difflib
import webbrowser
from datetime import datetime
import fitz  # PyMuPDF
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()


# === DeepL integration (optional) ===
try:
    import deepl

    DEEPL_AVAILABLE = True
except Exception:
    DEEPL_AVAILABLE = False

ENABLE_GRAMMAR_CHECK = True  # toggle grammar check (LanguageTool)
ENABLE_DEEPL_SUGGEST = True  # toggle DeepL suggestions (INFO only)
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")  # set in .env or system environment
DEEPL_TIMEOUT = 15  # seconds
LT_URL = os.getenv("LT_URL", "http://localhost:8081/v2")  # local LanguageTool server

# === PATHS / CONFIG ===
DEFAULT_RULES = "rules.example.json"
INPUT_DIR = "Input"
OUTPUT_DIR = "Output"

# Noise handling
IGNORE_INFO_WITHOUT_SUGGESTION = False  # Skip INFO rows that have no suggestion
HIDE_INFO_IN_REPORT = False  # Do not include INFO rows in CSV/HTML


# ---------- Helpers ----------
def _sev_key(s: str) -> str:
    if not s:
        return "INFO"
    s = s.strip().upper()
    if s in ("ERROR", "ERR"):
        return "ERROR"
    if s in ("WARNING", "WARN"):
        return "WARNING"
    return "INFO"


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _compute_score(errors: int, warnings: int, scoring: dict) -> int:
    """score = 100 - error_penalty*errors - warning_penalty*warnings (min 0)."""
    score = 100 - scoring["error_penalty"] * errors - scoring["warning_penalty"] * warnings
    return int(max(0, score))


# === DeepL helper functions ===
_deepl_cache = {"translator": None, "glossary": None}


def _get_deepl_translator():
    if not (DEEPL_AVAILABLE and DEEPL_API_KEY):
        return None
    if _deepl_cache["translator"] is None:
        _deepl_cache["translator"] = deepl.Translator(
            DEEPL_API_KEY, timeout=DEEPL_TIMEOUT
        )
    return _deepl_cache["translator"]


def _ensure_deepl_glossary(rules):
    """Reuse or create the CS->EN DeepL glossary built from the termbase."""
    tr = _get_deepl_translator()
    if not tr:
        return None
    if _deepl_cache["glossary"] is not None:
        return _deepl_cache["glossary"]
    entries = {t["cz"].strip(): t["en"].strip() for t in rules["termbase"]}
    if not entries:
        return None
    name = rules["deepl_glossary_name"]
    try:
        for g in tr.list_glossaries():
            if g.name == name:
                _deepl_cache["glossary"] = g
                return g
        # Glossaries are not regional: target is "EN", not "EN-GB"
        target = rules["language"].split("-")[0].upper()
        g = tr.create_glossary(name, source_lang="CS", target_lang=target, entries=entries)
        _deepl_cache["glossary"] = g
        return g
    except Exception:
        return None


# ---------- Load rules ----------
def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def remove_ligatures(text: str) -> str:
    """Replace PDF ligatures (ﬁ, ﬂ, ﬀ, ﬃ, ﬄ) with normal characters."""
    lig_map = {
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬀ": "ff",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
    }
    for lig, norm in lig_map.items():
        text = text.replace(lig, norm)
    return text


# ---------- PDF text extraction ----------
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract plain text from a (non-scanned) PDF using PyMuPDF,
    then normalize ligatures so LT and regexes work correctly."""
    try:
        doc = fitz.open(pdf_path)
        chunks = []
        for page in doc:
            chunks.append(page.get_text("text"))
        doc.close()

        text = "\n".join(chunks)
        text = remove_ligatures(text)

        return text

    except Exception as e:
        print(f"[ERROR] Cannot read PDF {pdf_path}: {e}")
        return ""


# ---------- Pair finder ----------
def find_pdf_pairs(input_dir: str):
    """
    Find (CZ, EN) pairs recursively in all subfolders.
    Looks for <base>_cz.pdf + <base>_en.pdf in the SAME folder.
    Other language versions (e.g. _de.pdf) are ignored.
    Returns a list of (full_path_CZ, full_path_EN) tuples.
    """
    pairs = []

    for root, dirs, files in os.walk(input_dir):
        # Only PDFs in the current folder
        pdfs = [f for f in files if f.lower().endswith(".pdf")]
        if not pdfs:
            continue

        # map lower-case name -> real name
        lower_map = {f.lower(): f for f in pdfs}

        # find *_cz.pdf and the matching *_en.pdf
        for name_low, real_name in lower_map.items():
            if name_low.endswith("_cz.pdf"):
                base = name_low[:-7]  # strip "_cz.pdf"
                en_key = base + "_en.pdf"
                if en_key in lower_map:
                    cz_real = lower_map[name_low]
                    en_real = lower_map[en_key]

                    cz_path = os.path.join(root, cz_real)
                    en_path = os.path.join(root, en_real)
                    pairs.append((cz_path, en_path))

    return pairs


# ---------- Issues & rules ----------
def _suggest_fix(token: str) -> str:
    if not token:
        return ""
    t = token.strip()
    low = t.lower()

    # decimal comma -> dot
    if re.fullmatch(r"\d+,\d+", t):
        return t.replace(",", ".")

    # spacing before %
    if re.fullmatch(r"\d+\s+%", t):
        return t.replace(" %", "%")

    # nm -> N·m (torque)
    if re.search(r"\bnm\b", low):
        return re.sub(r"(?i)\bnm\b", "N·m", t)

    # degree symbol
    if re.search(r"°c\b", low):
        return re.sub(r"(?i)°c\b", "°C", t)

    # double space
    if re.search(r"\s{2,}", t):
        return re.sub(r"\s{2,}", " ", t)

    # US double quotes -> simple UK single quotes
    if '"' in t:
        return t.replace('"', "'")

    # Default: leave empty
    return ""


def _match_case(found: str, repl: str) -> str:
    """Copy the casing of `found` onto `repl` (COLOR → COLOUR, Color → Colour)."""
    if found.isupper():
        return repl.upper()
    if found[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _extract_context(text: str, start: int, end: int, window: int = 3) -> str:
    """
    Return the error marked as **wrong** with up to `window` words on each side.
    """
    left = re.search(
        rf"(?:\S*\s+){{0,{window}}}\S*\Z", text[max(0, start - 200) : start]
    ).group(0)
    right = re.match(rf"\S*(?:\s+\S*){{0,{window}}}", text[end : end + 200]).group(0)
    # Collapse whitespace around the error, but keep the error itself verbatim (e.g. double spaces)
    left = re.sub(r"\s+", " ", left).lstrip()
    right = re.sub(r"\s+", " ", right).rstrip()
    return f"{left}**{text[start:end].replace(chr(10), ' ')}**{right}"


def _attach_file_and_suggestions(issues, file_label: str):
    for it in issues:
        it.setdefault("File", file_label)
        if not (it.get("Suggested") or "").strip():
            it["Suggested"] = _suggest_fix(it.get("Wrong") or "")
    return issues


def check_forbidden_terms(en_text: str, forbidden_terms, phrase_whitelist):
    """
    Find forbidden terms. Hits that are part of a whitelisted phrase
    (e.g. an official product name) are ignored.
    """
    issues = []
    low_text = en_text.lower()
    whitelist = [p.lower() for p in phrase_whitelist]

    for term in forbidden_terms:
        wrong = term["wrong"]
        pattern = re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE)

        for m in pattern.finditer(en_text):
            start, end = m.span()

            # Small window around the hit (± 20 chars)
            context = low_text[max(0, start - 20) : min(len(low_text), end + 20)]

            # Skip the hit if a whitelisted phrase is in the window
            if any(phrase in context for phrase in whitelist):
                continue

            issues.append(
                {
                    "Type": "Terminology",
                    "Severity": "Error",
                    "Message": f"Forbidden term '{wrong}' found ({term.get('reason', 'forbidden')}).",
                    "Wrong": m.group(0),
                    "Suggested": _match_case(m.group(0), term.get("correct", "")),
                    "Context": _extract_context(en_text, start, end),
                }
            )

    return issues


def check_terminology(en_text: str, termbase):
    """
    Warn if a Czech term from the termbase appears in EN text.
    """
    issues = []
    low = en_text.lower()
    for t in termbase:
        cz, en = t["cz"], t["en"]
        idx = low.find(cz.lower())
        if cz and idx >= 0 and cz.lower() != en.lower():
            issues.append(
                {
                    "Type": "Terminology",
                    "Severity": "Warning",
                    "Message": f"Czech term '{cz}' found in English text.",
                    "Wrong": cz,
                    "Suggested": en,
                    "Context": _extract_context(en_text, idx, idx + len(cz)),
                }
            )
    return issues


# Groups: 1 = integer part with thousands separators, 2 = its decimals,
#         3 = plain integer part, 4 = its decimals.
_NUMBER_RE = {
    # CZ: thousands = space / NBSP (U+00A0) / narrow NBSP (U+202F), decimal = comma
    # (a dot is tolerated too, e.g. "1.5 TSI").
    # Known limitation: two separate numbers divided by a single space ("10 200")
    # are merged into one (10200) – plain text alone cannot tell them apart.
    "cz": re.compile(
        r"\b(\d{1,3}(?:[ \u00a0\u202f]\d{3})+)(?:,(\d+))?(?!\d)|\b(\d+)(?:[.,](\d+))?(?!\d)"
    ),
    # EN: thousands = comma + exactly 3 digits, decimal = dot. A wrong decimal comma
    # ("12,5") is read as 12.5 – the "Decimal separator" regex rule reports it.
    "en": re.compile(
        r"\b(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?(?!\d)|\b(\d+)(?:[.,](\d+))?(?!\d)"
    ),
}


def _find_numbers(text: str, lang: str, unit_re=None) -> dict:
    """
    Map each normalised number (no thousands separators, dot decimal) to
    (span of its first occurrence, unit found after any occurrence or "").
    """
    found = {}
    for m in _NUMBER_RE[lang].finditer(text):
        whole = re.sub(r"\D", "", m.group(1) or m.group(3))
        frac = m.group(2) or m.group(4)
        num = f"{whole}.{frac}" if frac else whole
        unit = unit_re.match(text, m.end()) if unit_re else None
        span, seen_unit = found.get(num, (m.span(), ""))
        found[num] = (span, seen_unit or (unit.group(1) if unit else ""))
    return found


def check_numbers(cz_text: str, en_text: str, units=()):
    """
    Compare numbers present in CZ vs EN texts (simple set approach),
    and warn when a CZ number has a unit but the same number in EN never does.
    """
    unit_re = None
    if units:
        alts = "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
        unit_re = re.compile(rf"[ \u00a0\u202f]?({alts})(?!\w)")
    cz_nums = _find_numbers(cz_text, "cz", unit_re)
    en_nums = _find_numbers(en_text, "en", unit_re)

    cz_only = sorted(cz_nums.keys() - en_nums.keys())
    en_only = sorted(en_nums.keys() - cz_nums.keys())

    issues = []
    # Exactly one unmatched number on each side with the same unit = one typo (CZ 75 kg, EN 57 kg)
    if len(cz_only) == len(en_only) == 1:
        cz_num, en_num = cz_only[0], en_only[0]
        unit = cz_nums[cz_num][1]
        if unit and unit == en_nums[en_num][1]:
            issues.append(
                {
                    "Type": "Numerical",
                    "Severity": "Error",
                    "Message": f"Number mismatch: CZ {cz_num} {unit} ≠ EN {en_num} {unit}.",
                    "Wrong": en_num,
                    "Suggested": f"{cz_num} {unit}",
                    "Context": _extract_context(en_text, *en_nums[en_num][0]),
                }
            )
            cz_only = en_only = []

    for num in cz_only:
        issues.append(
            {
                "Type": "Numerical",
                "Severity": "Error",
                "Message": f"Number mismatch: CZ has {num}, not found in EN.",
                "Wrong": num,
                "Suggested": "Ensure the same numeric value appears in EN (use dot as decimal separator).",
                "Context": "CZ: " + _extract_context(cz_text, *cz_nums[num][0]),
            }
        )

    for num in en_only:
        issues.append(
            {
                "Type": "Numerical",
                "Severity": "Warning",
                "Message": f"Extra number in EN not present in CZ: {num}.",
                "Wrong": num,
                "Suggested": "Check if this number should be in CZ or removed from EN.",
                "Context": _extract_context(en_text, *en_nums[num][0]),
            }
        )

    for num in sorted(cz_nums.keys() & en_nums.keys()):
        cz_unit = cz_nums[num][1]
        if cz_unit and not en_nums[num][1]:
            issues.append(
                {
                    "Type": "Numerical",
                    "Severity": "Warning",
                    "Message": f"Missing unit in EN: CZ has '{num} {cz_unit}'.",
                    "Wrong": num,
                    "Suggested": f"Add the unit ({cz_unit}).",
                    "Context": _extract_context(en_text, *en_nums[num][0]),
                }
            )
    return issues


def check_regex_rules(en_text: str, regex_rules):
    """
    Apply regex rules from the rules file to EN text.

    Optional per-rule flags:
      case_sensitive    – match case-sensitively (default: ignore case)
      skip_if_uppercase – ignore matches already written in ALL CAPS
      allow_exact       – ignore matches equal to this exact form (whitespace-normalised)
      suggest           – fixed suggestion shown in the report
    """
    issues = []

    for rule in regex_rules:
        flags = 0 if rule.get("case_sensitive") else re.IGNORECASE
        try:
            regex = re.compile(rule["pattern"], flags)
        except re.error as e:
            print(f"[WARN] Invalid regex in rule '{rule.get('name')}': {e}")
            continue

        for m in regex.finditer(en_text):
            token = m.group(0)

            if rule.get("skip_if_uppercase") and token.isupper():
                continue

            if " ".join(token.split()) == rule.get("allow_exact"):
                continue

            issues.append(
                {
                    "Type": "Formatting",
                    "Severity": rule["severity"],
                    "Message": rule["message"],
                    "Wrong": token,
                    "Suggested": rule.get("suggest", ""),
                    "Context": _extract_context(en_text, *m.span()),
                }
            )

    return issues


def check_with_deepl(cz_text: str, en_text: str, rules):
    """
    Generate a 'reference' translation via DeepL + glossary; if it differs
    a lot from the EN text, add an INFO suggestion. (No effect on QA score.)
    """
    if not (ENABLE_DEEPL_SUGGEST and cz_text.strip() and en_text.strip()):
        return []
    tr = _get_deepl_translator()
    if tr is None:
        return []
    gloss = _ensure_deepl_glossary(rules)
    try:
        ref = tr.translate_text(
            cz_text,
            source_lang="CS",
            target_lang=rules["language"].upper(),
            glossary=gloss,
        ).text
    except Exception:
        return []

    issues = []
    if len(ref.split()) >= 6:
        ratio = difflib.SequenceMatcher(None, en_text.lower(), ref.lower()).ratio()
        if ratio < 0.65:
            issues.append(
                {
                    "Type": "Style",
                    "Severity": "INFO",
                    "Message": "Consider alternative phrasing (DeepL reference).",
                    "Wrong": (en_text[:180] + "…") if len(en_text) > 180 else en_text,
                    "Suggested": (ref[:180] + "…") if len(ref) > 180 else ref,
                }
            )
    return issues


def check_english_quality(en_text: str, rules):
    """
    English check via a local LanguageTool HTTP server (LT_URL,
    started by MASTER_RUN.bat). Filters noise (brand terms, ALL CAPS, long technical words).
    """
    issues = []

    if not ENABLE_GRAMMAR_CHECK or not en_text.strip():
        return issues

    try:
        resp = requests.post(
            f"{LT_URL}/check",
            data={
                "text": en_text,
                "language": rules["language"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("matches", [])
    except Exception:
        issues.append(
            {
                "Type": "English",
                "Severity": "INFO",
                "Message": "Grammar check skipped (local LanguageTool server not reachable).",
                "Wrong": "",
                "Suggested": f"Start LT server on {LT_URL} or disable grammar check.",
            }
        )
        return issues

    whitelist = {w.lower() for w in rules["grammar_whitelist"]}

    for m in matches:
        offset = m.get("offset", 0)
        length = m.get("length", 0)
        wrong = en_text[offset : offset + length]
        wrong_clean = (wrong or "").strip()

        # 1) whitelist from the rules file
        if wrong_clean.lower() in whitelist:
            continue

        # 2) anything in ALL CAPS (model names, LED, ABS...)
        if wrong_clean.isupper() and len(wrong_clean) >= 3:
            continue

        # 3) long technical words – letters and hyphens only
        if re.fullmatch(r"[A-Za-z\-]+", wrong_clean) and len(wrong_clean) >= 15:
            continue

        msg = m.get("message", "Possible issue found.")
        repl_values = [
            r.get("value")
            for r in m.get("replacements", [])
            if isinstance(r, dict) and r.get("value")
        ]
        suggested = ", ".join(repl_values[:3])

        issues.append(
            {
                "Type": "English",
                "Severity": "ERROR",
                "Message": msg,
                "Wrong": wrong_clean,
                "Suggested": suggested,
                "Context": _extract_context(en_text, offset, offset + length),
            }
        )

    return issues


def dedupe_language_overlaps(issues: list[dict]) -> list[dict]:
    """
    Remove LanguageTool hits that duplicate our own rules.
    Keeps the more specific custom issue, drops only the redundant English one.
    """
    custom_wrongs = {
        (i.get("Wrong") or "").strip()
        for i in issues
        if i.get("Type") != "English" and (i.get("Wrong") or "").strip()
    }

    filtered: list[dict] = []
    for it in issues:
        if it.get("Type") == "English":
            # 1) same text already reported by a custom rule (brand, model, forbidden term…)
            if (it.get("Wrong") or "").strip() in custom_wrongs:
                continue

            # 2) whitespace – we have our own regex rule
            low_msg = (it.get("Message") or "").lower()
            if "consecutive spaces" in low_msg or "repeated a whitespace" in low_msg:
                continue

        filtered.append(it)

    return filtered


def run_qa_checks(cz_text: str, en_text: str, rules):
    all_issues = []
    all_issues += check_forbidden_terms(
        en_text, rules["forbidden_terms"], rules["forbidden_phrase_whitelist"]
    )
    all_issues += check_terminology(en_text, rules["termbase"])
    all_issues += check_numbers(cz_text, en_text, rules.get("units", ()))
    all_issues += check_regex_rules(en_text, rules["regex_rules"])
    all_issues += check_with_deepl(cz_text, en_text, rules)
    all_issues += check_english_quality(en_text, rules)

    # Noise filtering
    if IGNORE_INFO_WITHOUT_SUGGESTION:
        filtered = []
        for it in all_issues:
            if (
                _sev_key(it.get("Severity")) == "INFO"
                and not (it.get("Suggested") or "").strip()
            ):
                # Drop low-value INFO without suggested fix
                continue
            filtered.append(it)
        all_issues = filtered

    return all_issues


# ---------- Output / reporting ----------
def prepare_output_folder(base_name: str) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = os.path.join(OUTPUT_DIR, f"{base_name}_QA_{date_str}")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_reports(base_name: str, output_dir: str, issues, rules):
    os.makedirs(output_dir, exist_ok=True)

    if HIDE_INFO_IN_REPORT:
        issues = [i for i in issues if _sev_key(i.get("Severity")) != "INFO"]

    issues = dedupe_language_overlaps(issues)

    # --- Sort by severity (ERROR → WARNING → INFO) ---
    sev_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    issues.sort(key=lambda i: sev_order.get(_sev_key(i.get("Severity")), 3))
    # ---------------------------------------------------------------

    df = pd.DataFrame(issues)
    csv_path = os.path.join(output_dir, "qa_report.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # Summary
    errors = sum(1 for x in issues if _sev_key(x.get("Severity")) == "ERROR")
    warnings = sum(1 for x in issues if _sev_key(x.get("Severity")) == "WARNING")
    infos = sum(1 for x in issues if _sev_key(x.get("Severity")) == "INFO")
    score = _compute_score(errors, warnings, rules["scoring"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # HTML
    color_map = {"ERROR": "#ffd6d6", "WARNING": "#fff1c2", "INFO": "#dfeaff"}
    rows = []
    for it in issues:
        sev = _sev_key(it.get("Severity"))
        bg = color_map.get(sev, "#ffffff")
        # **wrong** marker from _extract_context -> highlighted
        ctx = re.sub(
            r"\*\*(.+?)\*\*", r"<mark>\1</mark>", _html_escape(it.get("Context", ""))
        )
        rows.append(
            "<tr style='background:{bg}'>"
            "<td>{typ}</td>"
            "<td><b>{sev}</b></td>"
            "<td>{msg}</td>"
            "<td>{wrong}</td>"
            "<td><b>{sugg}</b></td>"
            "<td>{ctx}</td>"
            "</tr>".format(
                bg=bg,
                typ=_html_escape(it.get("Type")),
                sev=_html_escape(sev),
                msg=_html_escape(it.get("Message")),
                wrong=_html_escape(it.get("Wrong")),
                sugg=_html_escape(it.get("Suggested")),
                ctx=ctx,
            )
        )

    if not rows:
        rows.append(
            "<tr><td colspan='6' style='text-align:center;padding:12px'>"
            "✅ No issues found. Looks good."
            "</td></tr>"
        )

    gate_html = ""
    threshold = rules["scoring"].get("pass_threshold")  # null disables PASS/FAIL badge
    if threshold is not None:
        pass_fail = "✅ PASS" if score >= threshold else "❌ FAIL"
        gate_html = (
            f"<span class='pill' style='background:#e8e8e8'>"
            f"QA score: <b>{score}%</b> ({pass_fail}; min {threshold}%)"
            f"</span>"
        )

    html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>QA Report — {base_name}</title>
<style>
  body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; }}
  .pill {{ display:inline-block; padding:6px 10px; margin-right:8px; border-radius:10px; font-weight:600; }}
  .err {{ background:#ffd6d6; }}
  .war {{ background:#fff1c2; }}
  .inf {{ background:#dfeaff; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 8px; vertical-align: top; }}
  th {{ background:#f4f4f4; text-align:left; }}
  .summary {{ margin-bottom: 14px; }}
  .small {{ color:#666; font-size: 12px; }}
</style>
</head>
<body>
  <h2>Translation QA Report — {base_name}</h2>
  <h3 style="color:#444;">Total issues: <span style="color:#0078D7;">{len(issues)}</span></h3>
  <div class="summary">
    <span class="pill">🗓️ {now}</span>
    <span class="pill err">❌ Errors: {errors}</span>
    <span class="pill war">⚠️ Warnings: {warnings}</span>
    <span class="pill inf">ℹ️ Info: {infos}</span>
    {gate_html}
  </div>
  <table>
    <thead>
      <tr>
        <th>Type</th>
        <th>Severity</th>
        <th>Message</th>
        <th>Wrong text</th>
        <th>Suggested fix</th>
        <th>Context</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>

  <p class="small">{_html_escape(rules.get("report_note", ""))}</p>
</body>
</html>"""

    html_path = os.path.join(output_dir, "qa_report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    return (
        csv_path,
        html_path,
        {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
            "score": score,
        },
    )


# ---------- Processing ----------
def process_pair(cz_pdf_path: str, en_pdf_path: str, rules):
    base_name = os.path.basename(en_pdf_path)
    if base_name.lower().endswith("_en.pdf"):
        base_name = base_name[:-7]  # remove "_EN.pdf"

    out_dir = prepare_output_folder(base_name)

    cz_text = extract_text_from_pdf(cz_pdf_path)
    en_text = extract_text_from_pdf(en_pdf_path)

    issues = run_qa_checks(cz_text, en_text, rules)
    issues = _attach_file_and_suggestions(issues, os.path.basename(en_pdf_path))

    csv_path, html_path, summary = save_reports(base_name, out_dir, issues, rules)
    return {"base": base_name, "csv": csv_path, "html": html_path, **summary}


def main():
    parser = argparse.ArgumentParser(description="QA checker for CZ/EN PDF translations.")
    parser.add_argument(
        "--rules", default=DEFAULT_RULES, help=f"rules JSON file (default: {DEFAULT_RULES})"
    )
    parser.add_argument(
        "--input", default=INPUT_DIR, help=f"folder with CZ/EN PDF pairs (default: {INPUT_DIR})"
    )
    args = parser.parse_args()

    os.makedirs(args.input, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[INFO] Loading rules from {args.rules} …")
    rules = load_rules(args.rules)

    pairs = find_pdf_pairs(args.input)
    if not pairs:
        print(
            f"[WARN] No pairs found in '{args.input}'. Use names like 'XYZ_CZ.pdf' + 'XYZ_EN.pdf'."
        )
        return

    print(f"[INFO] Found {len(pairs)} pair(s). Processing …")
    opened_html = []
    totals = {"errors": 0, "warnings": 0, "infos": 0}
    scores = []

    for cz_path, en_path in pairs:
        print(f"  • {os.path.basename(en_path)}  (with {os.path.basename(cz_path)})")
        result = process_pair(cz_path, en_path, rules)
        totals["errors"] += result["errors"]
        totals["warnings"] += result["warnings"]
        totals["infos"] += result["infos"]
        scores.append(result.get("score", 100))
        opened_html.append(result["html"])

    avg_score = (sum(scores) // len(scores)) if scores else 100
    print(
        f"[DONE] All pairs processed. "
        f"Errors={totals['errors']}  Warnings={totals['warnings']}  Info={totals['infos']}  AvgScore={avg_score}%"
    )

    # Try to open all HTML reports in default browser
    for html_file in opened_html:
        try:
            webbrowser.open(f"file:///{os.path.abspath(html_file)}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
