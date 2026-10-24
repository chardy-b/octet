"""Minimal UPnP/DLNA control: find a MediaRenderer, serve a WAV over HTTP, tell the renderer to play it."""
import re
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


@dataclass(frozen=True)
class Renderer:
    name: str
    model: str
    ip: str
    location: str
    control_url: str


def _local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["0.0.0.0"]


def local_ip_for(remote_ip: str) -> str:
    """The local address that routes to remote_ip."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_ip, 9))
        return s.getsockname()[0]


def _describe(location: str) -> Renderer | None:
    try:
        xml = urllib.request.urlopen(location, timeout=4).read()
    except OSError:
        return None
    root = ET.fromstring(xml)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    dev = root.find("d:device", ns)
    if dev is None:
        return None
    ctl = None
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        if svc.findtext("d:serviceType", namespaces=ns) == AVT:
            ctl = urljoin(location, svc.findtext("d:controlURL", namespaces=ns))
    if ctl is None:
        return None
    return Renderer(name=dev.findtext("d:friendlyName", "?", ns),
                    model=dev.findtext("d:modelName", "", ns),
                    ip=urlparse(location).hostname, location=location, control_url=ctl)


def discover(timeout: float = 3.0, bind_ips: list[str] | None = None) -> list[Renderer]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: 2\r\nST: {RENDERER}\r\n\r\n").encode()
    locations: dict[str, None] = {}
    for ip in bind_ips or _local_ips():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            s.settimeout(0.5)
            for _ in range(2):
                s.sendto(msg, SSDP_ADDR)
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, _ = s.recvfrom(4096)
                except socket.timeout:
                    continue
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    locations[m.group(1).decode()] = None
        except OSError:
            pass
        finally:
            s.close()
    found, seen = [], set()
    for loc in locations:
        r = _describe(loc)
        if r and r.control_url not in seen:
            seen.add(r.control_url)
            found.append(r)
    return found


def find_renderer(query: str, timeout: float = 3.0) -> Renderer:
    """Match by IP address or by part of the friendly/model name."""
    rs = discover(timeout)
    q = query.lower()
    hits = [r for r in rs if q == r.ip or q in r.name.lower() or q in r.model.lower()]
    if not hits:
        names = ", ".join(f"{r.name} ({r.ip})" for r in rs) or "none found"
        raise RuntimeError(f"No renderer matching {query!r}. Found: {names}")
    return hits[0]


def _soap(r: Renderer, action: str, args: dict[str, str]) -> str:
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{AVT}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(r.control_url, env, {
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{AVT}#{action}"'})
    try:
        return urllib.request.urlopen(req, timeout=10).read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{action} failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from e


def _didl(url: str, title: str) -> str:
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<res protocolInfo="http-get:*:audio/wav:*">{escape(url)}</res></item></DIDL-Lite>')


def play(r: Renderer, url: str, title: str = "Measurement sweep") -> None:
    try:
        _soap(r, "Stop", {"InstanceID": "0"})
    except RuntimeError:
        pass  # nothing was playing
    _soap(r, "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": url, "CurrentURIMetaData": _didl(url, title)})
    _soap(r, "Play", {"InstanceID": "0", "Speed": "1"})


def stop(r: Renderer) -> None:
    try:
        _soap(r, "Stop", {"InstanceID": "0"})
    except RuntimeError:
        pass


def transport_state(r: Renderer) -> str:
    out = _soap(r, "GetTransportInfo", {"InstanceID": "0"})
    m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>", out)
    return m.group(1) if m else "UNKNOWN"


class FileServer:
    """Serves one file over HTTP with Range support (renderers often probe with Range requests)."""

    def __init__(self, path: Path, bind_ip: str = "0.0.0.0"):
        self.path = Path(path)
        self.requests: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, head: bool):
                size = outer.path.stat().st_size
                start, end, code = 0, size - 1, 200
                rng = self.headers.get("Range")
                outer.requests.append(f"{'HEAD' if head else 'GET'} {rng or ''}".strip())
                if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
                    a, b = m.groups()
                    if a:
                        start = int(a)
                        end = min(int(b), size - 1) if b else size - 1
                    elif b:
                        start = max(0, size - int(b))
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("transferMode.dlna.org", "Streaming")
                self.send_header("Content-Length", str(end - start + 1))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                try:
                    with open(outer.path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass

            def do_GET(self):
                self._serve(False)

            def do_HEAD(self):
                self._serve(True)

        self._srv = ThreadingHTTPServer((bind_ip, 0), H)
        self.url = f"http://{bind_ip}:{self._srv.server_port}/{self.path.name}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
