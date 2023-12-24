from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, overload

import aiohttp
import xmltodict
from dotmap import DotMap

from .settings import settings

if TYPE_CHECKING:
    from .dlna.dlna_device import DlnaDevice

logger = logging.getLogger(__name__)

ALLOWED_UPNP_AVT_VERSIONS = ["1", "2"]
ALLOWED_UPNP_RC_VERSIONS = ["1", "2"]

UPNP_AVT_SERVICE_TYPE_PREFIX = "urn:schemas-upnp-org:service:AVTransport"
UPNP_RC_SERVICE_TYPE_PREFIX = "urn:schemas-upnp-org:service:RenderingControl"
UPNP_AVT_SERVICE_TYPE = UPNP_RC_SERVICE_TYPE_PREFIX + ":{version}"
UPNP_RC_SERVICE_TYPE = UPNP_RC_SERVICE_TYPE_PREFIX + ":{version}"


class ClientSession(aiohttp.ClientSession):
    verify_ssl: bool

    def __init__(self, *args, verify_ssl: bool, **kwargs):
        self.verify_ssl = verify_ssl
        super().__init__(*args, **kwargs)

    async def _request(self, *args, **kwargs):
        if not "verify_ssl" in kwargs:
            kwargs["ssl"] = self.verify_ssl
        return await super()._request(*args, **kwargs)


@dataclass
class G:
    http: aiohttp.ClientSession = field(init=False)
    verify_ssl: bool = field(default=False, init=False)

    def create_session(self):
        self.http = ClientSession(
            verify_ssl=self.verify_ssl,
        )


g = G()


def unescape_xml(xml: bytes) -> str:
    return xml.decode().replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


@overload
def xml2dict(
    xml: str | bytes,
    upnp_avt_service_type: str = ...,
    upnp_rc_service_type: str = ...,
    as_dotmap: bool = True,
) -> DotMap:
    ...


@overload
def xml2dict(
    xml: str | bytes,
    upnp_avt_service_type: str = ...,
    upnp_rc_service_type: str = ...,
    as_dotmap: bool = False,
) -> dict:
    ...


def xml2dict(
    xml: str | bytes,
    upnp_avt_service_type: str = UPNP_AVT_SERVICE_TYPE.format(version=1),
    upnp_rc_service_type: str = UPNP_RC_SERVICE_TYPE.format(version=1),
    as_dotmap: bool = True,
) -> DotMap | dict:
    if not isinstance(xml, str):
        xml = unescape_xml(xml)

    parsed = xmltodict.parse(
        xml,
        process_namespaces=True,
        namespaces={
            upnp_avt_service_type: None,
            upnp_rc_service_type: None,
            "http://schemas.xmlsoap.org/soap/envelope/": None,
            "urn:schemas-upnp-org:event-1-0": None,
            "urn:schemas-upnp-org:metadata-1-0/AVT/": None,
        },
    )
    if as_dotmap:
        return DotMap(parsed)
    else:
        return parsed


def pms_header(device):
    return {
        "X-Plex-Client-Identifier": device.uuid,
        "X-Plex-Device": device.model,
        "X-Plex-Device-Name": device.name,
        "X-Plex-Platform": settings.platform,
        "X-Plex-Platform-Version": settings.platform_version,
        "X-Plex-Product": device.model,
        "X-Plex-Version": settings.version,
        "X-Plex-Provides": "player,pubsub-player",
    }


def plex_server_response_headers(device):
    return {
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Accept-Language": "en",
        "X-Plex-Device": device.model,
        "X-Plex-Platform": settings.platform,
        "X-Plex-Platform-Version": settings.platform_version,
        "X-Plex-Product": device.model,
        "X-Plex-Version": settings.version,
        "X-Plex-Client-Identifier": device.uuid,
        "X-Plex-Device-Name": device.name,
        "X-Plex-Provides": "player,pubsub-player",
    }


def subscriber_send_headers(device: DlnaDevice):
    return {
        "Content-Type": "application/xml",
        "Connection": "Keep-Alive",
        "X-Plex-Client-Identifier": device.uuid,
        "X-Plex-Platform": settings.platform,
        "X-Plex-Platform-Version": settings.platform_version,
        "X-Plex-Product": device.model,
        "X-Plex-Version": settings.version,
        "X-Plex-Device-Name": device.name,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en,*",
    }


def timeline_poll_headers(device):
    return {
        "X-Plex-Client-Identifier": device.uuid,
        "X-Plex-Protocol": "1.0",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Max-Age": "1209600",
        "Access-Control-Expose-Headers": "X-Plex-Client-Identifier",
        "Content-Type": "text/xml;charset=utf-8",
    }


def parse_timedelta(s: str):
    try:
        t = datetime.strptime(s, "%H:%M:%S.%f")
    except ValueError:
        t = datetime.strptime(s, "%H:%M:%S")
    return timedelta(
        hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond
    )


def convert_volume(
    value: int, from_max: int, from_min: int, to_max: int, to_min: int, to_step: int
):
    if from_max == to_max and from_min == to_min:
        return value
    if from_max - from_min == to_max - to_min:
        return value - from_min + to_min
    percent = float(value - from_min) / float(from_max - from_min)
    value = percent * (to_max - to_min)
    value = int(value / to_step)
    value += to_min
    return value
