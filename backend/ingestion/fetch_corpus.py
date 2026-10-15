"""Download the Well-Architected pillar whitepapers that form the retrieval corpus.

AWS publishes one PDF per pillar, which is why pillar attribution needs no
classification step: the source file *is* the label. That keeps the metadata
filter the specialist agents rely on trustworthy by construction.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

# pillar key -> AWS documentation slug
PILLARS: dict[str, str] = {
    "security": "security-pillar",
    "cost": "cost-optimization-pillar",
    "reliability": "reliability-pillar",
}

BASE_URL = "https://docs.aws.amazon.com/pdfs/wellarchitected/latest"


def pdf_url(slug: str) -> str:
    return f"{BASE_URL}/{slug}/wellarchitected-{slug}.pdf"


def fetch(pillar: str, slug: str, force: bool = False) -> Path:
    target = RAW_DIR / f"{pillar}.pdf"
    if target.exists() and not force:
        print(f"  {pillar:<12} already present ({target.stat().st_size / 1e6:.1f} MB)")
        return target

    url = pdf_url(slug)
    # A default User-Agent is required; the docs endpoint rejects urllib's own.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        target.write_bytes(response.read())

    print(f"  {pillar:<12} downloaded ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def main() -> int:
    force = "--force" in sys.argv
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {len(PILLARS)} pillar whitepapers into {RAW_DIR}")
    for pillar, slug in PILLARS.items():
        try:
            fetch(pillar, slug, force=force)
        except Exception as exc:  # noqa: BLE001 - surface the cause, keep going
            print(f"  {pillar:<12} FAILED: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
