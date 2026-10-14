from pathlib import Path

import pytest

import auto_qa as qa

ROOT = Path(__file__).parent.parent
RULES = qa.load_rules(ROOT / "rules.example.json")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never call DeepL or LanguageTool."""
    monkeypatch.setattr(qa, "ENABLE_DEEPL_SUGGEST", False)
    monkeypatch.setattr(qa, "ENABLE_GRAMMAR_CHECK", False)


def _rule(name):
    return [r for r in RULES["regex_rules"] if r["name"] == name]


def test_decimal_separator_ignores_thousands():
    text = "Load 1,000 kg, max 1,500 kg, 1,250,000 units, price 12,345.67, size 12,5 mm, 0,75 l."
    wrongs = [i["Wrong"] for i in qa.check_regex_rules(text, _rule("Decimal separator"))]
    assert wrongs == ["12,5", "0,75"]


@pytest.mark.parametrize(
    "word, fix",
    [("color", "colour"), ("Color", "Colour"), ("COLOR", "COLOUR"), ("Safety belt", "Seat belt")],
)
def test_forbidden_term_suggestion_keeps_case(word, fix):
    issues = qa.check_forbidden_terms(f"{word} here", RULES["forbidden_terms"], [])
    assert [i["Suggested"] for i in issues] == [fix]


@pytest.mark.parametrize("sep", [" ", "\u00a0", "\u202f"])  # space, NBSP, narrow NBSP
def test_numbers_thousands_match(sep):
    assert qa.check_numbers(f"1{sep}500 kg", "1,500 kg") == []


@pytest.mark.parametrize("en", ["12.5 mm", "12,5 mm"])  # wrong "12,5" is the regex rule's job
def test_numbers_decimals_match(en):
    assert qa.check_numbers("12,5 mm", en) == []


@pytest.mark.parametrize("sep", [" ", "\u00a0", "\u202f"])
def test_numbers_single_typo_is_one_error(sep):
    issues = qa.check_numbers(f"1{sep}500 kg", "1,800 kg", RULES["units"])
    assert [(i["Severity"], i["Message"]) for i in issues] == [
        ("Error", "Number mismatch: CZ 1500 kg ≠ EN 1800 kg.")
    ]


def test_numbers_mismatch_with_different_units_is_not_merged():
    issues = qa.check_numbers("75 kg", "57 l", RULES["units"])
    assert {(i["Severity"], i["Wrong"]) for i in issues} == {("Error", "75"), ("Warning", "57")}


def test_numbers_missing_unit():
    issues = qa.check_numbers("Rychlost 130 km/h, 4 kusy", "Speed 130, 4 pieces", RULES["units"])
    assert [(i["Wrong"], i["Message"]) for i in issues] == [
        ("130", "Missing unit in EN: CZ has '130 km/h'.")
    ]


def test_example_pdfs_report_exactly_the_planted_errors():
    cz = qa.extract_text_from_pdf(str(ROOT / "examples" / "velora_roof_box_cz.pdf"))
    en = qa.extract_text_from_pdf(str(ROOT / "examples" / "velora_roof_box_en.pdf"))
    issues = qa.run_qa_checks(cz, en, RULES)
    assert {(i["Wrong"], i["Message"]) for i in issues} == {
        ("57", "Number mismatch: CZ 75 kg ≠ EN 57 kg."),  # wrong number
        ("130", "Missing unit in EN: CZ has '130 km/h'."),  # missing unit
        ("Color", "Forbidden term 'color' found (UK spelling)."),  # American English
        ("trunk", "Forbidden term 'trunk' found (UK wording)."),  # forbidden term
    }


def test_languagetool_noise_is_filtered(monkeypatch):
    en = "The Glidetex box comes in a nice color with a NIVARA badge. Their is a typo."
    matches = [
        {"offset": en.index(w), "length": len(w), "message": msg}
        for w, msg in [
            ("Glidetex", "Possible spelling mistake."),  # grammar_whitelist
            ("NIVARA", "Possible spelling mistake."),  # ALL CAPS
            ("color", "American English spelling."),  # already a forbidden term
            ("Their", "Did you mean 'There'?"),  # real issue
        ]
    ]

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"matches": matches}

    monkeypatch.setattr(qa, "ENABLE_GRAMMAR_CHECK", True)
    monkeypatch.setattr(qa.requests, "post", lambda *a, **kw: FakeResponse())
    issues = qa.dedupe_language_overlaps(qa.run_qa_checks("", en, RULES))
    assert [i["Wrong"] for i in issues if i["Type"] == "English"] == ["Their"]
