"""ONVIF client (device + media services over SOAP).

Why this exists: WS-Discovery only tells you that a device is there and where
its ``device_service`` lives.  The *stream* address (and the profile tokens it
needs) can only be obtained through the ONVIF WSDL services:

    GetDeviceInformation  -> manufacturer / model / firmware  (device service)
    GetCapabilities       -> where the media service lives     (device service)
    GetProfiles           -> profile tokens                    (media service)
    GetStreamUri          -> rtsp://...?token=...              (media service)

The previous implementation guessed ``/media_service`` and sent a profile
token called ``main`` with WS-Security ``PasswordDigest``; most cameras answer
HTTP 400 to that, so discovery appeared to "find" nothing usable.  This client
implements the real messages and supports both WS-Security digest and
HTTP digest, which is what ONVIF cameras actually accept.

Nothing here is mandatory: every failure returns ``None``/``""`` and the
caller falls back to the compiled profile path.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from ..core.logging_setup import get_logger

log = get_logger("onvif")

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)

MEDIA_NS = (
    "http://www.onvif.org/ver10/media/wsdl",
    "http://www.onvif.org/ver20/media/wsdl",
)
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"

NS = {
    "s": SOAP_ENV,
    "tds": _DEVICE_NS,
    "trt": MEDIA_NS[0],
    "tr2": MEDIA_NS[1],
    "tt": "http://www.onvif.org/ver10/schema",
}


@dataclass
class OnvifDeviceInfo:
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    hardware_id: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "firmware": self.firmware,
            "serial": self.serial,
            "hardware_id": self.hardware_id,
        }


@dataclass
class OnvifProfile:
    token: str = ""
    name: str = ""
    resolution: Tuple[int, int] = (0, 0)
    encoding: str = ""
    uri: str = ""

    def as_dict(self) -> Dict:
        return {
            "token": self.token,
            "name": self.name,
            "encoding": self.encoding,
            "width": self.resolution[0],
            "height": self.resolution[1],
            "uri": self.uri,
            "label": f"{self.name or self.token} ({self.resolution[0]}x{self.resolution[1]})"
            if self.resolution[0]
            else (self.name or self.token),
        }


def _envelope(body: str, auth_xml: str = "") -> bytes:
    header = f"<s:Header>{auth_xml}</s:Header>" if auth_xml else "<s:Header/>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" xmlns:tds="{_DEVICE_NS}" '
        f'xmlns:trt="{MEDIA_NS[0]}" xmlns:tr2="{MEDIA_NS[1]}" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    ).encode("utf-8")


def _wsse(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        f'<Security xmlns="{WSSE}" s:mustUnderstand="0">'
        "<UsernameToken>"
        f"<Username>{_xml_escape(username)}</Username>"
        f'<Password Type="{PASSWORD_DIGEST}">{digest}</Password>'
        f'<Nonce EncodingType="{BASE64_BINARY}">{nonce_b64}</Nonce>'
        f'<Created xmlns="{WSU}">{created}</Created>'
        "</UsernameToken></Security>"
    )


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    out = []
    for el in root.iter():
        if _local(el.tag) == name:
            out.append(el)
    return out


def _text(root: ET.Element, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name:
            return (el.text or "").strip()
    return ""


def _same_origin(url: str) -> Tuple[str, str, int]:
    p = urlsplit(url)
    scheme = (p.scheme or "http").lower()
    port = p.port or (443 if scheme == "https" else 80)
    return scheme, p.hostname or "", port


def _replace_path(url: str, path: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def _soap_post(url: str, payload: bytes, username: str, password: str, timeout: float) -> Optional[str]:
    """POST a SOAP envelope, retrying with HTTP digest on 401."""
    from urllib.error import HTTPError
    from urllib.request import HTTPDigestAuthHandler, HTTPSHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener

    origin = _same_origin(url)
    pmgr = HTTPPasswordMgrWithDefaultRealm()
    if username:
        pmgr.add_password(None, f"{origin[0]}://{origin[1]}:{origin[2]}", username, password)
    opener = build_opener(HTTPDigestAuthHandler(pmgr), HTTPSHandler())
    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{url}"',
            "Accept": "application/soap+xml, application/xml",
            "User-Agent": "FACW-ONVIF/1.0",
        },
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - operator LAN endpoint
            return resp.read().decode("utf-8", "ignore")
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:400]
        except Exception:  # noqa: BLE001
            pass
        log.info("onvif http error", extra={"url": url, "status": exc.code, "body": body})
        return None
    except Exception as exc:  # noqa: BLE001 - network/protocol fault: degrade quietly
        log.info("onvif request failed", extra={"url": url, "error": str(exc)})
        return None


def _parse(soap: Optional[str]) -> Optional[ET.Element]:
    if not soap:
        return None
    try:
        return ET.fromstring(soap)
    except ET.ParseError as exc:
        log.info("onvif xml parse failed", extra={"error": str(exc)})
        return None


class OnvifClient:
    """Minimal ONVIF device+media client for one camera."""

    def __init__(self, device_url: str, username: str = "", password: str = "", timeout: float = 5.0) -> None:
        self.device_url = (device_url or "").strip()
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.media_url = ""
        self._errors: List[str] = []

    # -- transport ------------------------------------------------------
    def _call(self, url: str, body: str) -> Optional[ET.Element]:
        auth = _wsse(self.username, self.password) if self.username else ""
        soap = _soap_post(url, _envelope(body, auth), self.username, self.password, self.timeout)
        return _parse(soap)

    def _try_media_urls(self) -> List[str]:
        urls = []
        if self.media_url:
            urls.append(self.media_url)
        base = self.device_url
        for suffix in ("/onvif/media_service", "/onvif/media2_service", "/onvif/Media", "/media_service"):
            urls.append(_replace_path(base, suffix))
        # last resort: same endpoint (some firmware multiplexes services)
        urls.append(base)
        seen = set()
        out = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # -- device service -------------------------------------------------
    def get_device_information(self) -> Optional[OnvifDeviceInfo]:
        root = self._call(self.device_url, "<tds:GetDeviceInformation/>")
        if root is None:
            return None
        info = OnvifDeviceInfo(
            manufacturer=_text(root, "Manufacturer"),
            model=_text(root, "Model"),
            firmware=_text(root, "FirmwareVersion"),
            serial=_text(root, "SerialNumber"),
            hardware_id=_text(root, "HardwareId"),
        )
        if not (info.manufacturer or info.model):
            return None
        return info

    def probe(self) -> bool:
        """True when the endpoint speaks ONVIF at all."""
        root = self._call(self.device_url, "<tds:GetSystemDateAndTime/>")
        return root is not None

    def discover_media_url(self) -> str:
        """Ask the device where its media service is (WSDL capability)."""
        root = self._call(self.device_url, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>")
        if root is None:
            root = self._call(self.device_url, "<tds:GetCapabilities/>")
        if root is not None:
            for el in root.iter():
                if _local(el.tag) == "XAddr" and el.text:
                    self.media_url = el.text.strip()
                    return self.media_url
        return ""

    # -- media service --------------------------------------------------
    def get_profiles(self) -> List[OnvifProfile]:
        profiles: List[OnvifProfile] = []
        for url in self._try_media_urls():
            root = self._call(url, "<trt:GetProfiles/>")
            if root is None:
                # some cameras only implement the v2 media service
                root = self._call(url, "<tr2:GetProfiles/>")
            if root is None:
                continue
            for prof in _find_all(root, "Profiles"):
                token = prof.attrib.get("token", "")
                name = ""
                width = height = 0
                encoding = ""
                for el in prof.iter():
                    tag = _local(el.tag)
                    if tag == "Name" and not name:
                        name = (el.text or "").strip()
                    elif tag == "Encoding" and not encoding:
                        encoding = (el.text or "").strip()
                    elif tag == "Width":
                        try:
                            width = int((el.text or "0").strip())
                        except ValueError:
                            width = 0
                    elif tag == "Height":
                        try:
                            height = int((el.text or "0").strip())
                        except ValueError:
                            height = 0
                if token:
                    profiles.append(OnvifProfile(token=token, name=name, resolution=(width, height), encoding=encoding))
            if profiles:
                self.media_url = url
                return profiles
        return profiles

    def get_stream_uri(self, profile_token: str, media_url: str = "") -> str:
        url = media_url or self.media_url or (self._try_media_urls()[0] if self._try_media_urls() else "")
        if not url:
            return ""
        body = (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{_xml_escape(profile_token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        root = self._call(url, body)
        if root is not None:
            for el in root.iter():
                if _local(el.tag) == "Uri" and el.text:
                    return el.text.strip()
        # v2 media service variant
        body2 = (
            "<tr2:GetStreamUri>"
            "<tr2:Protocol>RTSP</tr2:Protocol>"
            f"<tr2:ProfileToken>{_xml_escape(profile_token)}</tr2:ProfileToken>"
            "</tr2:GetStreamUri>"
        )
        root = self._call(url, body2)
        if root is not None:
            for el in root.iter():
                if _local(el.tag) == "Uri" and el.text:
                    return el.text.strip()
        return ""

    # -- one call from the console --------------------------------------
    def describe(self, with_streams: bool = True) -> Dict:
        """Everything the console needs: identity, media URL, profiles."""
        result: Dict = {"device_url": self.device_url, "media_url": "", "info": {}, "profiles": [], "errors": []}
        info = self.get_device_information()
        if info is not None:
            result["info"] = info.as_dict()
        media = self.discover_media_url()
        if not media:
            self._errors.append("media service address not advertised")
        profiles: List[OnvifProfile] = self.get_profiles() if with_streams else []
        if with_streams and not profiles:
            self._errors.append("no media profiles returned (check credentials)")
        for prof in profiles:
            if with_streams:
                prof.uri = self.get_stream_uri(prof.token)
        result["media_url"] = self.media_url
        result["profiles"] = [p.as_dict() for p in profiles]
        result["errors"] = list(self._errors)
        return result


def probe_onvif(device_url: str, username: str = "", password: str = "", timeout: float = 5.0) -> Dict:
    """Ad-hoc describe used by the discovery panel and the camera form."""
    client = OnvifClient(device_url, username, password, timeout)
    return client.describe()


def guess_stream_paths(vendor: str = "", model: str = "") -> List[str]:
    """Common RTSP paths, best guess first, for cameras without ONVIF.

    Kept as a *fallback*: if ONVIF answers, its GetStreamUri is always used.
    """
    text = f"{vendor} {model}".lower()
    common = ["/stream1", "/Streaming/Channels/101", "/live", "/h264", "/11", "/live/ch0"]
    if any(k in text for k in ("hikvision", "hilook", "lts")):
        return ["/Streaming/Channels/101", "/Streaming/Channels/102"] + common
    if any(k in text for k in ("dahua", "amcrest", "imou", "lechange")):
        return ["/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"] + common
    if any(k in text for k in ("axis",)):
        return ["/axis-media/media.amp", "/axis-media/media.amp?videocodec=h264"] + common
    if any(k in text for k in ("reolink",)):
        return ["/h264Preview_01_main", "/h264Preview_01_sub"] + common
    if any(k in text for k in ("tvt", "uniview", "unv", "xiongmai", "xm")):
        return ["/media/video1", "/media/video2"] + common
    return common


def build_rtsp(host: str, path: str, username: str = "", password: str = "", port: int = 554) -> str:
    from urllib.parse import quote

    cred = ""
    if username:
        cred = quote(str(username), safe="")
        if password:
            cred += ":" + quote(str(password), safe="")
        cred += "@"
    path = path if path.startswith("/") else "/" + path
    return f"rtsp://{cred}{host}:{int(port or 554)}{path}"
