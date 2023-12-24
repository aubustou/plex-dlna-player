from __future__ import annotations

import asyncio
import logging
import re
import traceback
from dataclasses import InitVar, dataclass, field, fields
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import ClientConnectorError

from ..plex.adapters import remove_adapter
from ..settings import settings
from ..utils import (
    ALLOWED_UPNP_AVT_VERSIONS,
    ALLOWED_UPNP_RC_VERSIONS,
    UPNP_AVT_SERVICE_TYPE,
    UPNP_AVT_SERVICE_TYPE_PREFIX,
    UPNP_RC_SERVICE_TYPE,
    UPNP_RC_SERVICE_TYPE_PREFIX,
    g,
    xml2dict,
)
from .models.service import Action, StateVariable

if TYPE_CHECKING:
    from .models.root import Root, Service
    from .models.service import SCPDRoot

logger = logging.getLogger(__name__)

PAYLOAD_FMT = (
    '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{urn}">'
    "{fields}</u:{action}></s:Body></s:Envelope>"
)


DEFAULT_ACTION_DATA = {
    "InstanceID": 0,
    "Channel": "Master",
    "CurrentURIMetaData": "",
    "NextURIMetaData": "",
    "Unit": "REL_TIME",
    "Speed": 1,
}

USER_AGENT = f"{__file__}/1.0"

ERROR_COUNT_TO_REMOVE = 20

devices: list[DlnaDevice] = []


@dataclass
class DlnaDeviceService:
    service_dict: InitVar[Service]
    device: DlnaDevice

    service_type: str = field(init=False)
    control_url: str = field(init=False)
    event_url: str = field(init=False)
    spec_url: str = field(init=False)
    urn: str = field(init=False)
    subscribed: bool = field(default=False, init=False)
    _spec_info: SCPDRoot | None = field(default=None, init=False)
    next_subscribe_call_time: datetime | None = field(default=None, init=False)

    actions: dict[str, Action] = field(default_factory=dict, init=False)

    def __post_init__(self, service_dict: Service):
        self.service_type = service_dict["serviceType"]
        self.control_url = urljoin(self.device.location_url, service_dict["controlURL"])
        self.event_url = urljoin(self.device.location_url, service_dict["eventSubURL"])
        self.spec_url = urljoin(self.device.location_url, service_dict["SCPDURL"])
        self.urn = self.service_type

    def payload_from_template(self, action: str, data: dict[str, str]) -> str:
        fields = ""
        for tag, value in data.items():
            fields += "<{tag}>{value}</{tag}>".format(tag=tag, value=value)
        payload = PAYLOAD_FMT.format(action=action, urn=self.urn, fields=fields)
        return payload

    async def control(
        self,
        action: str,
        data: dict[str, str],
        client: aiohttp.ClientSession | None = None,
    ):
        headers = {
            "Content-type": "text/xml",
            f"SOAPACTION": f'"{self.urn}#{action}"',
            "charset": "utf-8",
            "User-Agent": USER_AGENT,
        }
        if client is None:
            client = g.http

        action_spec = await self.get_action_spec(action, client=client)
        if action_spec is None:
            raise Exception(f"No such action {action}, {self.service_type}")

        if arguments := action_spec["argumentList"]["argument"]:
            args = arguments if isinstance(arguments, list) else [arguments]

            if not isinstance(data, dict):
                none_default_arguments = [
                    argument
                    for argument in args
                    if argument["name"] not in DEFAULT_ACTION_DATA.keys()
                ]
                length = len(none_default_arguments)

                if length == 1:
                    data = {none_default_arguments[0]["name"]: data}
                elif length!= 0:
                    raise Exception(
                        f"{action} needs {length} arguments, pass data as dict."
                    )
            for argument in args:
                argument_name = argument["name"]
                if (
                    argument_name in DEFAULT_ACTION_DATA.keys()
                    and argument_name not in data.keys()
                ):
                    data[argument_name] = DEFAULT_ACTION_DATA[argument_name]

        payload = self.payload_from_template(action, data)

        try:
            async with client.post(
                self.control_url,
                data=payload.encode(),
                headers=headers,
                timeout=10,
            ) as response:
                response.raise_for_status()

                self.device.repeat_error_count = 0
                info = xml2dict(await response.text())
                error = info.Envelope.Body.Fault.detail.UPnPError.get(
                    "errorDescription"
                )
                if error is not None:
                    logger.error("DLNA device control request error %s", info.toDict())
                    return None
                else:
                    return info.Envelope.Body.get(f"{self.device.avt_service_type}:{action}Response")
        except ClientConnectorError as exc:
            logger.error(
                "DLNA client connection error %s %s %s",
                self.device.name,
                action,
                exc,
            )
            self.device.repeat_error_count += 1
            if self.device.repeat_error_count >= ERROR_COUNT_TO_REMOVE:
                logger.warning(
                    "remove device %s due to %s connection error",
                    self.device.name,
                    self.device.repeat_error_count,
                )
                if asyncio.get_running_loop() == self.device.loop:
                    asyncio.create_task(self.device.remove_self())
                else:
                    asyncio.run_coroutine_threadsafe(
                        self.device.remove_self(), self.device.loop
                    )
        except Exception as exc:
            logger.error(
                "DLNA %s %s control error %s %s",
                self.device.name,
                action,
                exc.__class__.__name__,
                exc,
            )

            if "different loop" in str(exc):
                traceback.print_tb(exc.__traceback__)

    async def subscribe(self, timeout_sec: int = 120) -> bool | None:
        if settings.host_ip is None:
            logger.warning("dlna subscribe no host ip")
            return False

        if self.next_subscribe_call_time is not None:
            if datetime.utcnow() < self.next_subscribe_call_time:
                return None

        headers = {
            "Cache-Control": "no-cache",
            "User-Agent": USER_AGENT,
            "NT": "upnp:event",
            "Callback": f"<http://{settings.host_ip}:{settings.http_port}/dlna/callback/{self.device.uuid}>",
            "Timeout": f"Second-{timeout_sec}",
        }
        logger.info("Subscribe DLNA device %s %s", self.device.name, self.service_type)

        async with g.http.request(
            "SUBSCRIBE", self.event_url, headers=headers
        ) as response:
            if response.ok:
                logging.info(
                    "DLNA device %s %s subscribed", self.device.name, self.service_type
                )
                self.next_subscribe_call_time = datetime.utcnow() + timedelta(
                    seconds=(timeout_sec // 2)
                )
                return True
            else:
                return False

    async def get_spec(self, client: aiohttp.ClientSession | None = None) -> SCPDRoot:
        if self._spec_info is not None:
            return self._spec_info

        if client is None:
            client = g.http

        logger.info("DLNA device %s %s get spec", self.device.name, self.service_type)

        async with client.get(self.spec_url) as response:
            response.raise_for_status()
            xml = re.sub(' xmlns="[^"]+"', "", await response.text(), count=1)
            info: SCPDRoot = xml2dict(xml)
            self._spec_info = info

        return self._spec_info

    async def get_actions(self, client: aiohttp.ClientSession | None = None) -> list[Action]:
        spec = await self.get_spec(client=client)

        for action in spec["scpd"]["actionList"]["action"]:
            self.actions[action["name"]] = action

        return spec["scpd"]["actionList"]["action"]

    async def get_action_spec(
        self, action_name, client: aiohttp.ClientSession | None = None
    ) -> Action | None:
        if action := self.actions.get(action_name):
            return action

        for action in await self.get_actions(client=client):
            if action["name"] == action_name:
                return action
            else:
                return None

    async def get_state_variables(self) -> list[StateVariable]:
        spec: SCPDRoot = await self.get_spec()
        return spec["scpd"]["serviceStateTable"]["stateVariable"]


@dataclass
class DlnaDeviceCommand:
    def GetTransportInfo(self, client: aiohttp.ClientSession | None = None):
        pass

    def GetVolume(self, client: aiohttp.ClientSession | None = None):
        pass

    def SetVolume(self, volume: int):
        pass

    def GetMute(self, client: aiohttp.ClientSession | None = None):
        pass

    def SetAVTransportURI(self, uri):
        pass

    def Play(self):
        pass

    def Pause(self):
        pass

    def Stop(self):
        pass

    def Seek(self, timestamp: str):
        pass


class DlnaDeviceCommandAVTransport(DlnaDeviceCommand):
    def __init__(self, service: DlnaDeviceService):
        self.service = service

    def GetTransportInfo(self, client: aiohttp.ClientSession | None = None):
        return self.service.call("GetTransportInfo", client=client)

    def SetAVTransportURI(self, uri):
        return self.service.call(
            "SetAVTransportURI",
            client=None,
            instanceID=0,
            currentURI=uri,
            currentURIMetaData="",
        )

    def Play(self):
        return self.service.call("Play", client=None, instanceID=0, speed=1)

    def Pause(self):
        return self.service.call("Pause", client=None, instanceID=0)

    def Stop(self):
        return self.service.call("Stop", client=None, instanceID=0)

    def Seek(self, timestamp: str):
        return self.service.call(
            "Seek", client=None, instanceID=0, unit="REL_TIME", target=timestamp
        )


@dataclass
class DlnaDevice:
    location_url: str
    name: str | None = field(default=None, init=False)
    model: str | None = field(default=None, init=False)
    ip: str | None = field(default=None, init=False)
    info: Root | None = field(default=None, init=False)
    services: dict[str, DlnaDeviceService] = field(default_factory=dict, init=False)
    volume_max: int | None = field(default=None, init=False)
    volume_min: int | None = field(default=None, init=False)
    volume_step: int | None = field(default=None, init=False)
    uuid: str | None = field(default=None, init=False)
    loop: asyncio.AbstractEventLoop = field(
        default_factory=asyncio.get_running_loop, init=False
    )
    repeat_error_count: int = field(default=0, init=False)

    avt_service_type: str = field(default="", init=False)
    rc_service_type: str = field(default="", init=False)
    avt_service_type_version: int = field(init=False)
    rc_service_type_version: int = field(init=False)

    actions: dict[str, DlnaDeviceCommand] = field(default_factory=dict, init=False)

    async def get_data(self):
        if self.info:
            return

        async with g.http.get(self.location_url) as response:
            if response.ok:
                xml = await response.text()
                xml = re.sub(' xmlns="[^"]+"', "", xml, count=1)
                info: Root = xml2dict(xml)
                info = info["root"]
                self.info = info

        if self.info:
            self.name = self.info["device"]["friendlyName"]
            self.model = self.info["device"].get("modelDescription", settings.product)
            self.uuid = self.info["device"]["UDN"].removeprefix("uuid:")

            for service in self.info["device"]["serviceList"]["service"]:
                service_type = service["serviceType"]
                self.services[service_type] = DlnaDeviceService(service, self)
                await self.services[service_type].get_actions()

                prefix, suffix = service_type.rsplit(":", 1)
                if (
                    prefix == UPNP_AVT_SERVICE_TYPE_PREFIX
                    and suffix in ALLOWED_UPNP_AVT_VERSIONS
                ):
                    self.avt_service_type = service_type
                    self.avt_service_type_version = int(suffix)
                elif (
                    prefix == UPNP_RC_SERVICE_TYPE_PREFIX
                    and suffix in ALLOWED_UPNP_RC_VERSIONS
                ):
                    self.rc_service_type = service_type
                    self.rc_service_type_version = int(suffix)

        if not self.name or not self.uuid:
            logger.error("DLNA service has no name or uuid %s", self.location_url)
            raise Exception(f"not a valid DLNA device {self.location_url}")

        if not (self.avt_service_type and self.rc_service_type):
            logger.error("DLNA service has no AVT or RC service %s", self.location_url)
            raise Exception(f"not valid DLNA device {self.name}")

        url = urlparse(self.location_url)
        self.ip = url.hostname
        self.name = settings.dlna_name_alias(self.uuid, self.name, self.ip)
        await self.get_volume_info()
        await asyncio.gather(*[s.get_spec() for s in self.services.values()])

    async def _find_service_by_action(self, action):
        await self.get_data()
        for key, service in self.services.items():
            if await service.get_action_spec(action) is not None:
                return service
        return None

    def __getattr__(self, item):
        """Get attribute or action."""
        if item in self.__dict__:
            return self.__dict__[item]

        def action(data: dict | None = {}, client: aiohttp.ClientSession | None = None):
            return self.action(item, data=data or {}, client=client)

        return action

    # def GetPositionInfo(
    #     self, data: dict | None = None, client: aiohttp.ClientSession | None = None
    # ):
    #     return self.action("GetPositionInfo", data=data or {}, client=client)

    async def action(
        self,
        action: str,
        data: dict | None = None,
        service_type: str | None = None,
        client: aiohttp.ClientSession | None = None,
    ):
        data = data or {}

        await self.get_data()
        service = None
        if service_type is not None:
            if (service := self._get_service(service_type)) is None:
                raise Exception(f"service type not found {service_type}")
        else:
            if (service := await self._find_service_by_action(action)) is None:
                raise Exception(f"action not found {action}")
        return await service.control(action, data, client=client)

    def _get_service(self, service_type: str):
        try:
            return self.services[service_type]
        except KeyError:
            logger.error("DLNA service has no service type %s", service_type)
            raise Exception(f"service type not found {service_type}")

    async def subscribe(self, service_type: str | None = None, timeout_sec: int = 120):
        await self.get_data()
        service = self._get_service(service_type or self.avt_service_type)
        await service.subscribe(timeout_sec=timeout_sec)

    async def loop_subscribe(
        self, service_type: str | None = None, timeout_sec: int = 120
    ):
        service = self._get_service(service_type or self.avt_service_type)
        if service.subscribed:
            return

        service.subscribed = True
        while service.subscribed:
            await self.subscribe(service_type=service_type, timeout_sec=timeout_sec)
            await asyncio.sleep(timeout_sec // 2)

    def stop_subscribe(self, service_type: str | None = None):
        service = self._get_service(service_type or self.avt_service_type)
        service.subscribed = False

    async def get_volume_info(self):
        await self.get_data()

        self.volume_min = 0
        self.volume_max = 100
        self.volume_step = 1
        service = self._get_service(self.rc_service_type)

        try:
            vars = await service.get_state_variables()
            for v in vars:
                if v["name"] == "Volume":
                    r = v["allowedValueRange"]
                    self.volume_min = int(r["minimum"])
                    self.volume_max = int(r["maximum"])
                    self.volume_step = int(r["step"])
                    break
        except Exception:
            logger.exception("get volume info error for %s", self.name)

    async def remove_self(self):
        devices.remove(self)

        from plex.adapters import adapter_by_device, remove_adapter
        from plex.subscribe import sub_man

        self.stop_subscribe()

        adapter = adapter_by_device(self)
        adapter.state.state = "STOPPED"
        adapter.state.looping_wait_event.set()
        adapter.state._thread_should_stop = True

        await sub_man.notify_device_disconnected(self)
        await sub_man.notify_server_device(self, force=True)

        adapter.queue = None
        remove_adapter(adapter)

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"DLNA Device {self.name} {self.ip} {self.uuid}>"

    def __eq__(self, other):
        return self.uuid == other.uuid


# if settings.location_url is not None:
#     devices.append(DlnaDevice(settings.location_url))


async def get_device_data():
    await asyncio.gather(*[device.get_data() for device in devices])


async def get_device_by_uuid(uuid: str) -> DlnaDevice | None:
    for device in devices:
        if device.uuid == uuid:
            await device.get_data()
            return device
    logger.info("device uuid not found %s", uuid)
    return None
