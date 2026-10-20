"""OKPD2 codes → the ids of the site's classifier, for the search to filter by codes.

The extended search filters by OKPD2 only when it gets the classifier's own ids (``okpd2Ids``) with nested codes on;
the codes alone (``okpd2IdsCodes``) are ignored. The ids come from the classifier search of the site: a tree of every
node whose code or name contains the value asked. They do not change, so the ones found are kept in a JSON file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from .client import SiteError
from .search import Client

log = logging.getLogger("zkparser.website")

CLASSIFIER_PATH = "/epz/api/nsi/okpd2/search.html"


def normalize(code: str) -> str:
    """A code as the classifier writes it: "10." → "10"; a KTRU code "21.20.10.234-00019" → its OKPD2 part."""
    return code.split("-")[0].strip().rstrip(".")


class Okpd2Ids:
    """The classifier ids of OKPD2 codes, asked from the site once and remembered in ``path``."""

    def __init__(self, client: Client, path: Path | None = None) -> None:
        self.client = client
        self.path = path
        self.known: dict[str, str] = {}
        if path and path.exists():
            try:
                self.known = {str(code): str(key) for code, key in json.loads(path.read_text("utf-8")).items()}
            except (OSError, ValueError, AttributeError):
                log.warning("Не удалось прочитать %s: номера кодов ОКПД2 будут запрошены заново", path)

    def ids(self, codes: Iterable[str]) -> dict[str, str]:
        """{code: id} for every code the classifier has; a code it does not know is left out.

        Raises SiteError when the site does not answer: without the ids the codes cannot be searched for.
        """
        found = {}
        asked = False
        for code in dict.fromkeys(normalize(code) for code in codes if normalize(code)):
            if code not in self.known:
                asked = True
                try:
                    tree = json.loads(self.client.get(CLASSIFIER_PATH, {"value": code}))
                except ValueError as error:
                    raise SiteError("Справочник ОКПД2 сайта ЕИС ответил не JSON") from error
                key = _find(tree, code)
                if key is None:
                    log.warning("ОКПД2 %s нет в справочнике сайта ЕИС", code)
                    continue
                self.known[code] = key
            found[code] = self.known[code]
        if asked and self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.known, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
            except OSError:
                log.debug("Could not save the OKPD2 ids", exc_info=True)
        return found


def _find(tree: object, code: str) -> str | None:
    if not isinstance(tree, dict):
        raise SiteError("Справочник ОКПД2 сайта ЕИС ответил не так, как ожидалось")
    return next((str(node["key"]) for node in _nodes(tree.get("children")) if node.get("code") == code), None)


def _nodes(children: object) -> Iterator[dict]:
    for node in children or []:
        if isinstance(node, dict):
            yield node
            yield from _nodes(node.get("children"))
