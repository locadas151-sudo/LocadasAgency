"""Mailserver einer eigenen Domain finden, ohne das Passwort bei fremden Hostern auszuprobieren.

Reihenfolge (wie Thunderbird):
1. Mozilla-ISPDB (https://autoconfig.thunderbird.net/v1.1/{domain})
2. Autoconfig der Domain selbst (nur HTTPS)
3. MX-Eintrag der Domain → bekannter Anbieter (IONOS, STRATO, Microsoft, Google, ALL-INKL …)
4. Eigene Hostnamen der Domain (imap./mail.{domain}), nur wenn dort ein IMAP-Server auf Port 993 antwortet

Erst danach wird ein einziger Server mit dem Passwort angemeldet. Nur Standardbibliothek: die MX-Abfrage ist
ein kleiner DNS-Client über UDP.
"""
from __future__ import annotations

import logging
import random
import socket
import struct
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .providers import ALLINKL, GMAIL, HOSTEUROPE, IONOS, OUTLOOK, STRATO, Provider

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 6
DNS_TIMEOUT = 3
MAX_XML = 200_000


@dataclass
class ServerConfig:
    imap_host: str
    imap_port: int
    imap_ssl: bool          # True = SSL/TLS, False = STARTTLS (Pflicht)
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    username: str = ""      # leer = Mailadresse
    source: str = ""        # ispdb | domain | mx | own-host
    provider: Provider | None = None


# --- Autoconfig-XML ---------------------------------------------------------------

def _fetch(url: str) -> bytes | None:
    if not url.startswith("https://"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Locadas-Nachfasser/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (nur https)
            if resp.status != 200:
                return None
            return resp.read(MAX_XML + 1)[:MAX_XML]
    except Exception as exc:
        log.info("Autoconfig %s nicht abrufbar: %s", url, exc)
        return None


def _server(el, email: str) -> tuple[str, int, bool, str] | None:
    host = (el.findtext("hostname") or "").strip()
    try:
        port = int((el.findtext("port") or "0").strip())
    except ValueError:
        return None
    sock = (el.findtext("socketType") or "").strip().upper()
    if not host or not port or sock not in ("SSL", "STARTTLS"):
        return None  # Klartext nie
    user = (el.findtext("username") or "%EMAILADDRESS%").strip()
    local, _, domain = email.partition("@")
    user = user.replace("%EMAILADDRESS%", email).replace("%EMAILLOCALPART%", local).replace("%EMAILDOMAIN%", domain)
    return host, port, sock == "SSL", user


def parse_autoconfig(xml: bytes, email: str, source: str) -> ServerConfig | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    imap = smtp = None
    # SSL vor STARTTLS, in der Reihenfolge der Datei
    for el in root.iter("incomingServer"):
        if el.get("type") == "imap":
            cand = _server(el, email)
            if cand and (imap is None or (cand[2] and not imap[2])):
                imap = cand
    for el in root.iter("outgoingServer"):
        if el.get("type") == "smtp":
            cand = _server(el, email)
            if cand and (smtp is None or (cand[2] and not smtp[2])):
                smtp = cand
    if imap is None or smtp is None:
        return None
    user = imap[3] if imap[3] != email else ""
    return ServerConfig(imap[0], imap[1], imap[2], smtp[0], smtp[1], smtp[2], user, source)


def from_ispdb(domain: str, email: str) -> ServerConfig | None:
    xml = _fetch(f"https://autoconfig.thunderbird.net/v1.1/{domain}")
    return parse_autoconfig(xml, email, "ispdb") if xml else None


def from_domain(domain: str, email: str) -> ServerConfig | None:
    for url in (
        f"https://autoconfig.{domain}/mail/config-v1.1.xml?emailaddress={email}",
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    ):
        xml = _fetch(url)
        cfg = parse_autoconfig(xml, email, "domain") if xml else None
        if cfg:
            return cfg
    return None


# --- MX ---------------------------------------------------------------------------

def _nameservers() -> list[str]:
    out: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out.append(parts[1])
    except OSError:
        pass
    return out or ["1.1.1.1", "9.9.9.9"]


def build_mx_query(domain: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # Rekursion erwünscht, eine Frage
    qname = b"".join(struct.pack("B", len(p)) + p.encode("idna") for p in domain.strip(".").split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", 15, 1)  # MX, IN


def _read_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    labels: list[str] = []
    while True:
        if offset >= len(data) or depth > 20:
            raise ValueError("kaputter Name")
        length = data[offset]
        if length == 0:
            return ".".join(labels), offset + 1
        if length & 0xC0 == 0xC0:  # Kompression
            ptr = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            name, _ = _read_name(data, ptr, depth + 1)
            labels.append(name)
            return ".".join(l for l in labels if l), offset + 2
        labels.append(data[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length


def parse_mx_response(data: bytes, qid: int) -> list[tuple[int, str]]:
    if len(data) < 12:
        return []
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or not flags & 0x8000 or flags & 0x000F:
        return []
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    out: list[tuple[int, str]] = []
    for _ in range(an):
        _, off = _read_name(data, off)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 15:
            pref = struct.unpack(">H", data[off:off + 2])[0]
            host, _ = _read_name(data, off + 2)
            out.append((pref, host.lower().rstrip(".")))
        off += rdlen
    return sorted(out)


def mx_lookup(domain: str) -> list[str]:
    """MX-Hosts der Domain, nach Priorität. Leer bei Fehler."""
    for ns in _nameservers()[:2]:
        qid = random.randint(0, 0xFFFF)
        try:
            with socket.socket(socket.AF_INET6 if ":" in ns else socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(DNS_TIMEOUT)
                sock.sendto(build_mx_query(domain, qid), (ns, 53))
                data, _ = sock.recvfrom(4096)
            records = parse_mx_response(data, qid)
            if records:
                return [h for _, h in records]
        except (OSError, ValueError, struct.error) as exc:
            log.info("MX-Abfrage %s über %s fehlgeschlagen: %s", domain, ns, exc)
    return []


# MX-Endung → Anbieter, dessen Server dann gelten
MX_PROVIDERS: tuple[tuple[tuple[str, ...], Provider], ...] = (
    (("kundenserver.de", "ionos.de", "ionos.com", "1and1.com", "1and1.de", "ui-dns.de", "schlund.de"), IONOS),
    (("strato.de", "rzone.de"), STRATO),
    (("protection.outlook.com", "outlook.com", "office365.com"), OUTLOOK),
    (("google.com", "googlemail.com"), GMAIL),
    (("kasserver.com", "all-inkl.com"), ALLINKL),
    (("hosteurope.de",), HOSTEUROPE),
)


def provider_for_mx(hosts: list[str]) -> Provider | None:
    for h in hosts:
        h = h.lower().rstrip(".")
        for suffixes, provider in MX_PROVIDERS:
            if any(h == s or h.endswith("." + s) for s in suffixes):
                return provider
    return None


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=DNS_TIMEOUT):
            return True
    except OSError:
        return False


def from_own_host(domain: str) -> ServerConfig | None:
    """imap./mail.{domain}: gehört der Domain selbst. Nur ein TCP-Test, kein Login."""
    for ih, sh in ((f"imap.{domain}", f"smtp.{domain}"), (f"mail.{domain}", f"mail.{domain}")):
        if _port_open(ih, 993):
            return ServerConfig(ih, 993, True, sh, 465, True, "", "own-host")
    return None


def resolve(email: str) -> ServerConfig | None:
    """Genau eine Server-Konfiguration für eine eigene Domain, oder None."""
    domain = email.rsplit("@", 1)[-1].lower().strip()
    if not domain or "." not in domain:
        return None
    for fn in (from_ispdb, from_domain):
        cfg = fn(domain, email)
        if cfg:
            return cfg
    p = provider_for_mx(mx_lookup(domain))
    if p is not None:
        return ServerConfig(p.imap_host, p.imap_port, p.imap_ssl, p.smtp_host, p.smtp_port, p.smtp_ssl, "", "mx", p)
    return from_own_host(domain)
