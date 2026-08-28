import pytest

from frameio_export_watcher.config import ConfigError, load_config

MINIMAL = """
watch:
  root: /data/AktiveProjekter
  export_template: "{year}/{client}/{case}/Projektfiler/Eksport"
frameio:
  project_template: "{year}"
  folder_template: "{client}/{case}"
"""


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_minimal_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEIO_CLIENT_ID", "id")
    monkeypatch.setenv("FRAMEIO_CLIENT_SECRET", "secret")
    config = load_config(write(tmp_path, MINIMAL))
    assert config.watch.export_template.fields == ("year", "client", "case")
    assert config.frameio.folder_template.render(
        {"year": "2026", "client": "B", "case": "K"}
    ) == ("B", "K")
    assert config.auth.mode == "ims"
    assert config.frameio.version_stack_on_duplicate is True


def test_ims_mode_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("FRAMEIO_CLIENT_ID", raising=False)
    monkeypatch.delenv("FRAMEIO_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="FRAMEIO_CLIENT_ID"):
        load_config(write(tmp_path, MINIMAL))


def test_secrets_can_come_from_files(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("s3cr3t\n", encoding="utf-8")
    monkeypatch.setenv("FRAMEIO_CLIENT_ID", "id")
    monkeypatch.delenv("FRAMEIO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("FRAMEIO_CLIENT_SECRET_FILE", str(secret))
    config = load_config(write(tmp_path, MINIMAL))
    assert config.auth.client_secret == "s3cr3t"


def test_legacy_mode_requires_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FRAMEIO_LEGACY_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="FRAMEIO_LEGACY_TOKEN"):
        load_config(write(tmp_path, MINIMAL + "\nauth:\n  mode: legacy\n"))


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEIO_CLIENT_ID", "id")
    monkeypatch.setenv("FRAMEIO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("WATCH_ROOT", "/mnt/other")
    monkeypatch.setenv("FRAMEIO_ACCOUNT_ID", "acc-9")
    monkeypatch.setenv("DRY_RUN", "true")
    config = load_config(write(tmp_path, MINIMAL))
    assert str(config.watch.root) == "/mnt/other"
    assert config.frameio.account_id == "acc-9"
    assert config.dry_run is True


def test_missing_required_keys_are_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEIO_CLIENT_ID", "id")
    monkeypatch.setenv("FRAMEIO_CLIENT_SECRET", "secret")
    monkeypatch.delenv("WATCH_ROOT", raising=False)
    with pytest.raises(ConfigError, match="watch.root"):
        load_config(write(tmp_path, "frameio:\n  project_template: '{year}'\n"))


def test_the_shipped_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("FRAMEIO_CLIENT_ID", "id")
    monkeypatch.setenv("FRAMEIO_CLIENT_SECRET", "secret")
    monkeypatch.delenv("WATCH_ROOT", raising=False)
    monkeypatch.delenv("FRAMEIO_ACCOUNT_ID", raising=False)
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert str(config.watch.root) == "/data/AktiveProjekter"
    assert config.frameio.project_template.raw == "{year}"
