"""Credential redaction for every POC output surface.

The requirement forbids passwords in source, in the repository, and in logs, and
forbids printing full passwords or DSNs. This module is the single choke point that
every output surface is expected to pass through: stdout, stderr, log records,
exception messages, result JSON, and the generated conclusions document.

Two mechanisms are combined, because either alone leaks:

* a registry of known secret values (passwords loaded from the environment) masked
  wherever they appear, whatever the surrounding text looks like;
* pattern rules for structured leaks that exist independently of the registry --
  DSN userinfo, and OceanBase's ``user@tenant#cluster`` username form.

The username rule keeps the part before ``@`` because that identifies the account
being tested; the tenant and cluster names are what must not travel.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"

# scheme://user:password@host -> scheme://user:***@host
_DSN_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/?#@\s]*):(?P<secret>[^/?#@\s]*)@")

# appuser@tenant1#cluster1 / appuser@tenant1 -> appuser@***
# The scope class deliberately excludes '.', and the trailing lookahead stops a
# partial match from mangling an ordinary e-mail address into "***.com".
_SCOPED_USERNAME = re.compile(r"(?P<user>[A-Za-z0-9_.$\-]+)@(?P<scope>[A-Za-z0-9_#$\-]+)(?![.\w#$\-])")

# Keys whose value is a credential outright and must be masked whole.
_SECRET_KEY = re.compile(r"(password|passwd|pwd|secret|token|credential)", re.IGNORECASE)


class Redactor:
    """Masks credentials in text and in JSON-like payloads.

    A redactor is constructed with the literal secret values in play and is then
    passed explicitly to anything that may produce output. There is deliberately no
    module-level singleton: an implicit global would make it possible to emit
    output from a code path nobody remembered to wire up.

    Registered secrets are masked wherever they appear, with no minimum length and no
    attempt to tell a credential from ordinary prose. A degenerate one-character
    password therefore mangles unrelated words in the report ("pymysql" becomes
    "***ymysql"). That is intentional: a mangled word is a cosmetic defect, while a
    password in a committed result file is a security failure, and there is no
    reliable way to distinguish "this occurrence of the password is the real one".
    Use a realistic credential; the masking is not the thing to weaken.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """Build a redactor holding the secret values to mask everywhere.

        Args:
            secrets: Literal secret values, typically passwords read from the
                environment. Blank values are dropped so an unset variable cannot
                turn the mask into a substring match on every string.
        """
        # Longest first, so a secret that contains another is masked before the
        # shorter substring can split it into unmaskable fragments.
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def redact(self, text: str) -> str:
        """Mask every known secret and structured credential in ``text``.

        Args:
            text: Arbitrary text that may carry a credential.

        Returns:
            The text with secrets, DSN passwords, and username scopes masked.
        """
        if not text:
            return text
        result = text
        for secret in self._secrets:
            result = result.replace(secret, MASK)
        result = _DSN_USERINFO.sub(lambda m: f"{m['scheme']}{m['user']}:{MASK}@", result)
        return _SCOPED_USERNAME.sub(lambda m: f"{m['user']}@{MASK}", result)

    def redact_deep(self, payload: Any, *, key: str = "") -> Any:
        """Recursively redact a JSON-like payload.

        Values stored under a credential-named key are masked whole rather than
        pattern-matched, so a password cannot survive merely by looking unlike a
        password.

        Args:
            payload: Any JSON-like value (mapping, sequence, scalar).
            key: The key this value is stored under, used for the whole-value mask.

        Returns:
            A structurally identical payload with credentials removed.
        """
        if isinstance(payload, str):
            return MASK if _SECRET_KEY.search(key) else self.redact(payload)
        if isinstance(payload, Mapping):
            return {str(k): self.redact_deep(v, key=str(k)) for k, v in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self.redact_deep(item, key=key) for item in payload]
        return payload
