"""
Generate the fictional CZ/EN product sheets in this folder (VELORA is a made-up brand).

The EN sheet contains four deliberate errors, marked "# ERROR" below;
tests/test_auto_qa.py checks that the QA tool finds exactly these.

Run:  python examples/make_examples.py
"""

from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).parent

CSS = """
* { font-family: sans-serif; font-size: 11pt; }
.brand { color: #666; font-size: 9pt; }
h1 { font-size: 18pt; margin-bottom: 4pt; }
h2 { font-size: 13pt; margin-top: 14pt; }
td { padding: 2pt 16pt 2pt 0; }
"""

SHEETS = {
    "cz": {
        "brand": "VELORA Originální příslušenství",
        "title": "Střešní box Glidetex 420",
        "intro": "Aerodynamický střešní box pro modely NIVARA a TALVIK. "
        "Oboustranné otevírání a centrální zamykání usnadňují nakládání.",
        "specs_title": "Technické parametry",
        "specs": [
            ("Objem", "420 l"),
            ("Nosnost", "75 kg"),
            ("Hmotnost", "14,5 kg"),
            ("Rozměry", "1\u202f900 × 800 × 390 mm"),  # narrow NBSP as thousands separator
            ("Maximální rychlost", "130 km/h"),
            ("Provozní teplota", "−30 až 60 °C"),
        ],
        "install_title": "Montáž",
        "install": "Box se upevňuje na příčníky VELORA pomocí 4 rychloupínačů. "
        "Utahovací moment: 6 N·m. Montážní sada je uložena v zavazadlovém prostoru.",
        "colour": "Barva: černá metalíza",
    },
    "en": {
        "brand": "VELORA Genuine Accessories",
        "title": "Roof box Glidetex 420",
        "intro": "Aerodynamic roof box for NIVARA and TALVIK models. "
        "Dual-side opening and central locking make loading easy.",
        "specs_title": "Technical data",
        "specs": [
            ("Volume", "420 l"),
            ("Load capacity", "57 kg"),  # ERROR: wrong number (CZ 75 kg)
            ("Weight", "14.5 kg"),
            ("Dimensions", "1,900 × 800 × 390 mm"),
            ("Maximum speed", "130"),  # ERROR: missing unit (km/h)
            ("Operating temperature", "−30 to 60 °C"),
        ],
        "install_title": "Installation",
        "install": "The box is attached to VELORA roof bars with 4 quick-release clamps. "
        "Tightening torque: 6 N·m. "
        "The installation kit is stored in the trunk.",  # ERROR: forbidden term (luggage compartment)
        "colour": "Color: black metallic",  # ERROR: American English (colour)
    },
}


def to_html(s: dict) -> str:
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in s["specs"])
    return (
        f"<p class='brand'>{s['brand']}</p><h1>{s['title']}</h1><p>{s['intro']}</p>"
        f"<h2>{s['specs_title']}</h2><table>{rows}</table>"
        f"<h2>{s['install_title']}</h2><p>{s['install']}</p><p><b>{s['colour']}</b></p>"
    )


def main():
    for lang, sheet in SHEETS.items():
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_htmlbox(fitz.Rect(56, 56, 539, 786), to_html(sheet), css=CSS)
        path = HERE / f"velora_roof_box_{lang}.pdf"
        doc.save(str(path))
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
