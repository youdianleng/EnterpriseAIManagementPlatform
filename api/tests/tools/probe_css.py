"""Check which utility classes Tailwind actually generated.

Guards against a theme override silently removing a default utility: a class
that never made it into the stylesheet renders as unstyled text.
"""

import os
import re
import sys
import urllib.request

# Defaults to the compose service name; override with WEB_BASE for other hosts.
BASE = os.environ.get("WEB_BASE", "http://web:3000")
WANTED = [
    ".text-white",
    ".bg-primary",
    ".text-primary-fg",
    ".bg-surface",
    ".text-fg-muted",
    ".tabular",
    ".bg-success-bg",
    ".text-danger",
    ".border-danger",
]


def main() -> None:
    with urllib.request.urlopen(f"{BASE}/en/style-guide", timeout=10) as response:
        html = response.read().decode()

    hrefs = re.findall(r'href="([^"]+\.css[^"]*)"', html)
    print(f"stylesheets referenced: {hrefs}")
    if not hrefs:
        print("no stylesheet linked")
        sys.exit(1)

    css = ""
    for href in hrefs:
        url = href if href.startswith("http") else f"{BASE}{href}"
        with urllib.request.urlopen(url, timeout=15) as response:
            css += response.read().decode()

    print(f"stylesheet bytes: {len(css)}")
    print()

    failures = 0
    for selector in WANTED:
        present = selector in css
        print(f"[{'ok  ' if present else 'FAIL'}] {selector}")
        if not present:
            failures += 1

    print()
    print("FAIL" if failures else "ALL UTILITIES PRESENT")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
