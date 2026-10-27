#!/usr/bin/env python3
"""Make this Mac show up under "Scan -> Computer (WSD)" on the printer's panel and save
each scan straight into a folder, without any Epson software.

Uses standard WS-Scan (WSD): subscribe to the printer's ScanAvailableEvent; when this
computer is picked on the panel the printer calls us back -> CreateScanJob -> RetrieveImage.
The printers tested only return JPEG over WSD (100/300 dpi); for PDF use push_listener.py.

Usage:
    python3 scan_listener.py --printer 192.168.1.20
    python3 scan_listener.py --printer 192.168.1.20 --out ~/Documents/Scans --dpi 300 --debug

The panel lists two destinations: "<name> Glass" (flatbed) and "<name> ADF" (feeder, whole stack).
Ctrl+C to quit (unsubscribes first).
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import http.server
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wse": "http://schemas.xmlsoap.org/ws/2004/08/eventing",
    "wscn": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
}
SCAN = NS["wscn"]
ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
PORT = 6390
RENEW_EVERY = 600  # the printer only grants PT15M, so renew every 10 minutes
# A4 in 1/1000 inch (printer maximum 8500 x 11700)
A4_W, A4_H = 8267, 11692
# ASCII labels: the small panel display may not render other scripts
SOURCES = {"platen": ("Platen", "Glass", 1), "adf": ("ADF", "ADF", 0)}  # 0 = whole ADF stack
CLIENT_ID = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname())}"

log_dir: Path | None = None


def debug(name: str, data: bytes | str) -> None:
    if log_dir:
        p = log_dir / f"{datetime.now():%H%M%S}-{name}"
        p.write_bytes(data if isinstance(data, bytes) else data.encode())


def envelope(to: str, action: str, body: str, extra_header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{NS["soap"]}" xmlns:wsa="{NS["wsa"]}" xmlns:wse="{NS["wse"]}" xmlns:wscn="{SCAN}">'
        f"<soap:Header><wsa:To>{to}</wsa:To><wsa:Action>{action}</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
        f"<wsa:ReplyTo><wsa:Address>{ANON}</wsa:Address></wsa:ReplyTo>"
        f"<wsa:From><wsa:Address>{CLIENT_ID}</wsa:Address></wsa:From>{extra_header}</soap:Header>"
        f"<soap:Body>{body}</soap:Body></soap:Envelope>"
    ).encode()


def post(url: str, data: bytes, name: str) -> tuple[str, bytes]:
    """Return (Content-Type, body). HTTP 500 with a SOAP fault is returned too, for the caller to inspect."""
    debug(f"{name}-req.xml", data)
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ctype, body = r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        ctype, body = e.headers.get("Content-Type", ""), e.read()
    debug(f"{name}-resp.bin", f"Content-Type: {ctype}\n\n".encode() + body)
    return ctype, body


def find(xml: bytes, path: str) -> str | None:
    el = ET.fromstring(xml).find(path, NS)
    return el.text.strip() if el is not None and el.text else None


class Scanner:
    def __init__(self, url: str, notify_url: str, name: str, out: Path, dpi: int):
        self.url, self.notify_url, self.name, self.out, self.dpi = url, notify_url, name, out, dpi
        self.sub_id: str | None = None
        self.tokens: dict[str, str] = {}  # ClientContext -> DestinationToken
        self.lock = threading.Lock()

    # ---- subscription ----
    def subscribe(self) -> None:
        dests = "".join(
            f"<wscn:ScanDestination><wscn:ClientDisplayName>{escape(f'{self.name} {label}')}</wscn:ClientDisplayName>"
            f"<wscn:ClientContext>{ctx}</wscn:ClientContext></wscn:ScanDestination>"
            for ctx, (_, label, _) in SOURCES.items()
        )
        ref = f"<wsa:ReferenceParameters><wse:Identifier>{CLIENT_ID}</wse:Identifier></wsa:ReferenceParameters>"
        body = (
            # Same shape Windows sends: Push delivery + EndTo + ReferenceParameters
            f"<wse:Subscribe><wse:EndTo><wsa:Address>{self.notify_url}</wsa:Address>{ref}</wse:EndTo>"
            '<wse:Delivery Mode="http://schemas.xmlsoap.org/ws/2004/08/eventing/DeliveryModes/Push">'
            f"<wse:NotifyTo><wsa:Address>{self.notify_url}</wsa:Address>{ref}</wse:NotifyTo></wse:Delivery>"
            "<wse:Expires>PT1H</wse:Expires>"
            '<wse:Filter Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/Action">'
            f"{SCAN}/ScanAvailableEvent</wse:Filter><wscn:ScanDestinations>{dests}</wscn:ScanDestinations></wse:Subscribe>"
        )
        _, resp = post(self.url, envelope(self.url, f"{NS['wse']}/Subscribe", body), "subscribe")
        root = ET.fromstring(resp)
        sub_id = root.find(".//wse:Identifier", NS)
        if sub_id is None:
            raise RuntimeError(f"Subscribe failed: {resp[:300]!r}")
        tokens = {
            d.findtext("wscn:ClientContext", namespaces=NS): d.findtext("wscn:DestinationToken", namespaces=NS)
            for d in root.iterfind(".//wscn:DestinationResponse", NS)
        }
        with self.lock:
            self.sub_id, self.tokens = sub_id.text, tokens
        print(f"[{datetime.now():%H:%M:%S}] Registered with the printer: {', '.join(f'{self.name} {SOURCES[c][1]}' for c in tokens)}")

    def renew_loop(self) -> None:
        while True:
            time.sleep(RENEW_EVERY)
            hdr = f"<wse:Identifier>{self.sub_id}</wse:Identifier>"
            try:
                _, resp = post(self.url, envelope(self.url, f"{NS['wse']}/Renew",
                                                  "<wse:Renew><wse:Expires>PT1H</wse:Expires></wse:Renew>", hdr), "renew")
                if b"RenewResponse" in resp:
                    continue
            except Exception as e:
                print(f"Renew failed: {e}", file=sys.stderr)
            # Printer rebooted or subscription expired -> subscribe again (tokens change)
            try:
                self.subscribe()
            except Exception as e:
                print(f"Re-subscribe failed, retrying in 10 minutes: {e}", file=sys.stderr)

    def unsubscribe(self) -> None:
        if self.sub_id:
            hdr = f"<wse:Identifier>{self.sub_id}</wse:Identifier>"
            post(self.url, envelope(self.url, f"{NS['wse']}/Unsubscribe", "<wse:Unsubscribe/>", hdr), "unsubscribe")

    # ---- scanning ----
    def ticket(self, ctx: str) -> str:
        source, _, images = SOURCES[ctx]
        side = (
            f"<wscn:ScanRegion><wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset><wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset>"
            f"<wscn:ScanRegionWidth>{A4_W}</wscn:ScanRegionWidth><wscn:ScanRegionHeight>{A4_H}</wscn:ScanRegionHeight></wscn:ScanRegion>"
            f"<wscn:ColorProcessing>RGB24</wscn:ColorProcessing>"
            f"<wscn:Resolution><wscn:Width>{self.dpi}</wscn:Width><wscn:Height>{self.dpi}</wscn:Height></wscn:Resolution>"
        )
        # Values from the printer's own DefaultScanTicket / ScannerConfiguration (exif = JPEG, quality fixed at 50, scaling fixed at 100)
        return (
            "<wscn:ScanTicket><wscn:JobDescription><wscn:JobName>scan</wscn:JobName>"
            f"<wscn:JobOriginatingUserName>{escape(self.name)}</wscn:JobOriginatingUserName></wscn:JobDescription>"
            "<wscn:DocumentParameters><wscn:Format>exif</wscn:Format><wscn:CompressionQualityFactor>50</wscn:CompressionQualityFactor>"
            f"<wscn:ImagesToTransfer>{images}</wscn:ImagesToTransfer><wscn:InputSource>{source}</wscn:InputSource>"
            "<wscn:ContentType>Text</wscn:ContentType>"
            f"<wscn:InputSize><wscn:InputMediaSize><wscn:Width>{A4_W}</wscn:Width><wscn:Height>{A4_H}</wscn:Height></wscn:InputMediaSize></wscn:InputSize>"
            "<wscn:Exposure><wscn:ExposureSettings><wscn:Contrast>0</wscn:Contrast><wscn:Brightness>0</wscn:Brightness>"
            "<wscn:Sharpness>0</wscn:Sharpness></wscn:ExposureSettings></wscn:Exposure>"
            "<wscn:Scaling><wscn:ScalingWidth>100</wscn:ScalingWidth><wscn:ScalingHeight>100</wscn:ScalingHeight></wscn:Scaling>"
            f"<wscn:Rotation>0</wscn:Rotation><wscn:MediaSides><wscn:MediaFront>{side}</wscn:MediaFront></wscn:MediaSides>"
            "</wscn:DocumentParameters></wscn:ScanTicket>"
        )

    def handle_event(self, ctx: str, scan_id: str) -> None:
        with self.lock:
            token = self.tokens.get(ctx)
        if ctx not in SOURCES or not token:
            print(f"Scan event for an unknown destination: {ctx}", file=sys.stderr)
            return
        body = (f"<wscn:CreateScanJobRequest><wscn:ScanIdentifier>{escape(scan_id)}</wscn:ScanIdentifier>"
                f"<wscn:DestinationToken>{escape(token)}</wscn:DestinationToken>{self.ticket(ctx)}</wscn:CreateScanJobRequest>")
        _, resp = post(self.url, envelope(self.url, f"{SCAN}/CreateScanJob", body), "create")
        job_id, job_token = find(resp, ".//wscn:JobId"), find(resp, ".//wscn:JobToken")
        if not job_id:
            print(f"CreateScanJob failed: {find(resp, './/soap:Reason/soap:Text') or resp[:300]!r}", file=sys.stderr)
            return
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        saved = []
        while True:
            body = (f"<wscn:RetrieveImageRequest><wscn:JobId>{job_id}</wscn:JobId><wscn:JobToken>{escape(job_token)}</wscn:JobToken>"
                    f"<wscn:DocumentDescription><wscn:DocumentName>{stamp}</wscn:DocumentName></wscn:DocumentDescription></wscn:RetrieveImageRequest>")
            ctype, resp = post(self.url, envelope(self.url, f"{SCAN}/RetrieveImage", body), f"retrieve{len(saved) + 1}")
            image = extract_image(ctype, resp)
            if image is None:  # no next page (fault) = end of this job
                break
            path = self.out / (f"scan-{stamp}.jpg" if SOURCES[ctx][2] == 1 else f"scan-{stamp}-{len(saved) + 1}.jpg")
            path.write_bytes(image)
            saved.append(path)
            print(f"  saved {path}")
            if SOURCES[ctx][2] == 1:
                break
        if saved:
            notify(f"Saved {len(saved)} page(s) to {self.out.name}", saved[0].name)
        else:
            print("No image received for this scan", file=sys.stderr)


def extract_image(ctype: str, body: bytes) -> bytes | None:
    """RetrieveImage answers with MTOM (multipart/related); the image is the part that is not xop+xml."""
    if "multipart" not in ctype.lower():
        return None
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    for part in msg.iter_parts():
        if "xml" not in part.get_content_type():
            return part.get_payload(decode=True)
    return None


def notify(title: str, text: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'], check=False)


def read_body(handler: http.server.BaseHTTPRequestHandler) -> bytes:
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        data = b""
        while (size := int(handler.rfile.readline().split(b";")[0], 16)):
            data += handler.rfile.read(size)
            handler.rfile.readline()
        handler.rfile.readline()
        return data
    return handler.rfile.read(int(handler.headers.get("Content-Length", 0)))


def make_handler(scanner: Scanner):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = read_body(self)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            debug("event.xml", body)
            if b"ScanAvailableEvent" not in body:
                return
            ctx, scan_id = find(body, ".//wscn:ClientContext"), find(body, ".//wscn:ScanIdentifier")
            print(f"[{datetime.now():%H:%M:%S}] Start pressed on the panel ({SOURCES.get(ctx, ('', ctx))[1]})")
            threading.Thread(target=scanner.handle_event, args=(ctx, scan_id), daemon=True).start()

        def log_message(self, fmt, *args):
            pass

    return Handler


def computer_name() -> str:
    r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True)
    return r.stdout.strip() or socket.gethostname()


def main() -> None:
    global log_dir
    ap = argparse.ArgumentParser(description='Epson panel "Scan -> Computer (WSD)" listener that saves JPEGs to a folder')
    ap.add_argument("--printer", default=os.environ.get("EPSON_IP"), help="printer IP (or set EPSON_IP)")
    ap.add_argument("--name", default=computer_name(), help="name shown on the panel (default: computer name)")
    ap.add_argument("--out", type=Path, default=Path.home() / "Documents" / "Scans", help="output folder")
    ap.add_argument("--dpi", type=int, choices=[100, 300], default=300, help="resolution (the printers tested only support 100/300)")
    ap.add_argument("--debug", action="store_true", help="save every SOAP message to <out>/.wsd-log/")
    args = ap.parse_args()
    if not args.printer:
        ap.error("pass --printer <IP> or set EPSON_IP")
    sys.stdout.reconfigure(line_buffering=True)

    args.out = args.out.expanduser()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.debug:
        log_dir = args.out / ".wsd-log"
        log_dir.mkdir(exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((args.printer, 9))
        my_ip = s.getsockname()[0]
    server = http.server.ThreadingHTTPServer(("", PORT), None)
    scanner = Scanner(f"http://{args.printer}:80/WDP/SCAN", f"http://{my_ip}:{PORT}/wsd-scan",
                      args.name, args.out, args.dpi)
    server.RequestHandlerClass = make_handler(scanner)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    signal.signal(signal.SIGTERM, signal.default_int_handler)
    scanner.subscribe()
    threading.Thread(target=scanner.renew_loop, daemon=True).start()
    print(f"Waiting for scans from the panel, saving to {args.out}. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scanner.unsubscribe()
        except Exception:
            pass
        server.shutdown()


if __name__ == "__main__":
    main()
