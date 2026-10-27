# How the protocol was captured (and how to check another model)

Epson ships Event Manager for Windows as a working native program, so a Windows PC that already shows up under
**Scan → Computer** is the reference implementation. Recording its traffic with the printer is enough to see
the whole "Scan to Computer" handshake. No Wireshark is needed on Windows: the built-in `pktmon` does it.

## 1. Record on Windows

Prerequisites: Epson Event Manager and Epson ScanSmart installed, and the PC listed on the printer panel under
Scan → Computer.

In PowerShell **as Administrator** (replace the printer IP):

```powershell
pktmon filter remove
pktmon filter add EPSON -i 192.168.1.20
pktmon start --capture --pkt-size 0 --file-name C:\epson.etl
```

Then:

1. On the panel choose **Scan → Computer**, pick the PC, press Start, and let ScanSmart finish saving.
2. Optionally, quit `EEventManager.exe` in Task Manager and start Epson Event Manager again, to see whether
   anything is sent at startup. (On the L6270 nothing is: the printer asks on demand.)
3. Stop and convert:

```powershell
pktmon stop
pktmon etl2pcap C:\epson.etl --out C:\epson.pcapng
```

`--pkt-size 0` keeps whole packets. `pktmon` logs each packet once per network component it passes, so expect
duplicates; de-duplicate by IP packet contents when reading.

## 2. Read it

Wireshark display filter, or the same expression with `tcpdump -r epson.pcapng -A`:

```
udp.port == 2968 || tcp.port == 2968
```

Look for:

- UDP from the printer to `239.255.255.253:2968` containing `service:NetScanMonitor-agent` (SLPv2 AttrRqst),
  and the PC's unicast reply containing `(ClientName=…)`.
- A TCP connection from the printer to the PC's port 2968 carrying `POST /PushScan`, and the PC's `200 OK`.
- Then a long TCP session from the PC to the printer's port 1865 (the image data, ESC/I).

`docs/protocol.md` has the L6270's exact bytes to compare against. If your model sends a different service
name, scope (`PID xxxx`), `PushScanIDIn` or extra fields, please open an issue with the (anonymised) payloads.

## 3. The Mac side

Things that were found on the Mac rather than in the capture:

- ScanSmart's scripting dictionary: `/Applications/Epson Software/Epson ScanSmart.app/Contents/Resources/ScanSmart.sdef`
  (the `notify` command).
- ScanSmart's debug log: create a folder `_ScanManagerLog` on the Desktop, trigger a scan, read the log, then
  delete the folder. This is how the missing `jobinfo` argument showed up (`no jobinfo`).
- Panel button numbers: `ScanSmart.plist` inside the model's bundle under
  `/Library/Image Capture/Support/EPSON/Epson Scan 2/Models/`.
- The scanner name ScanSmart expects: `defaults read com.epson.scan.Epson-ScanSmart previousScanner`
  (`displayName`).
- Why Event Manager does not start: `file "/Applications/Epson Software/Event Manager.app/Contents/MacOS/"*`
  shows `x86_64`/`i386` only, and without Rosetta macOS reports `Bad CPU type in executable`.

## 4. Test `push_listener.py` without the printer

Send a printer-style AttrRqst from another terminal and check that the listener answers:

```python
import socket
req = bytes.fromhex("020600005c2000000000000b0002656e0000001c") + b"service:NetScanMonitor-agent" \
    + bytes.fromhex("0008") + b"PID 1181" + bytes.fromhex("001e") + b"ClientName,IPAddress,EventPort" + bytes(2)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(req, ("239.255.255.253", 2968))
print(s.recvfrom(2048))  # -> b'...(ClientName=<your Mac>),(IPAddress=…),(EventPort=2968)\x00'
```
