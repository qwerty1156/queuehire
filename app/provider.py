import hashlib
import re


class ProviderTemporaryError(Exception):
    pass


class ProviderPermanentError(Exception):
    pass


def analyze(title: str, text: str, attempt: int) -> dict:
    """Deterministic stand-in for an external paid AI provider.

    Markers are test inputs, not real AI instructions. Never requires network.
    The caller passes the attempt number starting at one.
    """
    if "[PERMANENT]" in text:
        raise ProviderPermanentError("provider_rejected")
    if "[TEMPORARY]" in text:
        raise ProviderTemporaryError("provider_unavailable")
    if "[FAIL_ONCE]" in text and attempt == 1:
        raise ProviderTemporaryError("provider_unavailable")

    skills = [
        skill for skill in ["Python", "FastAPI", "PostgreSQL", "Docker", "Redis", "SQL"]
        if re.search(r"\b" + skill + r"\b", text, flags=re.IGNORECASE)
    ]
    return {
        "summary": f"{title}: {len(skills)} recognized skills",
        "skills": skills,
        "fingerprint": hashlib.sha256((title + "\n" + text).encode()).hexdigest(),
        "provider": "local-demo",
    }
