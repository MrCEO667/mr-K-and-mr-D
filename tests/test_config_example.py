"""config.example.yaml is what a new checkout copies; it must stay valid."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load():
    return yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))


def test_example_config_parses():
    assert load()["db"]["path"]


def test_both_operators_are_declared():
    telegram = load()["telegram"]
    assert len(telegram["operators"]) == 2
    assert all("id" in op and "name" in op for op in telegram["operators"])


def test_unknown_users_are_rejected_by_default():
    assert load()["telegram"]["reject_unknown_users"] is True


def test_env_example_declares_the_telegram_keys():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=" in env
    assert "TELEGRAM_CHAT_ID=" in env
