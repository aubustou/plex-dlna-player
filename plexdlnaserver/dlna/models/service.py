from typing import TypedDict


class Argument(TypedDict):
    name: str
    direction: str
    relatedStateVariable: str


class ArgumentList(TypedDict):
    argument: list[Argument]


class Action(TypedDict):
    name: str
    argumentList: ArgumentList


class ActionList(TypedDict):
    action: list[Action]


class AllowedValue(TypedDict):
    allowedValue: str


class StateVariable(TypedDict):
    name: str
    sendEvents: str
    dataType: str
    allowedValuelist: list[AllowedValue] | None


class StateVariableList(TypedDict):
    stateVariable: list[StateVariable]


class Scpd(TypedDict):
    specVersion: dict
    actionList: ActionList
    serviceStateTable: StateVariableList


class SCPDRoot(TypedDict):
    scpd: Scpd
