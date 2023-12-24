from __future__ import annotations

import asyncio
from asyncio.events import AbstractEventLoop
import logging
import socket
from asyncio.protocols import DatagramProtocol
from asyncio.transports import DatagramTransport
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Type

from ..settings import settings

logger = logging.getLogger(__name__)

SSDP_BROADCAST_PORT = 1900
SSDP_BROADCAST_ADDR = "239.255.255.250"

SSDP_BROADCAST_PARAMS = [
    "M-SEARCH * HTTP/1.1",
    f"HOST: {SSDP_BROADCAST_ADDR}:{SSDP_BROADCAST_PORT}",
    'MAN: "ssdp:discover"',
    "MX: 10",
    "ST: ssdp:all",
    "",
    "",
]
SSDP_BROADCAST_MSG = "\r\n".join(SSDP_BROADCAST_PARAMS)


SEND_INTERVAL_SECS = 30


def get_protocol(discover: DlnaDiscover) -> Type[DatagramProtocol]:
    @dataclass
    class DlnaProtocol(DatagramProtocol):
        transport: DatagramTransport | None = None
        is_connected: bool = False

        def __post_init__(self):
            discover.protocol = self

        def connection_made(self, transport: DatagramTransport):
            self.transport = transport
            self.is_connected = True
            logging.info("dlna discover connected")
            asyncio.create_task(self.send_loop())

        async def send_loop(self):
            if not self.transport:
                raise Exception("transport not set")
            while self.is_connected:
                self.transport.sendto(
                    SSDP_BROADCAST_MSG.encode("UTF-8"),
                    (SSDP_BROADCAST_ADDR, SSDP_BROADCAST_PORT),
                )
                await asyncio.sleep(SEND_INTERVAL_SECS)

        def datagram_received(self, data: bytes, addr: tuple[str, int]):
            info = [a.split(":", 1) for a in data.decode("UTF-8").split("\r\n")[1:]]
            device = dict(
                [(a[0].strip().lower(), a[1].strip()) for a in info if len(a) >= 2]
            )
            asyncio.create_task(discover.on_new_device(device["location"]))

        def error_received(self, exc: Exception):
            logging.error("Error received:", exc)

        def connection_lost(self, exc: Exception | None):
            logging.info("Socket closed, stop the event loop")
            if exc:
                logging.error("Error received:", exc)
            self.is_connected = False
            self.transport = None

    return DlnaProtocol


@dataclass
class DlnaDiscover:
    new_device_callback: Callable[[str], Awaitable[None]]

    device_locations: list[str] = field(default_factory=list, init=False)
    protocol: DatagramProtocol | None = field(default=None, init=False)
    socket: socket.socket | None = field(default=None, init=False)

    async def on_new_device(self, location_url: str):
        if location_url not in self.device_locations:
            self.device_locations.append(location_url)
            await self.new_device_callback(location_url)

    def init_socket(self):
        self.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception as e:
            logging.warning("socket reuse failed %s", e)

        self.socket.bind(("", SSDP_BROADCAST_PORT + 10))
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        self.socket.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(SSDP_BROADCAST_ADDR) + socket.inet_aton("0.0.0.0"),
        )
        self.socket.setblocking(False)

    async def discover(self, loop: AbstractEventLoop | None = None):
        if settings.location_url is not None and len(settings.location_url) > 0:
            await self.on_new_device(settings.location_url)
            return
        self.init_socket()
        if loop is None:
            loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(get_protocol(self), sock=self.socket)
