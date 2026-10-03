"""The full XML of a notice: the same epNotification* document the SOAP service puts into its archives."""

from __future__ import annotations

from .client import SiteError
from .search import Client

NOTICE_XML_PATH = "/epz/order/notice/printForm/viewXml.html"


def download_notice(client: Client, reg_number: str) -> bytes:
    xml = client.get(NOTICE_XML_PATH, {"regNumber": reg_number})
    if not xml.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<?xml"):
        raise SiteError(f"ЕИС не отдал XML извещения {reg_number}")
    return xml
