from typing import TypedDict


class Icon(TypedDict):
    mimetype: str
    width: int
    height: int
    depth: int
    url: str


class IconList(TypedDict):
    icon: list[Icon]


class Service(TypedDict):
    serviceType: str
    serviceId: str
    SCPDURL: str
    controlURL: str
    eventSubURL: str


class ServiceList(TypedDict):
    service: list[Service]


class DiscoveredDevice(TypedDict):
    deviceType: str
    friendlyName: str
    manufacturer: str
    manufacturerURL: str
    modelDescription: str
    modelName: str
    modelNumber: str
    modelURL: str
    serialNumber: str
    UDN: str
    UPC: str | None
    iconList:IconList 
    serviceList: ServiceList
    presentationURL: str | None
    dlna_X_DLNADOC: str


class Root(TypedDict):
    specVersion: dict

    device: DiscoveredDevice
