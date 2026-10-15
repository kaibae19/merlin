import pytest

from merlin.redact import redact


@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnop1234567890",
    "ghp_" + "a" * 36,
    "hf_" + "b" * 34,
    "AKIAABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "0123456789abcdef0123456789abcdef",
    "deadbeef" * 8,
])
def test_tokens_redacted(secret):
    out, n = redact(f"value: {secret} end")
    assert secret not in out and "[REDACTED]" in out and n >= 1


def test_url_credentials():
    out, _ = redact("git clone https://admin:hunter2secret@git.example.internal/repo.git")
    assert out == "git clone https://[REDACTED]@git.example.internal/repo.git"


def test_private_key_block():
    text = "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nz\nmore\n-----END OPENSSH PRIVATE KEY-----\nafter"
    out, n = redact(text)
    assert out == "before\n[REDACTED]\nafter" and n == 1


def test_key_value_and_bearer():
    out, _ = redact("DB_PASSWORD=correcthorse\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert "correcthorse" not in out and "abcdefghijklmnopqrstuvwxyz" not in out
    assert out.startswith("DB_PASSWORD=[REDACTED]")


def test_leaves_normal_text_and_placeholders():
    text = "commit 7f21beb, ip 192.0.2.10, PGPASSWORD=${PGPASSWORD}, token=<from-file>, the token scope was wrong"
    assert redact(text) == (text, 0)


def test_telegram_bot_token():
    text, n = redact("token 123456789:AAHfiqksKZ8abcdefghijklmnopqrstuvwx here")
    assert n == 1 and "AAHf" not in text and "[REDACTED]" in text


import hashlib

from merlin import redact as redaction
from merlin.layout import DEFAULT_TOML, Layout
from merlin.redact import SecretHashError, load_secret_hashes


def hash_file(tmp_path, secrets: dict[str, str], salt: str = "00ff" * 8):
    p = tmp_path / "hashes"
    p.write_text(f"salt {salt}\n" + "".join(
        f"{hashlib.sha256((salt + v).encode()).hexdigest()}\t{name}\n" for name, v in secrets.items()))
    return str(p)


def with_redact(table: str) -> Layout:
    return Layout.from_toml(DEFAULT_TOML + "\n[redact]\n" + table)


def test_secret_hashes_named(tmp_path):
    lay = with_redact(f'secret_hashes = "{hash_file(tmp_path, {"PBS_PW": "Tr0ub4dor&3", "API": "YWJjZGVmZ2g="})}"')
    text = 'pw Tr0ub4dor&3. then key=YWJjZGVmZ2g= and "Tr0ub4dor&3", not Tr0ub4dor&3x'
    out, n = redaction.redact(text, lay)
    assert out == ('pw [REDACTED:PBS_PW]. then key=[REDACTED:API] and "[REDACTED:PBS_PW]", not Tr0ub4dor&3x')
    assert n == 3


def test_secret_hashes_fail_closed(tmp_path):
    (tmp_path / "empty").write_text("salt abcd\n")
    (tmp_path / "nosalt").write_text("deadbeef\tX\n")
    for path, msg in ((tmp_path / "missing", "cannot read"), (tmp_path / "empty", "no secret hashes"),
                      (tmp_path / "nosalt", "salt <hex>")):
        load_secret_hashes.cache_clear()
        lay = with_redact(f'secret_hashes = "{path}"')
        with pytest.raises(SecretHashError, match=msg):
            redaction.check(lay)
        with pytest.raises(SecretHashError):
            redaction.redact("anything", lay)


def test_secret_hashes_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_HASHES", hash_file(tmp_path, {"K": "s3cr3t-value"}))
    assert redaction.redact("x s3cr3t-value y", Layout.from_toml(DEFAULT_TOML)) == ("x [REDACTED:K] y", 1)


def test_disable_families_and_unknown_name():
    text = "sha256 " + "ab" * 32 + " commit " + "c" * 40
    assert redaction.redact(text, with_redact('disable = ["hex32"]')) == (text, 0)
    assert redaction.redact(text, Layout.from_toml(DEFAULT_TOML))[1] == 2
    with pytest.raises(ValueError, match="unknown families"):
        redaction.check(with_redact('disable = ["hex"]'))


@pytest.mark.parametrize("text", [
    "sk-alpha-boxes-r1234 and sk-beta-docker-host and sk-gamma-ipmi-bmc",      # hostnames
    "%sysadmins ALL=(ALL) NOPASSWD: ALL_COMMANDS",                            # sudoers tag
])
def test_no_false_positives_on_names(text):
    assert redact(text) == (text, 0)


def test_real_sk_keys_still_redacted():
    for key in ("sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv", "sk-proj-aB3dE6gH9jK2mN5pQ8sT"):
        assert redact(f"key {key} x")[0] == "key [REDACTED] x"
