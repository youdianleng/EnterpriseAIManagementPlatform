"""Probe the web container's locale middleware without following redirects.

Run inside the compose network:
    docker compose exec -T api python /app/tests/tools/probe_locale.py
"""

import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


OPENER = urllib.request.build_opener(NoRedirect)
BASE = "http://web:3000/"

CASES = [
    ("no headers", {}),
    ("Accept-Language: en-US", {"Accept-Language": "en-US,en;q=0.9"}),
    ("Accept-Language: es-ES", {"Accept-Language": "es-ES,es;q=0.9"}),
    ("Accept-Language: fr-FR", {"Accept-Language": "fr-FR,fr;q=0.9"}),
    ("Accept-Language: de;q=0.8,es;q=0.5", {"Accept-Language": "de;q=0.8,es;q=0.5"}),
    ("lang=en + cookie=es", {"Accept-Language": "en-US", "Cookie": "eam_locale=es"}),
    ("lang=es + cookie=en", {"Accept-Language": "es-ES", "Cookie": "eam_locale=en"}),
    ("lang=fr + cookie=en", {"Accept-Language": "fr-FR", "Cookie": "eam_locale=en"}),
]


def probe(headers: dict[str, str]) -> tuple[int, str | None]:
    request = urllib.request.Request(BASE, headers=headers)
    try:
        response = OPENER.open(request, timeout=10)
        return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location")


def main() -> None:
    failures = 0
    for name, headers in CASES:
        status, location = probe(headers)
        print(f"{name:<38} -> {status}  {location or ''}")
        if status != 307 or not location:
            failures += 1
    print("FAIL" if failures else "OK")


if __name__ == "__main__":
    main()
