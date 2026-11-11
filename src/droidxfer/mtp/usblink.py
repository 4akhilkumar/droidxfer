"""USB transport for MTP (pyusb + the libusb bundled by `libusb-package`), plus device discovery.

All methods here block; MtpSession runs them on its own worker thread.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger(__name__)

STILL_IMAGE_CLASS = 6
# Phone makers' USB vendor IDs, used to recognise an Android phone that is connected but not in a data mode.
ANDROID_VENDORS = {
    0x18D1: "Google", 0x04E8: "Samsung", 0x2717: "Xiaomi", 0x2A70: "OnePlus", 0x22B8: "Motorola",
    0x12D1: "Huawei", 0x22D9: "OPPO", 0x2D95: "vivo", 0x1004: "LG", 0x0FCE: "Sony", 0x0BB4: "HTC",
    0x2B0E: "Nothing", 0x17EF: "Lenovo", 0x19D2: "ZTE", 0x1EBF: "Realme", 0x05C6: "Qualcomm", 0x0E8D: "MediaTek",
}


class LinkError(ConnectionError):
    """The USB connection to the phone failed or was unplugged."""


class LinkBusy(LinkError):
    """Another program has claimed the phone's MTP interface."""


class Link(Protocol):
    max_packet: int

    def write(self, data: bytes, timeout: int) -> None: ...
    def read(self, size: int, timeout: int) -> bytes: ...
    def cancel(self, tid: int) -> None: ...
    def device_status(self) -> int: ...
    def clear_halts(self) -> None: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


@dataclass
class Candidate:
    """A USB device that looks like an Android phone."""

    key: str  # stable id while plugged in (USB serial, else bus/address)
    serial: str
    manufacturer: str
    product: str
    vendor_id: int
    product_id: int
    mode: str  # "mtp" | "ptp" | "none"
    opener: Callable[[], Link] | None = field(default=None, repr=False)

    def open(self) -> Link:
        if self.opener is None:
            raise LinkError("this device has no MTP interface")
        return self.opener()


def _usb_error_text(exc: Exception) -> str:
    return getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__


class UsbLink:
    def __init__(self, device, interface) -> None:
        import usb.core
        import usb.util

        self._usb = usb
        self.dev = device
        self.intf = interface.bInterfaceNumber
        try:
            if device.is_kernel_driver_active(self.intf):
                device.detach_kernel_driver(self.intf)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(device, self.intf)
        except usb.core.USBError as exc:
            usb.util.dispose_resources(device)
            raise LinkBusy(
                "another app is using the phone's file transfer connection "
                f"({_usb_error_text(exc)}). Quit Image Capture, Photos, Android File Transfer, OpenMTP or "
                "MacDroid and try again."
            ) from exc
        endpoints = list(interface)
        self.ep_in = next(e for e in endpoints if e.bEndpointAddress & 0x80 and e.bmAttributes & 3 == 2)
        self.ep_out = next(e for e in endpoints if not e.bEndpointAddress & 0x80 and e.bmAttributes & 3 == 2)
        self.max_packet = self.ep_out.wMaxPacketSize

    def _wrap(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._usb.core.USBTimeoutError as exc:
            raise TimeoutError("the phone did not answer in time") from exc
        except self._usb.core.USBError as exc:
            raise LinkError(f"USB connection lost: {_usb_error_text(exc)}") from exc

    def write(self, data: bytes, timeout: int) -> None:
        self._wrap(self.ep_out.write, data, timeout=timeout)

    def read(self, size: int, timeout: int) -> bytes:
        return bytes(self._wrap(self.ep_in.read, size, timeout=timeout))

    def cancel(self, tid: int) -> None:
        payload = struct.pack("<HI", 0x4001, tid)
        self._wrap(self.dev.ctrl_transfer, 0x21, 0x64, 0, self.intf, payload, timeout=2000)

    def device_status(self) -> int:
        data = self._wrap(self.dev.ctrl_transfer, 0xA1, 0x67, 0, self.intf, 20, timeout=2000)
        return struct.unpack_from("<H", bytes(data), 2)[0] if len(data) >= 4 else 0

    def clear_halts(self) -> None:
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep)
            except self._usb.core.USBError:
                pass

    def reset(self) -> None:
        try:
            self.dev.reset()
        except self._usb.core.USBError:
            pass

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self.dev, self.intf)
        except self._usb.core.USBError:
            pass
        self._usb.util.dispose_resources(self.dev)


def _string(device, index: int) -> str:
    import usb.util

    if not index:
        return ""
    try:
        return usb.util.get_string(device, index) or ""
    except Exception:  # some devices refuse string requests in some modes
        return ""


def scan_usb() -> list[Candidate]:
    """List USB devices that look like Android phones. Never claims an interface."""
    try:
        import libusb_package
    except ImportError:  # pragma: no cover - dependency missing
        return []
    candidates: list[Candidate] = []
    try:
        devices = list(libusb_package.find(find_all=True))
    except Exception as exc:  # libusb unavailable on this system
        log.debug("USB scan failed: %s", exc)
        return []
    for dev in devices:
        try:
            config = dev.get_active_configuration()
        except Exception:
            config = None
        image_intf = None
        mode = "none"
        if config is not None:
            for intf in config:
                if intf.bInterfaceClass == STILL_IMAGE_CLASS:
                    label = _string(dev, intf.iInterface).upper()
                    if "PTP" in label:  # Android's "PTP" (photos only) mode, cameras, iPhones
                        mode = "ptp"
                        continue
                    # "MTP", or a label we couldn't read: the session's DeviceInfo confirms it on open.
                    image_intf, mode = intf, "mtp"
                    break
        if image_intf is None and dev.idVendor not in ANDROID_VENDORS:
            continue  # cameras, iPhones (PTP) and everything that isn't a phone
        serial = _string(dev, dev.iSerialNumber)
        manufacturer = _string(dev, dev.iManufacturer) or ANDROID_VENDORS.get(dev.idVendor, "")
        product = _string(dev, dev.iProduct)
        if image_intf is None and mode == "none" and not product:
            continue  # vendor hub/accessory with no identity: not worth showing
        key = serial or f"usb-{dev.bus}-{dev.address}"
        opener = (lambda d=dev, i=image_intf: UsbLink(d, i)) if image_intf is not None else None
        candidates.append(
            Candidate(key, serial, manufacturer, product, dev.idVendor, dev.idProduct, mode, opener)
        )
    return candidates
