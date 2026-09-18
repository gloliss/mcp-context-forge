/**
 * Credential redaction, mirroring `common/redaction.py` on the Python side.
 *
 * The requirement forbids passwords in source, in the repository, and in logs. The
 * TypeScript runner is a second implementation of the same policy, so it follows the
 * same two mechanisms and the same trade-off: registered secrets are masked wherever
 * they appear, with no minimum length, because there is no reliable way to tell "this
 * occurrence of the password is the real one" from an incidental match.
 */

export const MASK = '***';

// scheme://user:password@host -> scheme://user:***@host
const DSN_USERINFO = /(?<scheme>[A-Za-z][A-Za-z0-9+.-]*:\/\/)(?<user>[^:/?#@\s]*):(?<secret>[^/?#@\s]*)@/g;

// appuser@tenant1#cluster1 / appuser@tenant1 -> appuser@***
const SCOPED_USERNAME = /(?<user>[A-Za-z0-9_.$-]+)@(?<scope>[A-Za-z0-9_#$-]+)(?![.\w#$-])/g;

// Keys whose value is a credential outright and must be masked whole.
const SECRET_KEY = /(password|passwd|pwd|secret|token|credential)/i;

export class Redactor {
  /**
   * @param {Iterable<string>} [secrets] Literal secret values to mask everywhere.
   */
  constructor(secrets = []) {
    // Longest first, so a secret containing another is masked before the shorter
    // substring can split it into unmaskable fragments.
    this.secrets = [...new Set([...secrets].filter(Boolean))].sort((a, b) => b.length - a.length);
  }

  /**
   * Mask every known secret and structured credential in a string.
   *
   * @param {string} text
   * @returns {string}
   */
  redact(text) {
    if (!text) return text;
    let out = text;
    for (const secret of this.secrets) out = out.split(secret).join(MASK);
    out = out.replace(DSN_USERINFO, (_m, scheme, user) => `${scheme}${user}:${MASK}@`);
    out = out.replace(SCOPED_USERNAME, (_m, user) => `${user}@${MASK}`);
    return out;
  }

  /**
   * Recursively redact a JSON-like payload.
   *
   * @param {*} payload
   * @param {string} [key] The key this value is stored under.
   * @returns {*} A structurally identical payload with credentials removed.
   */
  redactDeep(payload, key = '') {
    if (typeof payload === 'string') return SECRET_KEY.test(key) ? MASK : this.redact(payload);
    if (Array.isArray(payload)) return payload.map((item) => this.redactDeep(item, key));
    if (payload && typeof payload === 'object') {
      return Object.fromEntries(Object.entries(payload).map(([k, v]) => [k, this.redactDeep(v, k)]));
    }
    return payload;
  }
}
