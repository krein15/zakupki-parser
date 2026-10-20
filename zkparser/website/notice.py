"""The full XML of a notice: the same epNotification* document the SOAP service puts into its archives.

A 44-ФЗ or ПП РФ 615 notice is asked for by its number; a 223-ФЗ one lives in another part of the site and needs the
notice's guid, which its search result links to.
"""

from __future__ import annotations

from .client import SiteError
from .search import Client

NOTICE_XML_PATH = "/epz/order/notice/printForm/viewXml.html"
NOTICE_223_XML_PATH = "/epz/order/notice/notice223/printForm/viewXml.html"


def download_notice(client: Client, reg_number: str, guid: str = "") -> bytes:
    if guid:
        xml = client.get(NOTICE_223_XML_PATH, {"purchaseNoticeGuid": guid, "purchaseNoticeNumber": reg_number})
    else:
        xml = client.get(NOTICE_XML_PATH, {"regNumber": reg_number})
    if not xml.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<?xml"):
        raise SiteError(f"ЕИС не отдал XML извещения {reg_number}")
    return xml
