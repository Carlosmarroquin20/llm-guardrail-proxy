"""Synthetic, labelled corpus for the secret scanner.

The corpus is *generated* from a fixed seed rather than stored as data.
Two reasons:

* Committing hundreds of credential-shaped strings would trip GitHub push
  protection and this repository's own self-scan, and would teach readers
  to ignore both.
* Generation keeps every sample tied to the issuer's published format, so
  the corpus can be audited by reading the generators instead of eyeballing
  opaque fixtures.

Positives follow each issuer's documented shape (prefix, alphabet, length)
and are embedded in the places credentials actually show up in prompts:
env files, JSON, YAML, URLs, headers, code and prose.

Negatives are *hard* lookalikes — strings a developer legitimately pastes
into a prompt that share a prefix, alphabet or length with a credential
(hashes, UUIDs, public keys, publishable keys, IDs, mentions of a prefix in
prose). Obvious placeholders such as ``AKIAXXXXXXXXXXXXXXXX`` are kept in a
separate category because whether blocking them is a false positive is a
policy question, not a detection one.

What this corpus does *not* measure: real-world prevalence of each format,
secrets with no distinctive surface format (passwords, AWS secret access
keys, generic hex API keys), and adversarial obfuscation. The numbers it
produces describe conformance to known formats and resistance to common
lookalikes, nothing more.
"""

from __future__ import annotations

import base64
import json
import random
import string
from collections.abc import Callable
from dataclasses import dataclass

SEED = 20_261_103

_ALNUM = string.ascii_letters + string.digits
_B64URL = _ALNUM + "-_"
_B32 = string.ascii_uppercase + "234567"
_HEX = "0123456789abcdef"


@dataclass(frozen=True, slots=True)
class Sample:
    """One labelled prompt.

    ``expected`` is the set of scanner kinds that *should* fire. An empty set
    marks a negative. ``category`` groups samples for reporting: the variant
    name for positives (e.g. ``openai_project``), the lookalike family for
    negatives.
    """

    text: str
    expected: frozenset[str]
    category: str


def _pick(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _b64url_json(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ------------------------------------------------------------ positive shapes
#
# Each generator returns one credential in its issuer's documented format.
# Keys are ``(scanner kind, variant name)``.

_Gen = Callable[[random.Random], str]


def _jwt(rng: random.Random) -> str:
    header = _b64url_json({"alg": rng.choice(["HS256", "RS256"]), "typ": "JWT"})
    payload = _b64url_json(
        {"sub": str(rng.randint(10_000, 99_999)), "iat": rng.randint(1_600_000_000, 1_800_000_000)}
    )
    return f"{header}.{payload}.{_pick(rng, _B64URL, 43)}"


def _pem(kind: str) -> _Gen:
    def gen(rng: random.Random) -> str:
        label = f"{kind} PRIVATE KEY" if kind else "PRIVATE KEY"
        body = "\n".join(_pick(rng, _ALNUM + "+/", 64) for _ in range(4))
        return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"

    return gen


POSITIVE_GENERATORS: dict[tuple[str, str], _Gen] = {
    # AWS access key IDs: 4-char prefix + 16 base32 chars. ``AKIA`` is a
    # long-term key, ``ASIA`` a temporary STS key; both authenticate.
    ("aws_access_key_id", "aws_long_term"): lambda r: "AKIA" + _pick(r, _B32, 16),
    ("aws_access_key_id", "aws_temporary"): lambda r: "ASIA" + _pick(r, _B32, 16),
    # GitHub: ``ghp_`` classic PAT; ``gho_``/``ghu_``/``ghs_``/``ghr_`` are
    # OAuth, user-to-server, server-to-server and refresh tokens.
    ("github_pat_classic", "github_classic"): lambda r: "ghp_" + _pick(r, _ALNUM, 36),
    ("github_pat_fine_grained", "github_fine_grained"): (
        lambda r: "github_pat_" + _pick(r, _ALNUM, 22) + "_" + _pick(r, _ALNUM, 59)
    ),
    ("github_oauth", "github_oauth"): lambda r: "gho_" + _pick(r, _ALNUM, 36),
    ("github_oauth", "github_user_to_server"): lambda r: "ghu_" + _pick(r, _ALNUM, 36),
    ("github_oauth", "github_server_to_server"): lambda r: "ghs_" + _pick(r, _ALNUM, 36),
    ("github_oauth", "github_refresh"): lambda r: "ghr_" + _pick(r, _ALNUM, 36),
    # OpenAI: legacy user keys are ``sk-`` + alphanumerics around the
    # ``T3BlbkFJ`` marker; project, service-account and admin keys use a
    # scoped prefix and a base64url body that includes ``-`` and ``_``.
    ("openai_api_key", "openai_legacy"): (
        lambda r: "sk-" + _pick(r, _ALNUM, 20) + "T3BlbkFJ" + _pick(r, _ALNUM, 20)
    ),
    ("openai_api_key", "openai_project"): (
        lambda r: "sk-proj-" + _pick(r, _B64URL, 74) + "T3BlbkFJ" + _pick(r, _B64URL, 74)
    ),
    ("openai_api_key", "openai_service_account"): (
        lambda r: "sk-svcacct-" + _pick(r, _B64URL, 74) + "T3BlbkFJ" + _pick(r, _B64URL, 74)
    ),
    ("openai_api_key", "openai_admin"): (
        lambda r: "sk-admin-" + _pick(r, _B64URL, 74) + "T3BlbkFJ" + _pick(r, _B64URL, 74)
    ),
    # Anthropic: ``sk-ant-api03-`` (API) or ``sk-ant-admin01-`` (admin), a
    # base64url body, and a trailing ``AA``.
    ("anthropic_api_key", "anthropic_api"): (
        lambda r: "sk-ant-api03-" + _pick(r, _B64URL, 93) + "AA"
    ),
    ("anthropic_api_key", "anthropic_admin"): (
        lambda r: "sk-ant-admin01-" + _pick(r, _B64URL, 93) + "AA"
    ),
    # Slack bot / user tokens and app-level tokens.
    ("slack_token", "slack_bot"): (
        lambda r: f"xoxb-{_pick(r, string.digits, 11)}-{_pick(r, string.digits, 13)}-"
        + _pick(r, _ALNUM, 24)
    ),
    ("slack_token", "slack_user"): (
        lambda r: f"xoxp-{_pick(r, string.digits, 11)}-{_pick(r, string.digits, 11)}-"
        f"{_pick(r, string.digits, 13)}-{_pick(r, _HEX, 32)}"
    ),
    ("slack_token", "slack_app"): (
        lambda r: f"xapp-1-A{_pick(r, string.ascii_uppercase + string.digits, 10)}-"
        f"{_pick(r, string.digits, 13)}-{_pick(r, _HEX, 64)}"
    ),
    # Stripe secret keys (live and test) and restricted keys.
    ("stripe_secret_key", "stripe_live"): lambda r: "sk_live_" + _pick(r, _ALNUM, 99),
    ("stripe_secret_key", "stripe_test"): lambda r: "sk_test_" + _pick(r, _ALNUM, 99),
    ("stripe_secret_key", "stripe_restricted"): lambda r: "rk_live_" + _pick(r, _ALNUM, 99),
    ("google_api_key", "google_api"): lambda r: "AIza" + _pick(r, _B64URL, 35),
    ("jwt", "jwt"): _jwt,
    ("pem_private_key", "pem_pkcs8"): _pem(""),
    ("pem_private_key", "pem_rsa"): _pem("RSA"),
    ("pem_private_key", "pem_ec"): _pem("EC"),
    ("pem_private_key", "pem_openssh"): _pem("OPENSSH"),
    ("pem_private_key", "pem_encrypted"): _pem("ENCRYPTED"),
}

# Where credentials end up in real prompts. ``{s}`` is the credential.
CONTEXTS: tuple[str, ...] = (
    "{s}",
    "My .env has API_KEY={s} and the request still fails, why?",
    'export TOKEN="{s}"',
    'Fix this JSON: {{"api_key": "{s}", "timeout": 30}}',
    "config:\n  service:\n    token: {s}\n    retries: 3",
    "git clone https://x-access-token:{s}@github.com/acme/repo.git fails with 403",
    "curl -H 'Authorization: Bearer {s}' https://api.example.com/v1/items",
    "client = Client(api_key='{s}')\nprint(client.list())",
    "Here is the key ({s}). Can you tell me which service it belongs to?",
    "```\n{s}\n```\nWhat format is this?",
    "Error log:\n2026-11-03T10:00:00Z auth failed for credential {s};",
)


# ---------------------------------------------------------- negative shapes


def _uuid(rng: random.Random) -> str:
    h = _pick(rng, _HEX, 32)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:]}"


NEGATIVE_GENERATORS: dict[str, _Gen] = {
    # Identifiers and hashes with credential-like alphabets / lengths.
    "uuid": _uuid,
    "git_sha": lambda r: _pick(r, _HEX, 40),
    "sha256": lambda r: _pick(r, _HEX, 64),
    "base64_blob": lambda r: base64.b64encode(r.randbytes(48)).decode(),
    "npm_integrity": lambda r: "sha512-" + base64.b64encode(r.randbytes(64)).decode(),
    "docker_digest": lambda r: "sha256:" + _pick(r, _HEX, 64),
    "aws_arn": lambda r: (
        f"arn:aws:iam::{_pick(r, string.digits, 12)}:role/"
        + r.choice(["deploy", "ci-runner", "lambda-exec"])
    ),
    "upper_id_20": lambda r: r.choice(["AKIB", "AKID", "AIDA", "AROA"]) + _pick(r, _B32, 16),
    # Public, non-secret material that shares a prefix or framing.
    "stripe_publishable": lambda r: "pk_live_" + _pick(r, _ALNUM, 99),
    "stripe_object_id": lambda r: r.choice(["pi_", "cus_", "ch_", "price_"]) + _pick(r, _ALNUM, 24),
    "pem_public_key": lambda r: (
        "-----BEGIN PUBLIC KEY-----\n"
        + _pick(r, _ALNUM + "+/", 64)
        + "\n-----END PUBLIC KEY-----"
    ),
    "pem_certificate": lambda r: (
        "-----BEGIN CERTIFICATE-----\n"
        + _pick(r, _ALNUM + "+/", 64)
        + "\n-----END CERTIFICATE-----"
    ),
    "ssh_public_key": lambda r: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA" + _pick(r, _ALNUM, 43),
    "slack_channel_id": lambda r: "C" + _pick(r, string.ascii_uppercase + string.digits, 10),
    # Prefixes mentioned in prose, short ids and slugs.
    "prefix_mention": lambda r: r.choice(
        [
            "AWS access keys start with AKIA, temporary ones with ASIA.",
            "GitHub classic tokens use the ghp_ prefix; fine-grained ones use github_pat_.",
            "OpenAI project keys begin with sk-proj- and Anthropic keys with sk-ant-.",
            "Slack bot tokens look like xoxb-..., Stripe secret keys like sk_live_...",
            "A JWT starts with eyJ because that is base64 for an opening brace.",
        ]
    ),
    "sk_slug": lambda r: r.choice(
        ["sk-learn", "sk-1234", "sk-proj-dashboard", "sk-ant-colony-sim", "task-sk-runner-v2"]
    )
    + f" v{r.randint(1, 9)}.{r.randint(0, 20)}",
    # Long lower-case slugs behind a scoped OpenAI prefix: these satisfy a
    # pure length rule and must be rejected by the mixed-case/digit guard.
    "sk_scoped_slug": lambda r: r.choice(["sk-proj-", "sk-admin-", "sk-svcacct-"])
    + "-".join(
        r.choice(["dashboard", "service", "account", "rotation", "billing", "export", "team"])
        for _ in range(r.randint(5, 8))
    ),
    "kebab_identifier": lambda r: "-".join(
        r.choice(["service", "account", "key", "rotation", "token", "proj", "admin", "sk"])
        for _ in range(r.randint(4, 7))
    ),
    "url_with_query": lambda r: (
        f"https://example.com/search?q={_pick(r, string.ascii_lowercase, 8)}"
        f"&session={_pick(r, _HEX, 32)}"
    ),
}

# Obvious placeholders: credential-shaped but clearly not real.
PLACEHOLDER_GENERATORS: dict[str, _Gen] = {
    "placeholder_x": lambda r: r.choice(
        ["AKIA" + "X" * 16, "ghp_" + "x" * 36, "sk-" + "x" * 48, "sk_live_" + "x" * 24]
    ),
    "placeholder_example": lambda r: r.choice(
        ["AKIAIOSFODNN7EXAMPLE", "sk-...", "<YOUR_API_KEY>", "ghp_<token>", "$OPENAI_API_KEY"]
    ),
}

NEGATIVE_CONTEXTS: tuple[str, ...] = (
    "{s}",
    "Can you explain what {s} refers to in this log line?",
    'Fix this JSON: {{"id": "{s}", "status": "ok"}}',
    "The build output printed {s} and then exited 0.",
    "```\n{s}\n```\nIs this safe to share publicly?",
)


def build_corpus(
    seed: int = SEED,
    positives_per_variant: int = 40,
    negatives_per_family: int = 40,
) -> tuple[Sample, ...]:
    """Return the full labelled corpus, deterministic for a given seed."""

    rng = random.Random(seed)
    samples: list[Sample] = []

    for (kind, variant), gen in POSITIVE_GENERATORS.items():
        for _ in range(positives_per_variant):
            text = rng.choice(CONTEXTS).format(s=gen(rng))
            samples.append(Sample(text, frozenset({kind}), variant))

    for family, gen in NEGATIVE_GENERATORS.items():
        for _ in range(negatives_per_family):
            text = rng.choice(NEGATIVE_CONTEXTS).format(s=gen(rng))
            samples.append(Sample(text, frozenset(), family))

    for family, gen in PLACEHOLDER_GENERATORS.items():
        for _ in range(negatives_per_family):
            text = rng.choice(NEGATIVE_CONTEXTS).format(s=gen(rng))
            samples.append(Sample(text, frozenset(), family))

    return tuple(samples)
