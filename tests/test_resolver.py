import pytest

from frameio_export_watcher.config import FrameioConfig
from frameio_export_watcher.paths import parse_template
from frameio_export_watcher.resolver import DestinationResolver, NoMatch, ResolveError

from fakes import FakeFrameio


def make_config(**overrides) -> FrameioConfig:
    defaults = dict(
        project_template=parse_template("{year}"),
        folder_template=parse_template("{client}/{case}"),
        account_id="acc-1",
    )
    defaults.update(overrides)
    return FrameioConfig(**defaults)


FIELDS = {"year": "2026", "client": "Beierholm", "case": "Kundecase #0711"}


def build_matching_api() -> FakeFrameio:
    api = FakeFrameio()
    project = api.add_project("2026")
    client = api.add_folder(project.root_folder_id, "Beierholm")
    api.add_folder(client.id, "Kundecase #0711")
    return api


def test_resolves_year_project_and_nested_folders():
    api = build_matching_api()
    resolver = DestinationResolver(api, make_config())
    destination = resolver.resolve(FIELDS)
    assert destination.display == "2026/Beierholm/Kundecase #0711"
    assert destination.project_name == "2026"


def test_missing_case_folder_is_a_no_match():
    api = FakeFrameio()
    project = api.add_project("2026")
    api.add_folder(project.root_folder_id, "Beierholm")
    resolver = DestinationResolver(api, make_config())
    outcome = resolver.resolve(FIELDS)
    assert isinstance(outcome, NoMatch)
    assert "Kundecase #0711" in outcome.reason


def test_missing_project_is_a_no_match():
    api = FakeFrameio()
    api.add_project("2025")
    resolver = DestinationResolver(api, make_config())
    outcome = resolver.resolve(FIELDS)
    assert isinstance(outcome, NoMatch)
    assert "2026" in outcome.reason


def test_results_are_cached_so_scans_do_not_hammer_the_api():
    api = build_matching_api()
    resolver = DestinationResolver(api, make_config())
    for _ in range(5):
        resolver.resolve(FIELDS)
    assert sum(1 for call in api.calls if call[0] == "children") == 2


def test_no_match_is_cached_too():
    api = FakeFrameio()
    api.add_project("2025")
    resolver = DestinationResolver(api, make_config())
    for _ in range(3):
        assert isinstance(resolver.resolve(FIELDS), NoMatch)
    # One miss triggers exactly one refresh of the project listing.
    assert sum(1 for call in api.calls if call[0] == "list_projects") == 2


def test_folder_names_match_case_insensitively_by_default():
    api = FakeFrameio()
    project = api.add_project("2026")
    client = api.add_folder(project.root_folder_id, "BEIERHOLM")
    api.add_folder(client.id, "kundecase #0711")
    resolver = DestinationResolver(api, make_config())
    assert resolver.resolve(FIELDS).folder_path == ("BEIERHOLM", "kundecase #0711")


def test_case_sensitive_mode_does_not_match_different_casing():
    api = FakeFrameio()
    project = api.add_project("2026")
    api.add_folder(project.root_folder_id, "BEIERHOLM")
    resolver = DestinationResolver(api, make_config(case_sensitive_names=True))
    assert isinstance(resolver.resolve(FIELDS), NoMatch)


def test_project_root_is_used_when_no_folder_template():
    api = FakeFrameio()
    project = api.add_project("2026")
    resolver = DestinationResolver(api, make_config(folder_template=None))
    destination = resolver.resolve(FIELDS)
    assert destination.folder_id == project.root_folder_id


def test_ambiguous_account_is_reported():
    api = FakeFrameio(accounts=[{"id": "a", "display_name": "A"}, {"id": "b", "display_name": "B"}])
    resolver = DestinationResolver(api, make_config(account_id=None))
    with pytest.raises(ResolveError, match="several Frame.io accounts"):
        resolver.resolve(FIELDS)


def test_multi_segment_project_template_is_rejected():
    api = build_matching_api()
    resolver = DestinationResolver(api, make_config(project_template=parse_template("{year}/{client}")))
    with pytest.raises(ResolveError, match="single project name"):
        resolver.resolve(FIELDS)


def test_workspace_filter_limits_projects():
    api = FakeFrameio(workspaces=[{"id": "ws-other", "name": "Arkiv"}])
    project = api.add_project("2026")
    api.add_folder(project.root_folder_id, "Beierholm")
    resolver = DestinationResolver(api, make_config(workspace_name="Arkiv"))
    # The project lives in ws-1, not in the requested workspace.
    assert isinstance(resolver.resolve(FIELDS), NoMatch)
