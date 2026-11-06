"""Идентичность устройства в памяти оператора; исходный iid остаётся на проводе."""

from typing import Any

DeviceKey = tuple[str, str]
LOCAL_NODE_IID = "local"


def device_key(iid: str, node_iid: str | None = None) -> DeviceKey:
    return node_iid or LOCAL_NODE_IID, iid


def device_identity(device: Any) -> DeviceKey:
    return device_key(device.iid, getattr(device, "node_iid", None))
