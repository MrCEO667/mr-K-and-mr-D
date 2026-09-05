"""Config precedence and the operator allowlist."""
import pytest
import yaml

from radar import config as config_module

BASE = {
    "db": {"path": "data/radar.db"},
    "alerts": {"max_per_day": 12},
    "telegram": {
        "operators": [{"id": 111, "name": "Mr K"}, {"id": 222, "name": "Mr D"}],
        "reject_unknown_users": True,
    },
    "sources": {"github": {"enabled": True}, "youtube": {"enabled": False}},
}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loads_and_reads_nested_values(tmp_path):
    cfg = config_module.load(write(tmp_path, BASE), environ={})
    assert cfg.get("alerts.max_per_day") == 12
    assert cfg.get("alerts.nothing_here", "fallback") == "fallback"


def test_env_overrides_yaml_and_keeps_the_type(tmp_path):
    cfg = config_module.load(
        write(tmp_path, BASE), environ={"RADAR__alerts__max_per_day": "3"}
    )
    assert cfg.get("alerts.max_per_day") == 3


def test_only_listed_operators_are_recognised(tmp_path):
    cfg = config_module.load(write(tmp_path, BASE), environ={})
    assert cfg.is_operator(111)
    assert cfg.operator_name(222) == "Mr D"
    # A group is joinable; anyone else must be a stranger to the bot.
    assert not cfg.is_operator(999)


def test_placeholder_operator_id_is_refused(tmp_path):
    data = {**BASE, "telegram": {"operators": [{"id": 0, "name": "Mr K"}]}}
    with pytest.raises(config_module.ConfigError, match="placeholder"):
        config_module.load(write(tmp_path, data), environ={})


def test_empty_operator_list_is_refused(tmp_path):
    data = {**BASE, "telegram": {"operators": []}}
    with pytest.raises(config_module.ConfigError):
        config_module.load(write(tmp_path, data), environ={})


def test_missing_config_file_names_the_fix(tmp_path):
    with pytest.raises(config_module.ConfigError, match="config.example.yaml"):
        config_module.load(tmp_path / "absent.yaml", environ={})


def test_enabled_source_without_its_key_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    data = {**BASE, "sources": {"youtube": {"enabled": True}}}
    cfg = config_module.load(write(tmp_path, data), environ={})
    assert cfg.missing_secrets() == {"youtube": ["YOUTUBE_API_KEY"]}


def test_env_override_survives_windows_uppercasing(tmp_path):
    # os.environ on Windows uppercases every key, so the override arrived as
    # RADAR__SOURCES__GITHUB__ENABLED and silently created a second section
    # while the real setting stayed true.
    data = {**BASE, "sources": {"github": {"enabled": True}}}
    cfg = config_module.load(
        write(tmp_path, data), environ={"RADAR__SOURCES__GITHUB__ENABLED": "false"}
    )
    assert cfg.get("sources.github.enabled") is False
    assert cfg.enabled_sources() == []


def test_env_override_still_works_in_the_original_case(tmp_path):
    data = {**BASE, "sources": {"github": {"enabled": True}}}
    cfg = config_module.load(
        write(tmp_path, data), environ={"RADAR__sources__github__enabled": "false"}
    )
    assert cfg.get("sources.github.enabled") is False
