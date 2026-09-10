import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import load_settings
from kalshi_bot.setup_cmd import run_setup

from test_config import TOML


def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def scripted(answers):
    it = iter(answers)

    def ask(prompt=""):
        return next(it)

    return ask


def make_root(tmp_path):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(TOML)
    return tmp_path


def test_setup_pasted_demo_key_only(tmp_path):
    root = make_root(tmp_path)
    demo = pem()
    answers = ["demo-id-123", *demo.strip().splitlines(), "n"]
    said = []
    run_setup(root, ask=scripted(answers), say=said.append)
    assert (root / "kalshi-demo.pem").read_text().strip() == demo.strip()
    s = load_settings(root, environ={})
    assert s.api_key_id == "demo-id-123" and s.has_credentials and not s.is_live
    live = load_settings(root, live=True, environ={"CONFIRM_LIVE": "yes"})
    assert not live.has_credentials  # production key not configured yet


def test_setup_with_key_file_path_and_live(tmp_path):
    root = make_root(tmp_path)
    demo_file = tmp_path / "downloaded-demo.key"
    demo_file.write_text(pem())
    live = pem()
    answers = ["demo-id", str(demo_file), "y", "live-id", *live.strip().splitlines()]
    run_setup(root, ask=scripted(answers), say=lambda _: None)
    s = load_settings(root, live=True, environ={"CONFIRM_LIVE": "yes"})
    assert s.api_key_id == "live-id" and s.private_key_path == (root / "kalshi-live.pem").resolve() and s.has_credentials
    s.require_credentials()
    assert load_settings(root, environ={}).api_key_id == "demo-id"


def test_setup_rejects_bad_key_then_accepts(tmp_path):
    root = make_root(tmp_path)
    good = pem()
    answers = ["demo-id", "-----BEGIN PRIVATE KEY-----", "notakey", "-----END PRIVATE KEY-----", *good.strip().splitlines(), "n"]
    said = []
    run_setup(root, ask=scripted(answers), say=said.append)
    assert any("not a valid" in m for m in said)
    assert load_settings(root, environ={}).has_credentials


def test_setup_keeps_existing_env_unless_confirmed(tmp_path):
    root = make_root(tmp_path)
    (root / ".env").write_text("KALSHI_API_KEY_ID=old\nALERT_WEBHOOK_URL=https://hook\n")
    with pytest.raises(SystemExit):
        run_setup(root, ask=scripted(["n"]), say=lambda _: None)
    assert "old" in (root / ".env").read_text()
    answers = ["y", "new-id", *pem().strip().splitlines(), "n"]
    run_setup(root, ask=scripted(answers), say=lambda _: None)
    text = (root / ".env").read_text()
    assert "KALSHI_API_KEY_ID=new-id" in text and "ALERT_WEBHOOK_URL=https://hook" in text
