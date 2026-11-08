"""Camera discovery (spec 3.2).

Two complementary probes, both streaming results as they are found:

1. **WS-Discovery** (ONVIF) - multicast UDP probe on 239.255.255.250:3702.
   The default per-device price is small and most cameras answer.  This is the
   primary path because it yields the device's XAddrs (management endpoint),
   from which the stream URL can be requested over ONVIF.
2. **TCP sweep** - a cancellable scan of the local /24 for the common camera
   ports (554 RTSP, 80/8080 HTTP).  Catches cameras that do not speak WS-D.

The scan runs in its own thread, never blocks the service, is cancellable, and
is rate-limited (spec 9.3).  Results are pushed to a callback as they arrive
so the console updates while the scan is still running.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from xml.etree import ElementTree as ET

from ..core.logging_setup import get_logger

log = get_logger("discovery")

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
COMMON_PORTS = (554, 80, 8080)


@dataclass
class DiscoveredDevice:
    address: str
    port: int = 554
    name: str = ""
    vendor: str = ""
    model: str = ""
    stream_address: str = ""
    service_url: str = ""        # ONVIF device service endpoint
    sources: List[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> Dict:
        return {
            "address": self.address,
            "port": self.port,
            "name": self.name or f"camera@{self.address}",
            "vendor": self.vendor,
            "model": self.model,
            "stream_address": self.stream_address,
            "service_url": self.service_url,
            "sources": self.sources,
            "note": self.note,
        }


def local_ipv4_networks() -> List[str]:
    """Best-effort list of the host's /24 subnets (no external deps)."""
    ips: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent for UDP
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        host = socket.gethostbyname_ex(socket.gethostname())[2]
        ips.extend(host)
    except OSError:
        pass
    nets = []
    for ip in dict.fromkeys(ips):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        parts = ip.split(".")
        nets.append(".".join(parts[:3]) + ".0/24")
    return nets


def _extract(tag: str, xml: str) -> str:
    m = re.search(rf"<(?:\w+:)?{tag}[^>]*>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


class DiscoveryScanner:
    """Cancellable, rate-limited, streaming scanner."""

    def __init__(self, rate_limit_seconds: float = 10.0) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._devices: Dict[str, DiscoveredDevice] = {}
        self._on_result: Optional[Callable[[Dict], None]] = None
        self._on_done: Optional[Callable[[int], None]] = None
        self._last_run = 0.0
        self._rate_limit = rate_limit_seconds
        self.running = False
        self.progress = "idle"

    # -- lifecycle -----------------------------------------------------
    def start(
        self,
        on_result: Callable[[Dict], None],
        on_done: Optional[Callable[[int], None]] = None,
        subnets: Optional[List[str]] = None,
        timeout: float = 4.0,
    ) -> bool:
        with self._lock:
            if self.running:
                return False
            if self._last_run and (time.time() - self._last_run) < self._rate_limit:
                wait = self._rate_limit - (time.time() - self._last_run)
                self.progress = f"rate limited - retry in {wait:.0f}s"
                return False
            self.running = True
            self._cancel.clear()
            self._devices.clear()
            self._on_result = on_result
            self._on_done = on_done
            self._last_run = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(subnets, timeout), name="discovery", daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def stop(self) -> None:
        self.cancel()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)

    @property
    def devices(self) -> List[Dict]:
        with self._lock:
            return [d.as_dict() for d in self._devices.values()]

    # -- worker --------------------------------------------------------
    def _emit(self, dev: DiscoveredDevice) -> None:
        with self._lock:
            key = dev.address
            if key in self._devices:
                # merge: keep the richest information
                existing = self._devices[key]
                existing.sources = list(dict.fromkeys(existing.sources + dev.sources))
                if not existing.stream_address and dev.stream_address:
                    existing.stream_address = dev.stream_address
                if not existing.service_url and dev.service_url:
                    existing.service_url = dev.service_url
                if not existing.vendor:
                    existing.vendor = dev.vendor
                if not existing.model:
                    existing.model = dev.model
                dev = existing
            else:
                self._devices[key] = dev
        if self._on_result:
            try:
                self._on_result(dev.as_dict())
            except Exception:
                pass

    def _run(self, subnets: Optional[List[str]], timeout: float) -> None:
        try:
            nets = subnets or local_ipv4_networks()
            self.progress = f"scanning {', '.join(nets) or 'local subnet'}"
            ws = threading.Thread(target=self._ws_discovery, args=(timeout,), daemon=True)
            ws.start()
            for net in nets or []:
                if self._cancel.is_set():
                    break
                self._tcp_sweep(net, timeout)
            ws.join(timeout=max(1.0, timeout))
        except Exception as exc:  # noqa: BLE001 - discovery must never crash the service
            log.error("discovery failed", extra={"error": str(exc)})
        finally:
            with self._lock:
                self.running = False
            self.progress = "done"
            if self._on_done:
                try:
                    self._on_done(len(self._devices))
                except Exception:
                    pass

    def _ws_discovery(self, timeout: float) -> None:
        probe = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
            ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
            ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            "<e:Header>"
            "<w:MessageID>uuid:facw-discovery</w:MessageID>"
            "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
            "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
            "</e:Header>"
            "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
            "</e:Envelope>"
        ).encode()
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.5)
            sock.sendto(probe, WS_DISCOVERY_ADDR)
        except OSError as exc:
            log.info("ws-discovery unavailable", extra={"error": str(exc)})
            if sock:
                sock.close()
            return

        deadline = time.time() + timeout
        while time.time() < deadline and not self._cancel.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                xml = data.decode("utf-8", "ignore")
            except Exception:
                continue
            if "ProbeMatches" not in xml and "ProbeMatch" not in xml:
                continue
            xaddrs = _extract("XAddrs", xml).split()
            service_url = xaddrs[0] if xaddrs else ""
            dev = DiscoveredDevice(
                address=addr[0],
                name=_extract("Name", xml) or _extract("Scopes", xml).split()[-1].split("/")[-1]
                if _extract("Scopes", xml) else f"camera@{addr[0]}",
                service_url=service_url,
                sources=["ws-discovery"],
            )
            m = re.search(r"onvif://www\.onvif\.org/name/([^ ]+)", xml)
            if m:
                dev.name = m.group(1).replace("_", " ")
            v = re.search(r"onvif://www\.onvif\.org/hardware/([^ ]+)", xml)
            if v:
                dev.model = v.group(1)
            self._emit(dev)
        sock.close()

    def _tcp_sweep(self, subnet: str, timeout: float) -> None:
        try:
            net = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            return
        hosts = list(net.hosts())[:254]
        self.progress = f"scanning {subnet}"

        def check(ip: str, port: int) -> Optional[tuple]:
            if self._cancel.is_set():
                return None
            try:
                with socket.create_connection((ip, port), timeout=0.25):
                    return ip, port
            except OSError:
                return None

        results: List[tuple] = []
        results_lock = threading.Lock()

        def worker(chunk: List[str]) -> None:
            for ip in chunk:
                if self._cancel.is_set():
                    return
                for port in COMMON_PORTS:
                    got = check(ip, port)
                    if got:
                        with results_lock:
                            results.append(got)

        threads = []
        chunk_size = max(8, len(hosts) // 24)
        for i in range(0, len(hosts), chunk_size):
            t = threading.Thread(target=worker, args=(hosts[i : i + chunk_size],), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=max(2.0, timeout))

        for ip, port in results:
            if self._cancel.is_set():
                return
            self._emit(
                DiscoveredDevice(
                    address=ip,
                    port=port if port == 554 else 554,
                    name=f"camera@{ip}",
                    sources=[f"tcp:{port}"],
                    note="open port detected - provide credentials to fetch the stream address",
                )
            )


def fetch_onvif_stream_url(service_url: str, username: str, password: str, timeout: float = 4.0) -> str:
    """Best-effort ONVIF ``GetStreamUri`` using the camera's own management API.

    Implemented with WS-Security UsernameToken digest over plain HTTP.  Returns
    the RTSP URL or "" when the device does not cooperate - the caller then
    falls back to the manually entered profile path.
    """
    if not service_url:
        return ""
    try:
        import base64
        import hashlib
        import os
        from urllib.request import Request, urlopen

        nonce = os.urandom(16)
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + password.encode()).digest()
        ).decode()
        nonce_b64 = base64.b64encode(nonce).decode()
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
    <Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <UsernameToken>
        <Username>{username}</Username>
        <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
        <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>
        <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
      </UsernameToken>
    </Security>
  </s:Header>
  <s:Body>
    <trt:GetStreamUri>
      <trt:StreamSetup>
        <tt:Stream>RTP-Unicast</tt:Stream>
        <tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
      </trt:StreamSetup>
      <trt:ProfileToken>main</trt:ProfileToken>
    </trt:GetStreamUri>
  </s:Body>
</s:Envelope>"""
        url = service_url.replace("/device_service", "/media_service")
        req = Request(url, data=envelope.encode(), headers={"Content-Type": "application/soap+xml"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-provided LAN endpoint
            body = resp.read().decode("utf-8", "ignore")
        m = re.search(r"<[^>]*Uri>\s*(rtsp://[^<\s]+)", body)
        return m.group(1) if m else ""
    except Exception as exc:  # noqa: BLE001 - discovery must degrade quietly
        log.info("onvif stream uri lookup failed", extra={"error": str(exc)})
        return ""
