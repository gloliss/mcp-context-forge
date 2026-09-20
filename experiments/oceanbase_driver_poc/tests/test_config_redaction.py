"""L0: connection configuration and credential redaction.

These are the two things that must hold before any database is involved. A
configuration bug points the POC at the wrong endpoint and produces a confident wrong
verdict; a redaction bug puts a password in a file that gets committed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from common.config import ConnectionConfig, load_config, missing_required
from common.redaction import MASK, Redactor

POC_ROOT = Path(__file__).resolve().parents[1]

FULL_ENV = {
    "OB_MYSQL_HOST": "ob-proxy.example.internal",
    "OB_MYSQL_PORT": "2883",
    "OB_MYSQL_USER": "appuser@tenant1#cluster1",
    "OB_MYSQL_PASSWORD": "sup3r-s3cret",
    "OB_MYSQL_DATABASE": "tenant1",
}


def test_complete_environment_yields_config() -> None:
    """A fully populated environment produces a usable configuration."""
    config = load_config("mysql", FULL_ENV)
    assert config is not None
    assert config.host == "ob-proxy.example.internal"
    assert config.port == 2883
    assert config.password == "sup3r-s3cret"


def test_missing_required_variable_yields_none_not_an_exception() -> None:
    """An unconfigured mode is a skip, not a crash."""
    assert load_config("mysql", {}) is None
    assert load_config("mysql", {k: v for k, v in FULL_ENV.items() if k != "OB_MYSQL_PASSWORD"}) is None


def test_missing_required_is_actionable() -> None:
    """The skip names the variables the operator has to set."""
    missing = missing_required("oracle", {})
    assert missing == ["OB_ORACLE_HOST", "OB_ORACLE_PORT", "OB_ORACLE_USER", "OB_ORACLE_PASSWORD"]


def test_blank_value_counts_as_absent() -> None:
    """A variable set to whitespace is not a configured target."""
    env = dict(FULL_ENV, OB_MYSQL_HOST="   ")
    assert load_config("mysql", env) is None
    assert "OB_MYSQL_HOST" in missing_required("mysql", env)


def test_port_has_no_default() -> None:
    """Port is required: OBProxy and direct endpoints use different ports.

    A default here would silently target the wrong endpoint rather than reporting
    that the environment is incomplete.
    """
    env = {k: v for k, v in FULL_ENV.items() if k != "OB_MYSQL_PORT"}
    assert load_config("mysql", env) is None
    assert "OB_MYSQL_PORT" in missing_required("mysql", env)


def test_unknown_mode_is_rejected() -> None:
    """An unknown compatibility mode is a programming error, not a skip."""
    with pytest.raises(KeyError):
        load_config("postgres", FULL_ENV)


def test_optional_timeouts_default_but_are_overridable() -> None:
    """Timeout and pool settings fall back to documented defaults."""
    default = load_config("mysql", FULL_ENV)
    assert default is not None
    assert default.connect_timeout_s == 5.0
    assert default.query_timeout_s == 30.0

    tuned = load_config("mysql", dict(FULL_ENV, OB_MYSQL_QUERY_TIMEOUT_S="12.5", OB_MYSQL_POOL_MAX="9"))
    assert tuned is not None
    assert tuned.query_timeout_s == 12.5
    assert tuned.pool_max == 9


def test_malformed_number_falls_back_to_default() -> None:
    """A malformed optional number does not abort the run."""
    config = load_config("mysql", dict(FULL_ENV, OB_MYSQL_CONNECT_TIMEOUT_S="soon"))
    assert config is not None
    assert config.connect_timeout_s == 5.0


class TestRedaction:
    """The redactor is the single choke point for every output surface."""

    def test_password_is_masked_in_free_text(self) -> None:
        """A registered secret is masked wherever it appears."""
        redactor = Redactor(["sup3r-s3cret"])
        assert redactor.redact("connect failed: sup3r-s3cret is wrong") == f"connect failed: {MASK} is wrong"

    def test_password_is_masked_inside_a_dsn(self) -> None:
        """DSN userinfo is masked even when the password was never registered."""
        redactor = Redactor()
        redacted = redactor.redact("mysql+pymysql://appuser:topsecret@ob-proxy.example.internal:2883/tenant1")
        assert "topsecret" not in redacted
        assert redacted == f"mysql+pymysql://appuser:{MASK}@ob-proxy.example.internal:2883/tenant1"

    def test_username_scope_is_stripped_but_account_kept(self) -> None:
        """Tenant and cluster names do not travel; the account name does."""
        redactor = Redactor()
        assert redactor.redact("appuser@tenant1#cluster1") == f"appuser@{MASK}"
        assert redactor.redact("appuser@tenant1") == f"appuser@{MASK}"

    def test_email_addresses_are_left_alone(self) -> None:
        """The scope rule must not mangle an address into '***.com'."""
        redactor = Redactor()
        assert redactor.redact("contact admin@example.com") == "contact admin@example.com"

    def test_credential_keyed_values_are_masked_whole(self) -> None:
        """A credential under a credential-named key is masked regardless of shape."""
        redactor = Redactor()
        payload = redactor.redact_deep({"password": "not-password-shaped", "host": "ob-proxy.example.internal"})
        assert payload["password"] == MASK
        assert payload["host"] == "ob-proxy.example.internal"

    def test_empty_and_special_character_passwords(self) -> None:
        """Blank secrets are ignored; secrets full of regex metacharacters still mask."""
        assert Redactor([""]).redact("nothing to mask") == "nothing to mask"
        tricky = "a.*[](){}|\\^$+?b"
        assert Redactor([tricky]).redact(f"dsn={tricky}") == f"dsn={MASK}"

    def test_overlapping_secrets_mask_longest_first(self) -> None:
        """A password containing another password does not fragment into leftovers."""
        redactor = Redactor(["secret", "secret-with-suffix"])
        assert redactor.redact("secret-with-suffix") == MASK

    def test_short_secret_over_masks_and_that_is_deliberate(self) -> None:
        """Masking is not length-gated; a mangled word beats a leaked password.

        This is a decision, not an accident: with a one-character password the
        report mangles unrelated words, and that is accepted rather than solved by
        weakening redaction.
        """
        redactor = Redactor(["p"])
        assert redactor.redact("pymysql") == f"{MASK}ymysql"
        assert redactor.redact("p") == MASK

    def test_config_view_never_carries_the_password(self) -> None:
        """The serialisable connection view is safe to write to a result file."""
        config = ConnectionConfig(
            mode="oracle",
            host="ob-proxy.example.internal",
            port=2883,
            user="appuser@tenant1#cluster1",
            password="sup3r-s3cret",
            service_name="svc1",
        )
        view = config.redacted(Redactor([config.password]))
        assert view["password"] == MASK
        assert view["user"] == f"appuser@{MASK}"
        assert "sup3r-s3cret" not in repr(view)
        assert "tenant1" not in repr(view)


def test_env_example_holds_only_placeholders() -> None:
    """Every assignment in the committed sample is a placeholder, not a value.

    Checking for specific known identifiers would only prove those particular
    strings are absent; what has to hold is that no assignment carries a real value
    of any kind.
    """
    lines = (POC_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [line.strip() for line in lines if "=" in line and not line.strip().startswith("#")]
    assert assignments, "样例文件应当包含可复制的变量名"

    for assignment in assignments:
        name, _, value = assignment.partition("=")
        assert value == "__FILL_ME__", f"{name} 的值不是占位符：{value!r}"
