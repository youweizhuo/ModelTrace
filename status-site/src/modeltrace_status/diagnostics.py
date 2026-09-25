"""Translate provider errors into useful, public-safe explanations.

Only fixed text and recognized codes leave this boundary. Providers sometimes
echo Authorization headers, URLs, account IDs or even prompts in their errors.
"""
from __future__ import annotations

import re


# code: (availability category, title, explanation, suggested action)
ERRORS = {
    "concurrency_queue_timeout": ("rate_limit", "Gateway concurrency queue timed out",
        "The gateway could not obtain a free account slot before its queue deadline. The request was not sent upstream.",
        "Reduce simultaneous requests or wait for active requests to finish."),
    "concurrency_queue_full": ("rate_limit", "Gateway concurrency queue is full",
        "The gateway rejected the request because its waiting queue was full.",
        "Reduce simultaneous requests and retry later."),
    "no_eligible_account": ("provider_error", "No eligible account for this model",
        "The gateway could not select an account for the requested model. This can mean model access restrictions, disabled accounts or a temporary cooldown.",
        "Check the gateway's model routing, account access and cooldown status."),
    "server_is_overloaded": ("provider_error", "Upstream servers are overloaded",
        "An upstream server rejected the request because it was overloaded.",
        "Retry later. A gateway may temporarily freeze an account after repeated overload errors."),
    "upstream_capacity_unavailable": ("provider_error", "Upstream capacity unavailable",
        "The gateway reported that upstream capacity was unavailable for this request.",
        "Retry later and check the gateway's account cooldown status."),
    "provider_infrastructure_unavailable": ("provider_error", "Gateway infrastructure unavailable",
        "The gateway could not prepare its upstream connection.",
        "Check the gateway's connection and infrastructure logs."),
    "insufficient_quota": ("rate_limit", "Provider quota exhausted",
        "The provider reported that the account has no remaining quota or credit.",
        "Check the account's usage allowance, reset time or billing balance."),
    "rate_limit_exceeded": ("rate_limit", "Provider rate limit reached",
        "The provider rejected the request because a rate limit was reached.",
        "Wait for the limit to reset or reduce request frequency."),
    "invalid_api_key": ("auth_error", "API key rejected",
        "The provider reported an invalid API key.",
        "Update this provider's API key in Manage providers."),
    "model_not_found": ("provider_error", "Requested model is unavailable",
        "The provider could not find the requested model or the account cannot access it.",
        "Check the requested model name and the account's model permissions."),
    "connection_refused": ("provider_error", "API connection refused",
        "The configured API server refused the connection.",
        "Check that the API service is running and the endpoint address is correct."),
    "dns_error": ("provider_error", "API hostname could not be resolved",
        "The monitor could not resolve the API server's hostname.",
        "Check the endpoint hostname and this machine's DNS configuration."),
    "tls_error": ("provider_error", "API TLS connection failed",
        "A secure connection to the API server could not be established.",
        "Check the API server's certificate and this machine's certificate trust."),
    "stream_disconnected": ("provider_error", "Response stream interrupted",
        "The provider connection ended before Codex received a completed response.",
        "Check the gateway's streaming connection and upstream logs."),
    "http_400": ("provider_error", "API rejected the request format",
        "The server returned HTTP 400 (Bad Request).",
        "Check model settings and compatibility with the Responses API."),
    "http_401": ("auth_error", "API authentication failed",
        "The server returned HTTP 401 (Unauthorized).",
        "Check this provider's API key in Manage providers."),
    "http_403": ("auth_error", "API access denied",
        "The server returned HTTP 403 (Forbidden).",
        "Check the key's permissions, account status and access to this model."),
    "http_404": ("provider_error", "API route or model not found",
        "The server returned HTTP 404 (Not Found).",
        "Check the base URL, Responses API route and requested model."),
    "http_429": ("rate_limit", "API request limit reached",
        "Codex reported HTTP 429 (Too Many Requests) without a detailed provider reason. Gateway concurrency, provider rate limits and quota limits can all cause this response.",
        "Check the gateway's concurrency limits and the provider's rate or quota limits."),
    "http_503": ("provider_error", "API service unavailable",
        "The server returned HTTP 503 (Service Unavailable), without a recognized reason.",
        "Check gateway routing, account availability and upstream service health."),
    "http_502": ("provider_error", "Gateway received an invalid upstream response",
        "The server returned HTTP 502 (Bad Gateway).", "Check the gateway's upstream connection logs."),
    "http_504": ("provider_error", "Gateway timed out waiting for upstream",
        "The server returned HTTP 504 (Gateway Timeout).", "Check upstream response times and the gateway timeout."),
    "http_500": ("provider_error", "API server encountered an internal error",
        "The server returned HTTP 500 (Internal Server Error).", "Check the API server's logs and retry later."),
    "timeout": ("timeout", "Probe exceeded its time limit",
        "The monitor stopped Codex before it received a complete response.",
        "Check gateway queueing and upstream response times."),
    "provider_error": ("provider_error", "API request failed",
        "Codex could not complete the provider request; a more specific reason was not available.",
        "Check the provider or gateway logs at this probe's timestamp."),
    "rate_limit": ("rate_limit", "Request limited by the API",
        "Codex reported a request limit. The saved result does not distinguish concurrency, rate and quota limits.",
        "Check gateway concurrency and account limits at this probe's timestamp."),
    "auth_error": ("auth_error", "API credentials or permissions rejected",
        "Codex reported an authentication or authorization failure.",
        "Check the provider's API key and model permissions."),
    "runner_error": ("runner_error", "Local Codex check failed",
        "The monitor could not obtain a valid completed Codex run. API availability is unknown.",
        "Check the local Codex installation and monitoring service logs."),
    "tool_use": ("tool_use", "Model attempted to use a tool",
        "This fingerprint probe requires an unaided model response; the attempted tool call was stopped.",
        "Check the model's compatibility with tool-free fingerprint probes."),
    "output_limit": ("output_limit", "Codex output exceeded the safety limit",
        "The monitor stopped the process after excessive output. API availability is unknown.",
        "Check the local runner and model behavior."),
    "interrupted": ("interrupted", "Monitoring was interrupted",
        "The monitoring worker restarted or this monitor's settings changed during the check.",
        "Wait for the next scheduled check."),
}

PROVIDER_CODES = tuple(k for k in ERRORS if not k.startswith("http_") and k not in (
    "timeout", "provider_error", "rate_limit", "auth_error", "runner_error", "tool_use", "output_limit", "interrupted"))


def explanation(code, source="codex_error"):
    category, title, detail, action = ERRORS[code]
    return category, {"code": code, "title": title, "detail": detail, "action": action, "source": source}


def diagnose(message="", outcome="runner_error", http_status=None):
    """Never return arbitrary provider text, even for unrecognized errors."""
    value = message.lower()
    for code in PROVIDER_CODES:
        if re.search(r"\b" + re.escape(code) + r"\b", value):
            category, diagnostic = explanation(code)
            return {"outcome": category, "diagnostic": diagnostic}
    patterns = (
        # Codex often keeps error.message but drops the provider's error.code.
        (r"concurrency.{0,20}queue.{0,20}(?:timed out|timeout)|timed out waiting for (?:a |an )?(?:free )?(?:account|concurrency) slot", "concurrency_queue_timeout"),
        (r"concurrency.{0,20}queue.{0,10}full", "concurrency_queue_full"),
        (r"no (?:eligible|available) (?:upstream )?accounts?", "no_eligible_account"),
        (r"servers? (?:are |is )?(?:currently )?overloaded", "server_is_overloaded"),
        (r"connection refused", "connection_refused"),
        (r"dns error|failed to lookup|name or service not known", "dns_error"),
        (r"certificate verify|invalid peer certificate|tls handshake", "tls_error"),
        (r"insufficient.credit|insufficient.quota|quota.exhausted", "insufficient_quota"),
    )
    for pattern, code in patterns:
        if re.search(pattern, value):
            category, diagnostic = explanation(code)
            return {"outcome": category, "diagnostic": diagnostic}
    if http_status and f"http_{http_status}" in ERRORS:
        category, diagnostic = explanation(f"http_{http_status}", "http_status")
    elif isinstance(http_status, int) and 400 <= http_status <= 599:
        category, diagnostic = explanation("provider_error", "http_status")
        diagnostic.update(title=f"API request failed (HTTP {http_status})",
                          detail=f"The API server returned HTTP {http_status}; a more specific reason was not available.")
    else:
        if "stream disconnected" in value:
            outcome = "stream_disconnected"
        category, diagnostic = explanation(outcome if outcome in ERRORS else "runner_error", "category")
    return {"outcome": category, "diagnostic": diagnostic}


# Probe outcomes that count against availability. An answer the scorer rejected
# (a refusal or a truncated list) reached the provider but gave the check nothing.
FAILURES = {"provider_error", "auth_error", "rate_limit", "timeout", "unusable"}


def probe_outcomes(run):
    """Each probe's outcome, with responses the scorer rejected marked `unusable`."""
    diagnostics = run.get("diagnostics") or []
    # Older runs dropped the diagnostics when upstream found no usable answer at all.
    none_usable = not diagnostics and run.get("identity") == "inconclusive" and not run.get("valid_samples")
    rejected = {d["index"] for d in diagnostics if d.get("accepted") is False}
    outcomes, responded = [], 0
    for attempt in run["attempts"]:
        outcome = attempt["outcome"]
        if outcome == "responded":
            if none_usable or responded in rejected:
                outcome = "unusable"
            responded += 1  # scorer diagnostics are indexed over responded probes only
        outcomes.append(outcome)
    return outcomes


def availability(usable, failed):
    if usable and not failed:
        return "available"
    if usable:
        return "partial"
    return "unavailable" if failed else "unknown"


def run_availability(run):
    outcomes = probe_outcomes(run)
    return availability(outcomes.count("responded"), sum(o in FAILURES for o in outcomes))


def with_explanations(run):
    for attempt in run["attempts"]:
        if attempt["outcome"] != "responded" and not attempt.get("diagnostic"):
            attempt["diagnostic"] = diagnose(outcome=attempt["outcome"], http_status=attempt.get("http_status"))["diagnostic"]
    # Recomputed on read, so stored runs follow the current availability rule.
    if run["state"] != "running" and run["attempts"]:
        run["availability"] = run_availability(run)
    return run
