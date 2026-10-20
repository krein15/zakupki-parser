"""Copy notice XML into tests/fixtures with the contact person's details replaced.

Every notice names a contact person of the placing organization: full name, e-mail, phone, fax and a free-text
note. That is personal data, so test fixtures carry placeholders instead. A 223-ФЗ notice also carries electronic
signatures, whose certificates name the person who signed: they are emptied. Organization names, INNs and addresses
are public and stay as they are.

Usage: python tools/anonymize_notice.py <notice.xml>... [--out tests/fixtures]
"""

from __future__ import annotations

import sys
from pathlib import Path

from lxml import etree

PLACEHOLDERS = {
    "lastName": "Иванов",
    "firstName": "Иван",
    "middleName": "Иванович",
    "contactEMail": "contact@example.org",
    "contactPhone": "7-000-0000000",
    "contactFax": "7-000-0000000",
    "addInfo": "Контактные данные удалены",
    "email": "contact@example.org",  # 223-ФЗ
    "phone": "7-000-0000000",
}
SIGNATURES = {"userSignature", "trustedSignature", "signature"}  # base64 certificates with the signer's name
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def anonymize(source: Path, target: Path) -> int:
    tree = etree.parse(str(source), PARSER)
    replaced = 0
    for element in tree.iter():
        if not isinstance(element.tag, str) or len(element):
            continue
        name = etree.QName(element).localname
        if name in PLACEHOLDERS:
            element.text = PLACEHOLDERS[name]
            replaced += 1
        elif name in SIGNATURES and element.text:
            element.text = ""
            replaced += 1
    tree.write(str(target), xml_declaration=True, encoding="UTF-8", standalone=True)
    return replaced


def main() -> None:
    args = sys.argv[1:]
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    if "--out" in args:
        index = args.index("--out")
        out = Path(args[index + 1])
        del args[index : index + 2]
    for name in args:
        source = Path(name)
        target = out / source.name
        print(f"{source.name}: {anonymize(source, target)} fields replaced -> {target}")


if __name__ == "__main__":
    main()
