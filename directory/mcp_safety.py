"""MCP malware/abuse static scanner.

Pure-function content scan over an MCP server's public metadata
(name + description + tools text). No network, no execution — it only
pattern-matches the *advertised* text for known social-engineering and
remote-code-execution lures, the kind that listing-spam servers use
(e.g. a "PaperSearcher" whose description tells you to paste
``echo <base64> | base64 -d | bash`` into your terminal).

Returns a verdict the directory can store and surface:
  verdict: "clean" | "suspicious" | "malicious"
  score:   0..100 risk score (higher = worse)
  reasons: list of human-readable findings (rule_id + snippet)

Design goals:
  - zero false-positive on legitimate servers (a paper/code server may
    legitimately mention "bash" or "curl" in tool docs, so a bare keyword
    is NOT enough — we require the *pipe-to-shell* or *decode-to-shell*
    composition, install-instruction framing, or bare-IP payload host).
  - explainable: every point of score has a reason string.
"""
from __future__ import annotations

import base64 as _b64
import re
from dataclasses import dataclass, field
from typing import Optional


# ---- rule patterns -------------------------------------------------------

# pipe a download straight into a shell:  curl ... | bash   /   wget ... | sh
_PIPE_TO_SHELL = re.compile(
    r"(?:curl|wget|fetch)\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:bash|sh|zsh|python3?|node|ruby|perl)\b",
    re.I,
)
# decode something and run it:  base64 -d | bash   /   | base64 --decode | sh
_DECODE_TO_SHELL = re.compile(
    r"base64\s+(?:-d|--decode|-D)\b[^\n]{0,80}\|\s*(?:bash|sh|zsh|python3?|node)\b"
    r"|\|\s*base64\s+(?:-d|--decode|-D)\b[^\n]{0,80}\|\s*(?:bash|sh|zsh)\b",
    re.I,
)
# eval of a download:  eval "$(curl ...)"   /   bash -c "$(curl ...)"
_EVAL_DOWNLOAD = re.compile(
    r"(?:eval|bash\s+-c|sh\s+-c)\s+[\"']?\$\((?:curl|wget|fetch)\b",
    re.I,
)
# "to install, open terminal and run/enter/paste ..." framing around a command
_INSTALL_LURE = re.compile(
    r"(?:open\s+(?:the\s+)?(?:macos\s+)?terminal|open\s+a\s+terminal|paste\s+(?:this|the following)|"
    r"run\s+(?:this|the following)\s+command|enter\s+the\s+command)\b",
    re.I,
)
# powershell download cradle:  IEX (New-Object Net.WebClient).DownloadString(...)
_PS_CRADLE = re.compile(
    r"(?:iex|invoke-expression)\b[^\n]{0,80}(?:downloadstring|downloadfile|webclient|invoke-webrequest|irm)\b"
    r"|\b(?:irm|invoke-webrequest)\b[^\n|]{0,120}\|\s*iex\b",
    re.I,
)
# a URL whose host is a bare IPv4 (payload servers rarely use domains)
_BARE_IP_URL = re.compile(
    r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/\S*)?",
    re.I,
)
# data-exfiltration / prompt-injection phrasings inside tool text
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions"
    r"|disregard\s+(?:the\s+)?(?:system|previous)\s+prompt"
    r"|exfiltrat|send\s+(?:your|the)\s+(?:api[_\s-]?key|secret|token|credential|\.env)\b"
    r"|read\s+(?:the\s+)?(?:~/\.ssh|/etc/passwd|\.env)\b",
    re.I,
)
# A security/defense product *describes* attacks it protects against; those
# mentions must NOT be flagged. If defensive framing is present we suppress the
# prompt-injection rule (RCE rules below stay strict regardless).
_DEFENSE_CONTEXT = re.compile(
    r"\b(?:detect|detection|block|blocks?|blocking|prevent|protect|protection|"
    r"defen[sc]e|defend|guard|firewall|scanner|scan(?:ning)?|mitigat|"
    r"security|audit|red[\s-]?team|circuit\s+breaker|safeguard|monitor)\b",
    re.I,
)
# long base64 blob (>= 40 chars) that decodes to something shell-looking
_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
# A decoded blob is only "shell payload" if it looks like an actual command:
# a shell shebang/interpreter invocation, or a download-and-run cradle. A bare
# URL is NOT enough (config JSON, data-URIs, manifests legitimately embed URLs).
_SHELLISH = re.compile(
    r"/bin/(?:ba|z)?sh\b"                      # /bin/bash, /bin/sh
    r"|\b(?:ba|z)?sh\s+-c\b"                   # bash -c
    r"|\b(?:curl|wget|fetch)\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"  # curl ... | bash
    r"|\beval\s+[\"']?\$\("                    # eval "$(...)
    r"|\bchmod\s+\+x\b"                        # chmod +x payload
    r"|\b(?:iex|invoke-expression)\b"          # powershell
    r"|\bpython3?\s+-c\b[^\n]{0,80}(?:exec|os\.system|subprocess)",  # python -c exec
    re.I,
)

# cheap/abused TLDs commonly used for throwaway payload/phishing hosts
_CHEAP_TLD = re.compile(r"https?://[^\s/]+\.(?:click|top|xyz|gq|cf|tk|ml|work|rest|sbs|cyou)\b", re.I)


@dataclass
class Finding:
    rule: str
    weight: int
    snippet: str


@dataclass
class ScanResult:
    verdict: str               # clean | suspicious | malicious
    score: int                 # 0..100
    reasons: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "reasons": [
                {"rule": f.rule, "weight": f.weight, "snippet": f.snippet[:160]}
                for f in self.reasons
            ],
        }


def _snippet(text: str, m: re.Match) -> str:
    s = max(0, m.start() - 20)
    e = min(len(text), m.end() + 20)
    return re.sub(r"\s+", " ", text[s:e]).strip()


def _decoded_is_shellish(blob: str) -> Optional[str]:
    """Return a short preview if a base64 blob decodes to shell-looking text."""
    # pad to multiple of 4
    pad = (-len(blob)) % 4
    try:
        raw = _b64.b64decode(blob + "=" * pad, validate=False)
    except Exception:
        return None
    try:
        txt = raw.decode("utf-8", "ignore")
    except Exception:
        return None
    if _SHELLISH.search(txt):
        return re.sub(r"\s+", " ", txt)[:140]
    return None


def scan_text(text: str, defensive: bool = False) -> list[Finding]:
    """Run all content rules over a single text blob.

    ``defensive``: when True, the prompt-injection rule is suppressed because a
    security/defense product is naming the attacks it protects against.
    """
    out: list[Finding] = []
    if not text:
        return out

    for rule, weight, pat in (
        ("pipe_to_shell", 70, _PIPE_TO_SHELL),
        ("decode_to_shell", 80, _DECODE_TO_SHELL),
        ("eval_download", 75, _EVAL_DOWNLOAD),
        ("powershell_cradle", 70, _PS_CRADLE),
        ("prompt_injection", 60, _INJECTION),
    ):
        m = pat.search(text)
        if m:
            # A defensive/security product legitimately *names* the attacks it
            # detects — suppress the injection rule when defense framing is near.
            if rule == "prompt_injection" and (defensive or _DEFENSE_CONTEXT.search(text)):
                continue
            out.append(Finding(rule, weight, _snippet(text, m)))

    # install-lure only counts as risk if it co-occurs with a command-ish token
    m = _INSTALL_LURE.search(text)
    if m and re.search(r"(?:curl|wget|bash|sh\b|base64|powershell|iex|\|\s*sh)", text, re.I):
        out.append(Finding("install_lure", 40, _snippet(text, m)))

    # bare-IP URL in advertised text (payload host smell)
    m = _BARE_IP_URL.search(text)
    if m:
        out.append(Finding("bare_ip_url", 35, _snippet(text, m)))

    # cheap throwaway TLD
    m = _CHEAP_TLD.search(text)
    if m:
        out.append(Finding("cheap_tld", 20, _snippet(text, m)))

    # base64 blob that decodes to a shell payload (the PaperSearcher trick)
    for bm in _B64_BLOB.finditer(text):
        preview = _decoded_is_shellish(bm.group(0))
        if preview:
            out.append(Finding("base64_shell_payload", 85, "decodes→ " + preview))
            break

    return out


def scan_mcp(name: str = "", description: str = "", tools_text: str = "") -> ScanResult:
    """Scan an MCP server's public metadata. Returns a ScanResult."""
    # Defense framing may appear in one field while the attack noun appears in
    # another (e.g. "security scanner" in description, "prompt injection" in
    # tools). Evaluate defense context over the *combined* text so a security
    # product is not flagged for naming the attacks it detects.
    combined = " ".join(x for x in (name, description, tools_text) if x)
    defensive = bool(_DEFENSE_CONTEXT.search(combined))

    findings: list[Finding] = []
    seen: set[str] = set()
    for blob in (name, description, tools_text):
        for f in scan_text(blob or "", defensive=defensive):
            if f.rule in seen:
                continue
            seen.add(f.rule)
            findings.append(f)

    score = min(100, sum(f.weight for f in findings))
    if score >= 70:
        verdict = "malicious"
    elif score >= 30:
        verdict = "suspicious"
    else:
        verdict = "clean"
    return ScanResult(verdict=verdict, score=score, reasons=findings)


__all__ = ["scan_mcp", "scan_text", "ScanResult", "Finding"]
