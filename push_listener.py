#!/usr/bin/env python3
"""Make this Mac show up under "Scan -> Computer" on an Epson printer's control panel,
and hand the scan to Epson ScanSmart when Start is pressed.

Replaces Epson Event Manager, which Epson only ships as an Intel binary for macOS, so it
cannot run on Apple silicon without Rosetta. The protocol was captured from Event Manager
on Windows (details in docs/protocol.md):

1. When "Computer" is chosen on the panel, the printer multicasts an SLPv2 AttrRqst for
   service:NetScanMonitor-agent to 239.255.255.253:2968, asking for ClientName / IPAddress /
   EventPort. Every computer that answers with an AttrRply is listed on the panel.
2. When a computer is picked and Start is pressed, the printer connects to TCP <EventPort>
   and sends HTTP POST /PushScan (SOAP, carries PushScanIDIn). The computer answers 200 OK;
   the scan itself is then pulled by ScanSmart over ESC/I (TCP 1865).
3. The hand-off to ScanSmart is its AppleScript command (from ScanSmart.sdef):
   notify kind "invokejob" scanner "<scanner name>" ipaddress "<printer IP>" jobinfo "<job file>"
   Create a folder named _ScanManagerLog on the Desktop and ScanSmart writes a debug log there.

Each Mac runs its own copy and answers the panel on its own; the panel lists all of them.
Start at login: bash install.sh

Usage: python3 push_listener.py [--name MBP] [--scanner "EPSON L6270 Series"] [--debug]
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

SLP_GROUP, PORT = "239.255.255.253", 2968
SERVICE = b"service:NetScanMonitor-agent"
SCANSMART_ID = "com.epson.scan.Epson-ScanSmart"
SCANSMART_PREFS = Path.home() / "Library/Preferences/com.epson.scan.Epson-ScanSmart.plist"
NO_JOB_FILE = "/tmp/epson-pushscan-no-job"

debug = False


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def slp_reply(req: bytes, attrs: str) -> bytes | None:
    """Answer an SLPv2 AttrRqst (function 6) with an AttrRply (function 7), reusing its XID and language tag."""
    if len(req) < 16 or req[0] != 2 or req[1] != 6 or SERVICE not in req:
        return None
    xid = req[10:12]
    lang_len = struct.unpack_from(">H", req, 12)[0]
    lang = req[14:14 + lang_len]
    body = b"\x00\x00" + struct.pack(">H", len(attrs)) + attrs.encode() + b"\x00"  # error=0, attr list, 0 auth blocks
    total = 14 + lang_len + len(body)
    return (bytes([2, 7]) + total.to_bytes(3, "big") + b"\x00\x00" + b"\x00\x00\x00"
            + xid + struct.pack(">H", lang_len) + lang + body)


def local_ip(target: str) -> str:
    """The address of the interface that traffic to `target` leaves from (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target, 9))
        return s.getsockname()[0]


def scansmart_scanner() -> str | None:
    """The scanner ScanSmart used last, e.g. "EPSON L6270 Series" (same string as its TWAIN name)."""
    try:
        with open(SCANSMART_PREFS, "rb") as f:
            return plistlib.load(f)["previousScanner"]["displayName"]
    except (OSError, KeyError, TypeError, plistlib.InvalidFileException):
        return None


def slp_loop(my_ip: str, attrs: str, route_to: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(SLP_GROUP) + socket.inet_aton(my_ip))
    s.settimeout(60)
    while True:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            data = None
        # Network changed (new IP): exit and let launchd restart us, so we rejoin the group and report the new IP
        try:
            now_ip = local_ip(route_to)
        except OSError:
            now_ip = None
        if now_ip != my_ip:
            log(f"Local IP changed from {my_ip} to {now_ip}; exiting so launchd restarts us")
            os._exit(1)
        if data is None:
            continue
        reply = slp_reply(data, attrs)
        if debug:
            log(f"SLP {addr} {data[:96]!r} -> {'reply' if reply else 'ignore'}")
        if reply:
            s.sendto(reply, addr)
            log(f"Panel is looking for computers ({addr[0]}); answered {attrs.split(',')[0]}")


PUSH_RESPONSE_BODY = (
    '<?xml version="1.0" ?>\r\n'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">\r\n'
    "  <s:Body>\r\n"
    '    <p:PushScanResponse xmlns:p="http://schema.epson.net/EpsonNet/Scan/2004/pushscan">\r\n'
    "      <StatusOut>OK</StatusOut>\r\n"
    "    </p:PushScanResponse>\r\n"
    "  </s:Body>\r\n"
    "</s:Envelope>\r\n"
).encode()


def push_response(uid: str) -> bytes:
    """200 OK exactly as Event Manager sends it (including the odd "Header : value" spacing)."""
    head = (
        "HTTP/1.0 200 OK\r\nServer : Epson Net Scan Monitor/2.0\r\nContent-Type : application/octet-stream\r\n"
        f"Content-Length : {len(PUSH_RESPONSE_BODY)}\r\nx-protocol-name : Epson Network Service Protocol\r\n"
        f"x-protocol-version : 2.00\r\nx-uid : {uid}\r\nx-status : 0001\r\n\r\n"
    ).encode()
    return head + PUSH_RESPONSE_BODY


def parse_push_scan(body: bytes, fallback_ip: str) -> tuple[str, str]:
    """(PushScanIDIn, printer IP) from a PushScan request body. IPAddressIn is the IPv4 address in hex."""
    scan_id = re.search(rb"<PushScanIDIn>([^<]*)<", body)
    ip_hex = re.search(rb"<IPAddressIn>([0-9A-Fa-f]{8})<", body)
    printer_ip = socket.inet_ntoa(bytes.fromhex(ip_hex.group(1).decode())) if ip_hex else fallback_ip
    return (scan_id.group(1).decode() if scan_id else "?"), printer_ip


def invoke_scansmart(printer_ip: str, scanner: str) -> None:
    # Without jobinfo ScanSmart only logs "no jobinfo" and does nothing. With a path to a job file
    # that does not exist it goes startPushScan -> scan -> preview window (tested 2026-10-27).
    script = (f'tell application id "{SCANSMART_ID}"\n'
              f'    notify kind "invokejob" scanner "{scanner}" ipaddress "{printer_ip}" jobinfo "{NO_JOB_FILE}"\n'
              "end tell\n")
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode:
        log(f"Could not start ScanSmart: {r.stderr.strip()}")


class PushHandler(socketserver.BaseRequestHandler):
    def handle(self):
        conn = self.request
        conn.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
        need = int(m.group(1)) if m else 0
        while len(body) < need:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        if debug:
            log(f"PushScan raw: {head!r} {body!r}")
        uid = re.search(rb"x-uid:\s*(\S+)", head, re.I)
        conn.sendall(push_response(uid.group(1).decode() if uid else "1"))

        scan_id, printer_ip = parse_push_scan(body, self.client_address[0])
        log(f"Start pressed on the panel (PushScanID={scan_id}, printer {printer_ip}); handing over to ScanSmart")
        threading.Thread(target=invoke_scansmart, args=(printer_ip, self.server.scanner), daemon=True).start()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    scanner = ""


def main() -> None:
    global debug
    ap = argparse.ArgumentParser(description='Epson panel "Scan -> Computer" listener for macOS (replaces Event Manager)')
    ap.add_argument("--name", default=subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                                                     text=True).stdout.strip() or socket.gethostname(),
                    help="name shown on the panel (default: this Mac's computer name; keep it ASCII)")
    ap.add_argument("--scanner", help='ScanSmart scanner name, e.g. "EPSON L6270 Series" '
                                      "(default: the scanner ScanSmart used last)")
    ap.add_argument("--printer", help="printer IP, only used to pick the network interface "
                                      "(default: the interface multicast goes out on)")
    ap.add_argument("--debug", action="store_true", help="log every SLP and PushScan message")
    args = ap.parse_args()
    debug = args.debug
    sys.stdout.reconfigure(line_buffering=True)

    scanner = args.scanner or scansmart_scanner()
    if not scanner:
        sys.exit("Could not find the scanner name: open Epson ScanSmart and scan once, or pass --scanner.")
    route_to = args.printer or SLP_GROUP
    my_ip = local_ip(route_to)
    attrs = f"(ClientName={args.name}),(IPAddress={my_ip}),(EventPort={PORT})"
    threading.Thread(target=slp_loop, args=(my_ip, attrs, route_to), daemon=True).start()
    log(f'Waiting for "Scan -> Computer" on the panel as {args.name} ({my_ip}), scanner "{scanner}". Ctrl+C to quit.')
    try:
        server = Server(("", PORT), PushHandler)
        server.scanner = scanner
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
