import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import load_settings
from kalshi_bot.setup_cmd import normalize_pem, run_setup

from test_config import TOML


def pem(fmt=serialization.PrivateFormat.PKCS8):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode()


class Console:
    """Scripted terminal: a list of 'pastes'; each paste is a block of lines delivered at once."""

    def __init__(self, pastes):
        self.queue = []
        for p in pastes:
            self.queue.append([p] if isinstance(p, str) and "\n" not in p else p.strip().splitlines())
        self.current = []
        self.said = []

    def ask(self, prompt=""):
        if not self.current:
            if not self.queue:
                raise EOFError
            self.current = list(self.queue.pop(0))
        return self.current.pop(0)

    def pending(self):
        return bool(self.current)

    def say(self, msg):
        self.said.append(msg)


def make_root(tmp_path):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(TOML)
    return tmp_path


def test_normalize_accepts_every_paste_shape():
    p8 = pem()
    p1 = pem(serialization.PrivateFormat.TraditionalOpenSSL)
    assert normalize_pem(p8) is not None and normalize_pem(p1) is not None
    body_only = "\n".join(l for l in p1.splitlines() if not l.startswith("-----"))
    assert normalize_pem(body_only) is not None                       # header lines missing (Kalshi copy button)
    assert normalize_pem(p8.replace("-----BEGIN", "----BEGIN")) is not None  # damaged dashes
    assert normalize_pem(" ".join(p1.split())) is not None              # collapsed onto one line
    assert normalize_pem("hello world") is None
    assert normalize_pem("MIIEog" * 20) is None                         # base64-looking garbage


def test_setup_with_body_only_paste(tmp_path):
    root = make_root(tmp_path)
    key = pem(serialization.PrivateFormat.TraditionalOpenSSL)
    body_only = "\n".join(l for l in key.splitlines() if not l.startswith("-----"))
    c = Console(["13a92a24-97e1-4698-bbaa-4e9c93e39bce", body_only, "n"])
    run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    s = load_settings(root, environ={})
    assert s.api_key_id == "13a92a24-97e1-4698-bbaa-4e9c93e39bce" and s.has_credentials
    from kalshi_bot.auth import load_private_key
    load_private_key(s.private_key_path)


def test_setup_full_paste_and_file_path_and_live(tmp_path):
    root = make_root(tmp_path)
    demo_file = tmp_path / "downloaded-demo.key"
    demo_file.write_text(pem())
    live = pem()
    c = Console(["demo-id-1", str(demo_file), "y", "live-id-1", live])
    run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    s = load_settings(root, live=True, environ={"CONFIRM_LIVE": "yes"})
    assert s.api_key_id == "live-id-1" and s.has_credentials
    s.require_credentials()
    assert load_settings(root, environ={}).api_key_id == "demo-id-1"


def test_bad_paste_is_drained_then_retry_succeeds(tmp_path):
    root = make_root(tmp_path)
    good = pem()
    junk = "\n".join(["MIIEogIBAAKCAQEAjunkjunkjunkjunkjunkjunkjunkjunkjunkjunkjunkjunk"] * 3)
    c = Console(["demo-id-1", junk, "not a key at all\nsecond junk line", good, "n"])
    run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    assert any("not a complete" in m for m in c.said)          # base64-looking junk: drained, retry offered
    assert any("does not look like a key" in m for m in c.said)  # plain text: treated as a path, drained
    assert load_settings(root, environ={}).has_credentials


def test_key_id_prompt_rejects_a_pasted_key(tmp_path):
    root = make_root(tmp_path)
    good = pem()
    c = Console([good, "demo-id-1", good, "n"])  # key pasted into the id prompt by mistake, then done right
    run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    assert any("does not look like a key ID" in m for m in c.said)
    assert load_settings(root, environ={}).api_key_id == "demo-id-1"


def test_setup_keeps_existing_env_unless_confirmed(tmp_path):
    root = make_root(tmp_path)
    (root / ".env").write_text("KALSHI_API_KEY_ID=old\nALERT_WEBHOOK_URL=https://hook\n")
    c = Console(["n"])
    with pytest.raises(SystemExit):
        run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    assert "old" in (root / ".env").read_text()
    c = Console(["y", "new-id-1", pem(), "n"])
    run_setup(root, ask=c.ask, say=c.say, pending=c.pending)
    text = (root / ".env").read_text()
    assert "KALSHI_API_KEY_ID=new-id-1" in text and "ALERT_WEBHOOK_URL=https://hook" in text
