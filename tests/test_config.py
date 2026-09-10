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


def test_live_and_demo_keys_live_side_by_side(root):
    (root / "demo.pem").write_text("k")
    (root / "live.pem").write_text("k")
    env = {"KALSHI_API_KEY_ID": "demo-id", "KALSHI_PRIVATE_KEY_PATH": "./demo.pem",
           "KALSHI_LIVE_API_KEY_ID": "live-id", "KALSHI_LIVE_PRIVATE_KEY_PATH": "./live.pem", "CONFIRM_LIVE": "yes"}
    demo = config.load_settings(root, live=False, environ=dict(env))
    assert demo.api_key_id == "demo-id" and demo.private_key_path == (root / "demo.pem").resolve() and demo.has_credentials
    live = config.load_settings(root, live=True, environ=dict(env))
    assert live.api_key_id == "live-id" and live.private_key_path == (root / "live.pem").resolve() and live.has_credentials
    assert "LIVE" in live.key_source
    live.require_credentials()


def test_live_never_falls_back_to_the_demo_key(root):
    (root / "demo.pem").write_text("k")
    env = {"KALSHI_API_KEY_ID": "demo-id", "KALSHI_PRIVATE_KEY_PATH": "./demo.pem", "CONFIRM_LIVE": "yes"}
    live = config.load_settings(root, live=True, environ=env)
    assert live.api_key_id is None and not live.has_credentials
    with pytest.raises(ConfigError, match="KALSHI_LIVE_API_KEY_ID"):
        live.require_credentials()


def test_live_refuses_the_same_key_as_demo(root):
    (root / "one.pem").write_text("k")
    env = {"KALSHI_API_KEY_ID": "demo-id", "KALSHI_PRIVATE_KEY_PATH": "./one.pem",
           "KALSHI_LIVE_API_KEY_ID": "live-id", "KALSHI_LIVE_PRIVATE_KEY_PATH": "./one.pem", "CONFIRM_LIVE": "yes"}
    with pytest.raises(ConfigError, match="different private key files"):
        config.load_settings(root, live=True, environ=env).require_credentials()
    env["KALSHI_LIVE_PRIVATE_KEY_PATH"] = "./two.pem"
    (root / "two.pem").write_text("k")
    env["KALSHI_LIVE_API_KEY_ID"] = "demo-id"
    with pytest.raises(ConfigError, match="different API keys"):
        config.load_settings(root, live=True, environ=env).require_credentials()


def test_demo_specific_variables_take_precedence(root):
    (root / "d.pem").write_text("k")
    env = {"KALSHI_API_KEY_ID": "generic", "KALSHI_PRIVATE_KEY_PATH": "./missing.pem",
           "KALSHI_DEMO_API_KEY_ID": "demo-only", "KALSHI_DEMO_PRIVATE_KEY_PATH": "./d.pem"}
    s = config.load_settings(root, live=False, environ=env)
    assert s.api_key_id == "demo-only" and s.has_credentials and "DEMO" in s.key_source
