# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/secret_policy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Plaintext-secret policy for protocol/runtime configuration (PR8,
design-document §67).

§67 forbids storing plaintext ``password``/``token``/``API Key``/
``clientSecret``/``private key`` in ``protocol_config``,
``runtime_config``, ``http-service.yaml`` and ``grpc-service.yaml``.  The
gateway's own Auth/Encrypted-Secret/``metadata_env`` mechanisms are the
only sanctioned homes for credentials.

``check_no_plaintext_secrets`` walks a configuration mapping and raises when
it finds a sensitive key whose value is a non-empty plaintext string.
Values that are not strings (nested dicts/lists/numbers/bools/None) or that
carry the encryption ``v2:`` marker are treated as safe.
"""

# Standard
import re
from typing import Any, Callable, Optional

# Keys considered sensitive by design-document §67.  Matching is
# case-insensitive on the bare key (``@``-prefix and namespaces stripped).
_SENSITIVE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^password$", re.IGNORECASE),
    re.compile(r"^passwd$", re.IGNORECASE),
    re.compile(r"^token$", re.IGNORECASE),
    re.compile(r"^api[_-]?key$", re.IGNORECASE),
    re.compile(r"^apikey$", re.IGNORECASE),
    re.compile(r"^client[_-]?secret$", re.IGNORECASE),
    re.compile(r"^secret$", re.IGNORECASE),
    re.compile(r"^private[_-]?key$", re.IGNORECASE),
    re.compile(r"^authorization$", re.IGNORECASE),
    re.compile(r"^access[_-]?token$", re.IGNORECASE),
    re.compile(r"^refresh[_-]?token$", re.IGNORECASE),
    re.compile(r"^bearer$", re.IGNORECASE),
    re.compile(r"^x-api-key$", re.IGNORECASE),
)

# Recognised encryption markers (EncryptionService emits ``v2:`` bundles).
_ENCRYPTED_PREFIXES = ("v2:", "enc:", "encrypted:", "gcm:")


def _is_encrypted_value(value: Any) -> bool:
    """Return True for values that look encrypted (never plaintext)."""
    if not isinstance(value, str):
        return True  # non-string leaves are structural, not credentials
    text = value.strip()
    if not text:
        return True  # empty is not a credential
    return any(text.startswith(prefix) for prefix in _ENCRYPTED_PREFIXES)


def _sensitive_key(key: str) -> bool:
    """Return True when a config key names a sensitive field (§67)."""
    bare = key.lstrip("@").rsplit("}", 1)[-1]
    return any(pattern.match(bare) for pattern in _SENSITIVE_KEY_PATTERNS)


def find_plaintext_secret_paths(config: Any, prefix: str = "") -> list[str]:
    """Return the dotted paths of any plaintext secrets in a config.

    Args:
        config: The configuration mapping (or nested value).
        prefix: Current path prefix for recursive descent.

    Returns:
        A list of ``path.key`` strings for sensitive keys whose values are
        non-empty plaintext strings.
    """
    hits: list[str] = []
    if isinstance(config, dict):
        for key, value in config.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _sensitive_key(str(key)) and not _is_encrypted_value(value):
                hits.append(path)
            hits.extend(find_plaintext_secret_paths(value, path))
    elif isinstance(config, list):
        for index, value in enumerate(config):
            hits.extend(find_plaintext_secret_paths(value, f"{prefix}[{index}]"))
    return hits


def check_no_plaintext_secrets(
    config: Any,
    label: str = "configuration",
    *,
    is_encrypted: Optional[Callable[[Any], bool]] = None,
) -> None:
    """Raise when ``config`` contains a plaintext secret (§67).

    Args:
        config: The configuration mapping to inspect.
        label: Human-readable label for error messages.
        is_encrypted: Optional override for the "already encrypted" check;
            defaults to the ``v2:``/``enc:``-prefix heuristic.

    Raises:
        ValueError: When a sensitive key carries a plaintext value.
    """
    checker = is_encrypted or _is_encrypted_value
    hits: list[str] = []
    if isinstance(config, dict):
        for key, value in config.items():
            path = str(key)
            if _sensitive_key(path) and not checker(value):
                hits.append(path)
            hits.extend(_walk(value, path, checker))
    elif isinstance(config, list):
        for index, value in enumerate(config):
            hits.extend(_walk(value, f"[{index}]", checker))

    if hits:
        raise ValueError(f"Plaintext secret not allowed in {label}: {', '.join(sorted(hits))}")


def _walk(value: Any, prefix: str, checker: Callable[[Any], bool]) -> list[str]:
    """Recursively collect plaintext-secret paths below ``prefix``."""
    hits: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if _sensitive_key(str(key)) and not checker(item):
                hits.append(path)
            hits.extend(_walk(item, path, checker))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_walk(item, f"{prefix}[{index}]", checker))
    return hits


__all__ = ["check_no_plaintext_secrets", "find_plaintext_secret_paths"]
