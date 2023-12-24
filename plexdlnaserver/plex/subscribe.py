from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..dlna import devices, get_device_by_uuid
from .adapters import adapter_by_device
from ..settings import settings
from ..utils import g, pms_header, subscriber_send_headers

if TYPE_CHECKING:
    from ..dlna.dlna_device import DlnaDevice

logger = logging.getLogger(__name__)

TIMELINE_STOPPED = (
    '<MediaContainer commandID="{command_id}">'
    '<Timeline type="music" state="stopped"/>'
    '<Timeline type="video" state="stopped"/>'
    '<Timeline type="photo" state="stopped"/>'
    "</MediaContainer>"
)


TIMELINE_DISCONNECTED = (
    '<MediaContainer commandID="{command_id}" disconnected="1">'
    '<Timeline type="music" state="stopped"/>'
    '<Timeline type="video" state="stopped"/>'
    '<Timeline type="photo" state="stopped"/>'
    "</MediaContainer>"
)


CONTROLLABLE = "playPause,stop,volume,shuffle,repeat,seekTo,skipPrevious,skipNext,stepBack,stepForward"

TIMELINE_PLAYING = (
    '<MediaContainer commandID="{command_id}"><Timeline controllable="'
    + CONTROLLABLE
    + '" '
    'type="music" {parameters}/><Timeline type="video" state="stopped"/><Timeline type="photo" '
    'state="stopped"/></MediaContainer> '
)


class SubscribeManager:
    subscribers: dict[str, list[Subscriber]] = {}
    running = True
    last_server_notify_state: dict[str, str] = {}

    def get_subscriber(self, target_uuid: str, client_uuid: str):
        subscribers = [
            s for s in self.subscribers.get(target_uuid, []) if s.uuid == client_uuid
        ]

        return subscribers[0] if subscribers else None

    def update_command_id(self, target_uuid: str, client_uuid: str, command_id: int):
        subscriber = self.get_subscriber(target_uuid, client_uuid)
        if subscriber is not None:
            subscriber.command_id = command_id

    async def add_subscriber(
        self,
        target_uuid: str,
        client_uuid: str,
        host: str,
        port: int,
        protocol: str = "http",
        command_id: int = 0,
    ):
        logger.info(
            "add_subscriber %s %s %s %s %s %s",
            target_uuid,
            client_uuid,
            host,
            port,
            protocol,
            command_id,
        )

        subscriber = self.get_subscriber(target_uuid, client_uuid)
        if subscriber:
            if subscriber.host != host or s.port != port or s.protocol != protocol:
                await self.remove_subscriber(subscriber.uuid)
            else:
                subscriber.command_id = command_id
                return

        subscribers = self.subscribers.get(target_uuid, [])
        subscribers.append(
            Subscriber(client_uuid, host, port, self, protocol, command_id)
        )
        self.subscribers[target_uuid] = subscribers

    async def remove_subscriber(self, uuid, target_uuid: str | None = None):
        logger.info("remove_subscriber %s %s", uuid, target_uuid)

        for uuid_ in (
            [target_uuid] if target_uuid is not None else self.subscribers.keys()
        ):
            subscribers = self.subscribers.get(uuid_, [])
            remove = None
            for subscribe in subscribers:
                if subscribe.uuid == uuid:
                    remove = subscribe
                    break
            if remove in subscribers:
                subscribers.remove(remove)
            if not subscribers:
                device = await get_device_by_uuid(uuid_)
                if device is not None and not self.subscribers.get(uuid_):
                    device.stop_subscribe()

    def stop(self):
        self.running = False

    async def notify_server(self):
        await asyncio.gather(*[self.notify_server_device(device) for device in devices])

    async def notify_server_device(self, device, force=False):
        subs = self.subscribers.get(device.uuid, [])
        if not subs and not force:
            return

        adapter = adapter_by_device(device)
        if adapter.plex_lib is None or adapter.queue is None:
            return
        if adapter.no_notice and not force:
            logger.info("ignore sub notice for %s", adapter.dlna.name)
            return
        if adapter.plex_state is None:
            return
        if (
            self.last_server_notify_state.get(device.uuid, "")
            == adapter.plex_state
            == "stopped"
            and not force
        ):
            return
        self.last_server_notify_state[device.uuid] = adapter.plex_state
        params = await adapter.get_pms_state()
        if not params or params.get("state", None) is None:
            return
        params.update(pms_header(device))
        async with g.http.get(adapter.plex_lib.get_timeline(), params=params) as res:
            try:
                res.raise_for_status()
            except Exception as e:
                logger.warning("notify server error %s %s %s", e, res.content, params)

    async def notify(self):
        await self.notify_server()
        tasks = [self.notify_device(device) for device in devices]
        await asyncio.gather(*tasks)

    async def msg_for_device(self, device):
        adapter = adapter_by_device(device)
        if adapter.no_notice:
            return None

        if (
            adapter.state.state is None
            or adapter.state.state == "STOPPED"
            or adapter.queue is None
        ):
            return TIMELINE_STOPPED

        state = await adapter.get_state()
        if not state or state.get("state", None) is None:
            return TIMELINE_STOPPED

        state["itemType"] = "music"
        logger.debug("notify %s %s", device.uuid, state)
        xml = TIMELINE_PLAYING.format(
            parameters=" ".join([f'{k}="{v}"' for k, v in state.items()]),
            command_id="{command_id}",
        )
        return xml

    async def notify_device(self, device: DlnaDevice):
        subs = self.subscribers.get(device.uuid, [])
        adapter = adapter_by_device(device)
        if adapter.no_notice:
            logger.info("ignore sub notice for %s", adapter.dlna.name)
            return

        msg = await self.msg_for_device(device)
        if msg is None:
            return

        await asyncio.gather(*[sub.send(msg, device) for sub in subs])

    async def notify_device_disconnected(self, device):
        subs = self.subscribers.get(device.uuid, [])
        await asyncio.gather(*[sub.send(TIMELINE_DISCONNECTED, device) for sub in subs])
        asyncio.create_task(
            asyncio.gather(
                *[
                    self.remove_subscriber(sub.uuid, target_uuid=device.uuid)
                    for sub in subs
                ]
            )
        )

    async def start(self):
        await self.notify()
        while self.running:
            await asyncio.sleep(settings.plex_notify_interval)
            wait_timeout = settings.plex_notify_interval * 10
            try:
                target_devices = []
                none_uuids = []
                for u, l in self.subscribers.items():
                    if len(l) > 0:
                        d = await get_device_by_uuid(u)
                        if d is not None:
                            target_devices.append(d)
                        else:
                            none_uuids.append(u)
                for u in none_uuids:
                    if u in self.subscribers:
                        del self.subscribers[u]
                if len(target_devices) == 0:
                    continue
                await asyncio.wait(
                    [
                        asyncio.create_task(
                            adapter_by_device(device).wait_for_event(wait_timeout)
                        )
                        for device in target_devices
                    ],
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
            except asyncio.exceptions.TimeoutError:
                pass
            try:
                await self.notify()
            except Exception as e:
                logger.info("subscribe notify error %s", e)


@dataclass
class Subscriber:
    uuid: str
    host: str
    port: int
    manager: SubscribeManager
    protocol: str = "http"
    command_id: int = 0

    url: str = field(init=False)

    def __post_init__(self):
        self.url = f"{self.protocol}://{self.host}:{self.port}/:/timeline"

    async def send(self, msg: str, device: DlnaDevice):
        msg = msg.format(command_id=self.command_id)
        response = None
        # print(f"sub send {self.host} {msg}")
        try:
            async with g.http.post(
                self.url, data=msg, headers=subscriber_send_headers(device), timeout=1
            ) as response:
                response.raise_for_status()
        except Exception as e:
            logger.warning("subscriber send error %s %s %s", e, msg, device)
            await self.manager.remove_subscriber(self.uuid)

    def __eq__(self, other):
        return self.uuid == other.uuid

    def __repr__(self):
        return f"{self.host}:{self.port}"


sub_man = SubscribeManager()
