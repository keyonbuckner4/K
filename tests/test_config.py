import pytest

from kalshi_bot import config
from kalshi_bot.errors import ConfigError

TOML = """
[hosts.demo]
rest = "https://demo.example/trade-api/v2"
ws = "wss://demo.example/trade-api/ws/v2"
[hosts.live]
rest = "https://live.example/trade-api/v2"
ws = "wss://live.example/trade-api/ws/v2"
[storage]
db_path = "data/bot.db"
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(TOML)
    return tmp_path


def test_default_is_demo(root):
    s = config.load_settings(root, live=False, environ={})
    assert s.env == "demo"
    assert not s.is_live
    assert s.rest_base_url == "https://demo.example/trade-api/v2"
    assert s.db_path.name == "bot.demo.db"
    assert s.halt_path == root / "HALT"


def test_live_requires_flag_and_confirm(root):
    with pytest.raises(ConfigError):
        config.load_settings(root, live=True, environ={"CONFIRM_LIVE": "no"})
    with pytest.raises(ConfigError):
        config.load_settings(root, live=True, environ={})
    # CONFIRM_LIVE alone does not activate live
    s = config.load_settings(root, live=False, environ={"CONFIRM_LIVE": "yes"})
    assert s.env == "demo"
    s = config.load_settings(root, live=True, environ={"CONFIRM_LIVE": "yes"})
    assert s.env == "live"
    assert s.rest_base_url.startswith("https://live.example")
    assert s.db_path.name == "bot.live.db"


def test_dotenv_loaded_without_overriding_process_env(root):
    (root / ".env").write_text("KALSHI_API_KEY_ID=from-dotenv\nKALSHI_PRIVATE_KEY_PATH=./k.pem\n# comment\nCONFIRM_LIVE='no'\n")
    env = {"KALSHI_API_KEY_ID": "from-process"}
    s = config.load_settings(root, environ=env)
    assert s.api_key_id == "from-process"
    assert s.private_key_path == (root / "k.pem").resolve()
    assert not s.has_credentials
    with pytest.raises(ConfigError):
        s.require_credentials()


def test_missing_hosts_is_an_error(root):
    (root / "config" / "bot.toml").write_text("[storage]\ndb_path='x.db'\n")
    with pytest.raises(ConfigError):
        config.load_settings(root, environ={})


def test_insecure_scheme_rejected(root):
    (root / "config" / "bot.toml").write_text(TOML.replace("https://demo.example", "http://demo.example"))
    with pytest.raises(ConfigError):
        config.load_settings(root, environ={})


def test_parse_dotenv_quotes_and_comments():
    d = config.parse_dotenv('A=1\nB="two words"\nC=\'x\'\nexport D=4 # trailing\n\n#E=5\nF\n')
    assert d == {"A": "1", "B": "two words", "C": "x", "D": "4"}
