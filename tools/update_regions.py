"""Refresh the region table in zkparser/regions.py from the EIS website.

The site lists its regions in the "my region" picker (``/epz/nsi/kladr/chooseRegion.html``): one ``<li>`` per
region with its 11-digit KLADR code as the id.

Usage: python tools/update_regions.py [--write]
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zkparser.website.client import SiteClient  # noqa: E402

REGIONS_FILE = ROOT / "zkparser" / "regions.py"
PICKER_PATH = "/epz/nsi/kladr/chooseRegion.html"
ITEM = re.compile(r'<li class="select-vars__item[^"]*" id="(\d{11})">\s*([^<]+?)\s*</li>')
HEADER = "REGIONS: dict[str, str] = {"


def main() -> None:
    page = SiteClient().get(PICKER_PATH).decode("utf-8")
    regions = {code: " ".join(html.unescape(name).split()) for code, name in ITEM.findall(page)}
    print(f"{len(regions)} regions")
    block = "\n".join(f'    "{code}": "{name}",' for code, name in sorted(regions.items()))
    print(f"\n{HEADER}\n{block}\n}}")
    if "--write" in sys.argv:
        source = REGIONS_FILE.read_text(encoding="utf-8")
        start = source.index(HEADER)
        end = source.index("\n}", start) + 2
        REGIONS_FILE.write_text(f"{source[:start]}{HEADER}\n{block}\n}}{source[end:]}", encoding="utf-8")
        print(f"\nwritten to {REGIONS_FILE}")


if __name__ == "__main__":
    main()
