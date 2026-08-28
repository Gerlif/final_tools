import pytest

from frameio_export_watcher.paths import (
    TemplateError,
    fold,
    normalize,
    parse_template,
    split_version,
)


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


@pytest.mark.parametrize(
    "name, identity, version",
    [
        # The version sits at the end, in the middle, with any separator.
        ("Beierholm - HERO v1.mp4", "Beierholm HERO", (1,)),
        ("Beierholm - HERO v1.1.mp4", "Beierholm HERO", (1, 1)),
        ("Beierholm - HERO V11.mp4", "Beierholm HERO", (11,)),
        ("CBS SCM_6 sek_V02_1x1.mov", "CBS SCM 6 sek 1x1", (2,)),
        ("CBS - Girltalk v3 16x9.mp4", "CBS Girltalk 16x9", (3,)),
        ("CBS - Girltalk v04_9x16.mp4", "CBS Girltalk 9x16", (4,)),
        ("CBS - Girltalk - HERO 1:1 v2.mp4", "CBS Girltalk HERO 1:1", (2,)),
        # No marker at all.
        ("Beierholm - HERO.mp4", "Beierholm HERO", None),
        # The letters appear, but not as a token of their own.
        ("Groov5.mp4", "Groov5", None),
        ("TV2 spot.mp4", "TV2 spot", None),
    ],
)
def test_version_markers_are_found_wherever_they_sit(name, identity, version):
    parsed = split_version(name)
    assert (parsed.identity, parsed.version) == (identity, version)


def test_the_dot_in_v1_1_is_a_version_not_a_file_type():
    assert split_version("spot v1.1.mp4").extension == ".mp4"
    assert split_version("spot v1.1").extension == ""
    assert split_version("spot v1.1").version == (1, 1)


def test_an_aspect_ratio_is_part_of_what_the_file_is():
    """A 16x9 and a 9x16 are two deliverables, never versions of each other."""
    wide = split_version("CBS - Girltalk v3 16x9.mp4")
    tall = split_version("CBS - Girltalk v04_9x16.mp4")
    assert not wide.same_asset_as(tall, case_sensitive=False)


def test_the_same_asset_is_recognised_across_separator_styles():
    spaced = split_version("CBS - Girltalk v3 16x9.mp4")
    underscored = split_version("CBS_Girltalk_16x9_v5.mp4")
    assert spaced.same_asset_as(underscored, case_sensitive=False)


def test_a_different_file_type_is_a_different_asset():
    video = split_version("spot v1.mp4")
    audio = split_version("spot v1.wav")
    assert not video.same_asset_as(audio, case_sensitive=False)
