"""Checks push_listener against packets captured between an EPSON L6270 and Event Manager on Windows.

The Windows host name and the IP addresses were replaced with strings of the same length,
so every length field below is exactly as captured.
"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("push_listener", Path(__file__).parent.parent / "push_listener.py")
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)

# Printer -> 239.255.255.253:2968, SLPv2 AttrRqst (XID 0x000b, scope "PID 1181")
ATTR_RQST = bytes.fromhex(
    "020600005c2000000000000b0002656e"
    "0000001c" + b"service:NetScanMonitor-agent".hex()
    + "0008" + b"PID 1181".hex()
    + "001e" + b"ClientName,IPAddress,EventPort".hex()
    + "0000"
)
ATTRS = "(ClientName=WIN-LAPTOP),(IPAddress=192.168.10.103),(EventPort=2968)"
# Windows -> printer, SLPv2 AttrRply
ATTR_RPLY = bytes.fromhex("02070000580000000000000b0002656e" "00000043" + ATTRS.encode().hex() + "00")

PUSH_SCAN_BODY = (
    b'<?xml version="1.0" ?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
    b'<p:PushScan xmlns:p="http://schema.epson.net/EpsonNet/Scan/2004/pushscan"><ProductNameIn>PID 1181</ProductNameIn>'
    b"<IPAddressIn>C0A80AAB</IPAddressIn><PushScanIDIn>04</PushScanIDIn></p:PushScan></s:Body></s:Envelope>"
)


class SlpTest(unittest.TestCase):
    def test_reply_matches_capture(self):
        self.assertEqual((len(ATTR_RQST), len(ATTR_RPLY)), (92, 88))
        self.assertEqual(pl.slp_reply(ATTR_RQST, ATTRS), ATTR_RPLY)

    def test_ignores_other_messages(self):
        self.assertIsNone(pl.slp_reply(b"\x02\x01" + ATTR_RQST[2:], ATTRS))  # SrvRqst, not AttrRqst
        self.assertIsNone(pl.slp_reply(ATTR_RQST.replace(b"NetScanMonitor", b"OtherService!!"), ATTRS))
        self.assertIsNone(pl.slp_reply(ATTR_RQST[:10], ATTRS))


class PushScanTest(unittest.TestCase):
    def test_body_length_matches_capture(self):
        self.assertEqual(len(PUSH_SCAN_BODY), 306)

    def test_parse(self):
        self.assertEqual(pl.parse_push_scan(PUSH_SCAN_BODY, "10.0.0.1"), ("04", "192.168.10.171"))
        self.assertEqual(pl.parse_push_scan(b"<x/>", "10.0.0.1"), ("?", "10.0.0.1"))

    def test_response_matches_capture(self):
        resp = pl.push_response("1")
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertEqual(len(body), 276)
        self.assertEqual(head.split(b"\r\n"), [
            b"HTTP/1.0 200 OK",
            b"Server : Epson Net Scan Monitor/2.0",
            b"Content-Type : application/octet-stream",
            b"Content-Length : 276",
            b"x-protocol-name : Epson Network Service Protocol",
            b"x-protocol-version : 2.00",
            b"x-uid : 1",
            b"x-status : 0001",
        ])


if __name__ == "__main__":
    unittest.main()
