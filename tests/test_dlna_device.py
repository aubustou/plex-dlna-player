from __future__ import annotations

from plexdlnaserver.dlna.dlna_device import DlnaDevice
import pytest
import logging
import pytest_asyncio


logger = logging.getLogger(__name__)

@pytest_asyncio.fixture
async def session():
    import plexdlnaserver.utils

    yield plexdlnaserver.utils.g.create_session()

    await plexdlnaserver.utils.g.http.close()


@pytest.mark.asyncio
async def test_dlna_device(session):
    device = DlnaDevice(
        "http://192.168.1.25:46047/4e02d1d6-f938-4515-a5db-f5a5ce9bbdf1.xml"
    )
    await device.get_data()
    logger.info("info: %s", device.info)
    logger.info("GetPositionInfo: %s", (await device.GetPositionInfo()).toDict())
