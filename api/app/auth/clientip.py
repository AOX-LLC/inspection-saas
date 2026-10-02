"""The client's address, with `X-Forwarded-For` trusted only from a configured proxy.

Any client can send `X-Forwarded-For`, so the header means something only when
the request came from a proxy we run. `TRUSTED_PROXIES` lists those: IP
addresses, CIDR ranges or host names (a Compose service name resolves to the
container's address, which is not fixed). A request whose socket address is not
listed keeps that address, whatever the header says. Nothing is trusted by default.

For a request from a trusted proxy the header is read from the right. Each proxy
appends the address it saw, so the rightmost entries are the ones our own proxies
wrote; entries further left are whatever the client or earlier hops claimed. The
client is the first entry from the right that is not itself a trusted proxy.
"""

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# How long a resolved host name is believed. Short, because a restarted container may move.
RESOLVE_TTL_SECONDS = 30
MAX_FORWARDED_ENTRIES = 20


def _parse_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


@dataclass
class TrustedProxies:
    networks: list[IPNetwork] = field(default_factory=list)
    hostnames: list[str] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    _resolved: tuple[float, frozenset[IPAddress]] | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, setting: str, **kwargs) -> "TrustedProxies":
        """From the comma-separated setting. An entry that is no address or range is a host name."""
        proxies = cls(**kwargs)
        for entry in (e.strip() for e in setting.split(",")):
            if not entry:
                continue
            try:
                proxies.networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                proxies.hostnames.append(entry)
        return proxies

    @property
    def configured(self) -> bool:
        return bool(self.networks or self.hostnames)

    async def trusts(self, address: IPAddress) -> bool:
        if any(address in network for network in self.networks):
            return True
        return bool(self.hostnames) and address in await self._resolve_hostnames()

    async def _resolve_hostnames(self) -> frozenset[IPAddress]:
        now = self.clock()
        if self._resolved is not None and now - self._resolved[0] < RESOLVE_TTL_SECONDS:
            return self._resolved[1]
        found: set[IPAddress] = set()
        for name in self.hostnames:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    name, None, type=socket.SOCK_STREAM
                )
            except OSError:
                # Unresolvable means untrusted: failing open would trust every forwarded header.
                logger.warning("trusted proxy %r did not resolve", name)
                continue
            found.update(ip for info in infos if (ip := _parse_ip(info[4][0])) is not None)
        self._resolved = (now, frozenset(found))
        return self._resolved[1]


async def client_ip(request: Request, proxies: TrustedProxies) -> str:
    """The address to rate-limit and log: the socket's, or a trusted proxy's report of it."""
    if request.client is None:
        return "unknown"
    socket_address = request.client.host
    peer = _parse_ip(socket_address)
    if peer is None or not proxies.configured or not await proxies.trusts(peer):
        return socket_address

    entries = [
        part.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for part in header.split(",")
    ]
    # A header this long is not from our proxy's append; nothing in it is believed.
    if not entries or len(entries) > MAX_FORWARDED_ENTRIES:
        return socket_address
    for entry in reversed(entries):
        address = _parse_ip(entry)
        if address is None:
            # An entry that is not an address means the header is malformed or hostile.
            return socket_address
        if not await proxies.trusts(address):
            return str(address)
    return socket_address
