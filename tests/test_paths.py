import pytest

from frameio_export_watcher.paths import TemplateError, fold, normalize, parse_template


def test_matches_the_real_production_layout():
    template = parse_template("{year}/{client}/{case}/Projektfiler/Eksport")
    fields = template.match(("2026", "Beierholm", "Kundecase #0711", "Projektfiler", "Eksport"))
    assert fields == {"year": "2026", "client": "Beierholm", "case": "Kundecase #0711"}


def test_rejects_a_sibling_folder():
    template = parse_template("{year}/{client}/{case}/Projektfiler/Eksport")
    assert template.match(("2026", "Beierholm", "Sag", "Projektfiler", "Grafik")) is None
    assert template.match(("2026", "Beierholm", "Sag", "Projektfiler")) is None


def test_literal_segments_are_case_insensitive_by_default():
    template = parse_template("{year}/Projektfiler/EKSPORT")
    assert template.match(("2026", "projektfiler", "eksport")) == {"year": "2026"}


def test_case_sensitive_templates_are_strict():
    template = parse_template("{year}/Projektfiler/Eksport", case_sensitive=True)
    assert template.match(("2026", "projektfiler", "Eksport")) is None


def test_mixed_segment_extracts_the_field():
    template = parse_template("Kundecase {number}")
    assert template.match(("Kundecase #0711",)) == {"number": "#0711"}


def test_render_builds_the_frameio_path():
    template = parse_template("{client}/{case}")
    fields = {"year": "2026", "client": "Beierholm", "case": "Kundecase #0711"}
    assert template.render(fields) == ("Beierholm", "Kundecase #0711")


def test_render_reports_unknown_fields():
    template = parse_template("{kunde}")
    with pytest.raises(TemplateError, match="kunde"):
        template.render({"client": "Beierholm"})


def test_backslash_templates_are_accepted():
    template = parse_template(r"{year}\{client}")
    assert template.match(("2026", "Beierholm")) == {"year": "2026", "client": "Beierholm"}


def test_empty_template_is_rejected():
    with pytest.raises(TemplateError):
        parse_template("   ")


def test_danish_names_compare_across_unicode_forms():
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Kundecase Ødegård")  # as macOS writes it over SMB
    assert nfd != "Kundecase Ødegård"
    assert normalize(nfd) == normalize("Kundecase Ødegård")
    assert fold(nfd, case_sensitive=False) == fold("kundecase ØDEGÅRD", case_sensitive=False)
