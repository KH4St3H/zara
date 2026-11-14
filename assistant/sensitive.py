"""Redact things that look like secrets from Mattermost text before it leaves for Telegram or Claude.

redact(text) -> (clean_text, n_redacted)

Catches:
- known credential formats (private keys, JWTs, cloud/GitHub/GitLab/Slack tokens, URL user:password@)
- key=value / key: value where the key says password/token/secret/api key
- random-looking strings: mixed letters/digits/symbols, long base64-ish blobs, and letter
  "gibberish" that doesn't read like any language used in your chats (letter-pair model trained on
  an English word list + words seen in your own Mattermost, see build_local_model()).
Ids that are random by design but not secret (UUIDs, hex hashes, k8s pod suffixes, Mattermost ids,
URLs, paths, versions, IPs) are left alone.
"""
import json, math, os, re, string

MASK = "‹redacted›"
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_MODEL = os.path.join(os.environ.get("STATE_DIRECTORY", HERE), "bigram_local.json")
GIBBERISH_TH = float(os.environ.get("SENSITIVE_GIBBERISH_TH", "-3.3"))   # mean log-prob per letter pair
MIN_GIBBERISH_LEN = 8

_A = "^" + string.ascii_lowercase + "$"


def _load_model():
    """-> (letter-pair log-probs, set of known words)."""
    cnt = json.load(open(os.path.join(HERE, "bigram_base.json")))
    vocab = set(open(os.path.join(HERE, "tech_words.txt")).read().lower().split())   # jargon a dictionary lacks
    try:   # words from the owner's own Mattermost (jargon, Finglish, names) — built on the device
        local = json.load(open(LOCAL_MODEL))
        for k, v in local["pairs"].items():
            cnt[k] = cnt.get(k, 0) + v
        vocab |= set(local["words"])
    except (OSError, ValueError, KeyError):
        pass
    for w in vocab:
        s = "^" + w + "$"
        for i in range(len(s) - 1):
            cnt[s[i:i + 2]] = cnt.get(s[i:i + 2], 0) + 5
    tot = {a: sum(cnt.get(a + b, 0) + 1 for b in _A) for a in _A}
    return {a + b: math.log((cnt.get(a + b, 0) + 1) / tot[a]) for a in _A for b in _A}, vocab


_LP, VOCAB = _load_model()


def letter_score(word):
    s = "^" + word.lower() + "$"
    return sum(_LP[s[i:i + 2]] for i in range(len(s) - 1)) / (len(s) - 1)


# ---------- known formats ----------
KNOWN = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),           # JWT
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),                                          # AWS key id
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),        # GitHub
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),                                           # GitLab
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),                                       # Slack
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                                              # OpenAI/Anthropic style
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),                                             # Google API key
    re.compile(r"\b(?:hvs|hvb|s)\.[A-Za-z0-9]{20,}\b"),                                    # Vault/OpenBao token
]
# secret in a URL: scheme://user:PASSWORD@host
URL_PASS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.I)
# password=..., "token": "...", api_key: ..., Authorization: Bearer ...
KEYWORDS = (r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
            r"client[_-]?secret|credentials?|auth|bearer|رمز|پسورد")
KV = re.compile(r"""(?ix)(\b(?:[a-z0-9]+[_-])?(?:%s)(?:[_-][a-z0-9]+)?\b["']?\s*(?:[:=]|\bis\b)\s*["']?)([^\s"',;]{4,})""" % KEYWORDS)
QUERY = re.compile(r"(?i)([?&](?:token|access_token|id_token|key|api_key|apikey|sig|signature|secret|"
                   r"password|pass|code|x-amz-signature|x-amz-credential|x-amz-security-token)=)([^&\s)\]]+)")
BEARER = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{12,})")

# ---------- harmless random-looking things ----------
SAFE = [
    re.compile(r"^[a-z][a-z0-9+.-]*://", re.I),                                   # URL (secrets in it handled above)
    re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I),                             # email
    re.compile(r"^[~.]?/"),                                                        # path
    re.compile(r"^[0-9a-f]{7,64}$", re.I),                                         # hex hash / digest
    re.compile(r"^(sha256:)?[0-9a-f]{32,}$", re.I),
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),  # UUID
    re.compile(r"^v?\d+(\.\d+)+[-+~.\w]*$"),                                       # version (1.2.3-1ubuntu1~22.04)
    re.compile(r"^@[\w.-]+$"),                                                     # @mention
    re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?(/\d+)?$"),                          # IP
    re.compile(r"^[a-z0-9]{26}$"),                                                 # Mattermost id
]
NAME = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")      # k8s/DNS names: only the gibberish check applies
SPECIAL = set("!@#$%^&*+=?~|<>\\")
STRIP = "\"'`,;()[]{}<>*_"


def _classes(t):
    return (any(c.islower() for c in t), any(c.isupper() for c in t), any(c.isdigit() for c in t),
            any(c in SPECIAL for c in t))


def _wordlike(t):
    """Share of the token made of word-like pieces (CamelCase words, snake_case parts): high for
    identifiers such as KubeDeploymentReplicasMismatch or $amount.Value, ~0 for random strings."""
    segs = re.findall(r"[A-Z]?[a-z]{2,}|[A-Z]{2,}(?![a-z])", t)
    ok = sum(len(x) for x in segs if len(x) >= 3 and (x.lower() in VOCAB or letter_score(x) >= GIBBERISH_TH))
    return ok / len(t)


def _letter_runs(t):
    # split on non-letters and camelCase boundaries; ALLCAPS runs are scored as words too
    for run in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", t):
        if len(run) >= MIN_GIBBERISH_LEN:
            yield run


def why_sensitive(tok):
    """Reason string if a single whitespace-separated token looks like a secret, else None."""
    t = tok.strip(STRIP).rstrip(".:!?")
    if len(t) < 8:
        return None
    if any(p.search(t) for p in SAFE):
        return None
    lower, upper, digit, special = _classes(t)
    if not NAME.match(t):
        if special and lower + upper + digit >= 2 and len(t) <= 64 and _wordlike(t) < 0.5:
            return "mixed+symbols"
        if lower and upper and digit and len(t) >= 10 and _wordlike(t) < 0.4:
            return "mixed"
        if len(t) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", t) and lower and upper and digit \
                and _wordlike(t) < 0.5:
            return "blob"
    for run in _letter_runs(t):
        if run.lower() not in VOCAB and letter_score(run) < GIBBERISH_TH:
            return "gibberish"
    return None


def redact(text):
    if not text:
        return text, 0
    n = 0

    def sub(rx, s, repl):
        nonlocal n
        s2 = rx.sub(repl, s)
        n += s2.count(MASK) - s.count(MASK)
        return s2

    for rx in KNOWN:
        text = sub(rx, text, MASK)
    text = sub(URL_PASS, text, lambda m: m.group(1) + MASK + m.group(3))
    text = sub(BEARER, text, lambda m: m.group(1) + MASK)
    text = sub(QUERY, text, lambda m: m.group(1) + MASK)
    def kv(m):
        v = m.group(2)
        # "token: expired", "auth=none", "password is wrong" are words, not secrets
        if v == MASK or (v.isalpha() and v.islower() and len(v) <= 14 and letter_score(v) >= GIBBERISH_TH):
            return m.group(0)
        return m.group(1) + MASK
    text = sub(KV, text, kv)

    out = []
    for piece in SPLIT.split(text):
        if not piece or SPLIT.fullmatch(piece) or MASK in piece:
            out.append(piece)
            continue
        kv_ = KEYVAL.match(piece)
        if kv_ and why_sensitive(kv_.group(2)):
            out.append(kv_.group(1) + MASK)
            n += 1
        elif not kv_ and why_sensitive(piece):
            out.append(MASK)
            n += 1
        else:
            out.append(piece)
    return "".join(out), n


SPLIT = re.compile(r"(\s+|\]\(|[\[\](){}\"'`,|<>])")
KEYVAL = re.compile(r"^([A-Za-z_][\w.-]*[=:])([^=:].*)$")


def build_local_model(texts):
    """Letter-pair counts from words seen in the owner's chats (so their jargon/Finglish isn't 'gibberish').
    Only words that occur in at least 3 distinct messages count, so a one-off password never trains it."""
    from collections import Counter
    seen = Counter()
    for t in texts:
        seen.update({w.lower() for w in re.findall(r"[A-Za-z]{3,}", t)})
    words = sorted(w for w, k in seen.items() if k >= 3)
    cnt = Counter()
    for w in words:
        s = "^" + w + "$"
        for a, b in zip(s, s[1:]):
            cnt[a + b] += 1
    return {"pairs": dict(cnt), "words": words}
