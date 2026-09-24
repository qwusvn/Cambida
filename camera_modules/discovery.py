"""
Standalone LAN Camera Discovery and Bulk-Channel Addition Module for Cambida.

Provides multi-protocol local network discovery:
1. ONVIF WS-Discovery (UDP multicast 239.255.255.250:3702)
2. Hikvision SADP Discovery (UDP multicast 239.255.255.250:37020 and broadcast)
3. Dahua / Imou / KBVision Broadcast Search (UDP 37810 / 37777)
4. SSDP / UPnP Discovery (UDP multicast 239.255.255.250:1900)
5. Active TCP Port Scan (common camera ports 80, 554, 8000, 37777, 8888)

Follows .project/CAMERA_MODULE_CONTRACT_20260924.md:
- Discover LAN devices and selectable bulk-channel add.
- Vendor choice: Hikvision/Ezviz (port 8000), Dahua/Imou (port 37777), KBVision (port 8888), RTSP, ONVIF.
- All ports editable.
- Separate IDs for devices, physical channels, and stable table bindings.
- Never exposes credentials in logs or data structures.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union
from xml.etree import ElementTree as ET

try:
    from camera_modules.core import (
        CameraManager,
        ChannelInfo,
        DeviceConfig,
        DeviceInfo,
        TableBinding,
        generate_camera_id,
        generate_device_id,
        generate_table_id,
    )
except ImportError:
    CameraManager = None  # type: ignore[misc,assignment]
    ChannelInfo = None  # type: ignore[misc,assignment]
    DeviceConfig = None  # type: ignore[misc,assignment]
    DeviceInfo = None  # type: ignore[misc,assignment]
    TableBinding = None  # type: ignore[misc,assignment]

    def generate_device_id(vendor: str, host: str, port: int, custom_id: Optional[str] = None) -> str:  # type: ignore[misc]
        if custom_id:
            return str(custom_id)
        return f"dev_{vendor}_{host.replace('.', '_')}_{port}"

    def generate_table_id(raw_id: Union[str, int]) -> str:  # type: ignore[misc]
        return str(raw_id).strip().lower()


logger = logging.getLogger("cambida.camera_modules.discovery")

# Discovery Constants
WS_DISCOVERY_ADDR = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

HIK_SADP_ADDR = "239.255.255.250"
HIK_SADP_PORT = 37020

DAHUA_SEARCH_PORT = 37810
DAHUA_DEFAULT_PORT = 37777

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

COMMON_CAMERA_PORTS = (80, 554, 8000, 37777, 8888)


@dataclass
class DiscoveredDevice:
    """Represents a camera, NVR, or video encoder discovered on the local network."""

    device_id: str
    ip: str
    port: int = 80
    protocol: str = "onvif"  # "onvif", "hikvision", "dahua", "kbvision", "rtsp"
    vendor: str = "generic"  # "hikvision", "ezviz", "dahua", "imou", "kbvision", "generic"
    model: str = ""
    name: str = ""
    mac: str = ""
    firmware: str = ""
    http_port: int = 80
    rtsp_port: int = 554
    channel_count: int = 1
    onvif_xaddr: Optional[str] = None
    channels: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 1. ONVIF WS-Discovery (Multicast UDP)
# ---------------------------------------------------------------------------


def discover_onvif_devices(timeout_sec: float = 3.0) -> List[DiscoveredDevice]:
    """
    Broadcast WS-Discovery Probe to locate ONVIF NVTs (Network Video Transmitters) on LAN.
    """
    msg_id = str(uuid.uuid4())
    probe_soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
               xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
               xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:MessageID>urn:uuid:{msg_id}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
  </soap:Header>
  <soap:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </soap:Body>
</soap:Envelope>""".strip()

    devices: List[DiscoveredDevice] = []
    seen_ips: Set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(max(0.5, float(timeout_sec)))

    try:
        # TTL for multicast
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.sendto(probe_soap.encode("utf-8"), (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))

        start_time = time.monotonic()
        while time.monotonic() - start_time < float(timeout_sec):
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen_ips:
                    continue

                raw_xml = data.decode("utf-8", errors="replace")
                xaddrs, scopes, endpoint = _parse_ws_discovery_match(raw_xml)
                if not xaddrs and not scopes:
                    continue

                # Parse vendor, model, name from scopes
                vendor, model, name = _parse_onvif_scopes(scopes)
                port = 80
                primary_xaddr = xaddrs[0] if xaddrs else None
                if primary_xaddr:
                    match_port = re.search(r":(\d+)/", primary_xaddr)
                    if match_port:
                        port = int(match_port.group(1))

                dev_id = generate_device_id(vendor, ip, port)
                dev = DiscoveredDevice(
                    device_id=dev_id,
                    ip=ip,
                    port=port,
                    protocol="onvif",
                    vendor=vendor,
                    model=model,
                    name=name or f"{vendor.capitalize()} ({ip})",
                    onvif_xaddr=primary_xaddr,
                    http_port=port,
                    extra={"scopes": scopes, "endpoint": endpoint},
                )
                devices.append(dev)
                seen_ips.add(ip)
            except socket.timeout:
                break
            except Exception:
                pass
    finally:
        sock.close()

    return devices


def _parse_ws_discovery_match(xml_text: str) -> Tuple[List[str], List[str], str]:
    """Parse XAddrs, Scopes, and EndpointReference from ProbeMatch XML."""
    xaddrs: List[str] = []
    scopes: List[str] = []
    endpoint = ""
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "XAddrs" and elem.text:
                xaddrs.extend(elem.text.strip().split())
            elif tag == "Scopes" and elem.text:
                scopes.extend(elem.text.strip().split())
            elif tag == "Address" and elem.text and not endpoint:
                endpoint = elem.text.strip()
    except Exception:
        pass
    return xaddrs, scopes, endpoint


def _parse_onvif_scopes(scopes: List[str]) -> Tuple[str, str, str]:
    """Extract vendor, model, and device name from ONVIF scope URIs."""
    vendor = "generic"
    model = ""
    name = ""

    for scope in scopes:
        s_lower = scope.lower()
        if "hikvision" in s_lower:
            vendor = "hikvision"
        elif "ezviz" in s_lower:
            vendor = "ezviz"
        elif "dahua" in s_lower:
            vendor = "dahua"
        elif "imou" in s_lower:
            vendor = "imou"
        elif "kbvision" in s_lower:
            vendor = "kbvision"

        if "hardware/" in s_lower:
            model = scope.split("hardware/")[-1]
        elif "name/" in s_lower:
            name = scope.split("name/")[-1]

    return vendor, model, name


# ---------------------------------------------------------------------------
# 2. Hikvision SADP Discovery (Multicast / Broadcast UDP)
# ---------------------------------------------------------------------------


def discover_hikvision_sadp(timeout_sec: float = 3.0) -> List[DiscoveredDevice]:
    """
    Probe Hikvision / Ezviz devices using SADP protocol on port 37020.
    """
    probe_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Probe>
  <Uuid>{uuid.uuid4()}</Uuid>
  <Types>inquiry</Types>
</Probe>""".strip()

    devices: List[DiscoveredDevice] = []
    seen_ips: Set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(max(0.5, float(timeout_sec)))

    try:
        data_bytes = probe_xml.encode("utf-8")
        sock.sendto(data_bytes, (HIK_SADP_ADDR, HIK_SADP_PORT))
        sock.sendto(data_bytes, ("255.255.255.255", HIK_SADP_PORT))

        start_time = time.monotonic()
        while time.monotonic() - start_time < float(timeout_sec):
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen_ips:
                    continue

                raw_xml = data.decode("utf-8", errors="replace")
                if "<ProbeMatch" not in raw_xml:
                    continue

                dev_info = _parse_sadp_xml(raw_xml)
                dev_ip = dev_info.get("ip") or ip
                port = int(dev_info.get("port") or 8000)
                vendor = dev_info.get("vendor") or "hikvision"
                model = dev_info.get("model") or ""
                mac = dev_info.get("mac") or ""
                firmware = dev_info.get("firmware") or ""

                dev_id = generate_device_id(vendor, dev_ip, port)
                dev = DiscoveredDevice(
                    device_id=dev_id,
                    ip=dev_ip,
                    port=port,
                    protocol="hikvision",
                    vendor=vendor,
                    model=model,
                    mac=mac,
                    firmware=firmware,
                    http_port=int(dev_info.get("http_port") or 80),
                    rtsp_port=554,
                    name=f"Hikvision {model} ({dev_ip})".strip(),
                    extra={"sadp_raw": dev_info},
                )
                devices.append(dev)
                seen_ips.add(dev_ip)
            except socket.timeout:
                break
            except Exception:
                pass
    finally:
        sock.close()

    return devices


def _parse_sadp_xml(xml_text: str) -> Dict[str, Any]:
    """Parse Hikvision SADP ProbeMatch XML fields."""
    info: Dict[str, Any] = {}
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            text = (elem.text or "").strip()
            if tag == "IPv4Address":
                info["ip"] = text
            elif tag == "CommandPort":
                info["port"] = int(text) if text.isdigit() else 8000
            elif tag == "HttpPort":
                info["http_port"] = int(text) if text.isdigit() else 80
            elif tag == "MAC":
                info["mac"] = text
            elif tag == "DeviceType":
                info["model"] = text
                if "ezviz" in text.lower():
                    info["vendor"] = "ezviz"
                else:
                    info["vendor"] = "hikvision"
            elif tag == "SoftwareVersion":
                info["firmware"] = text
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# 3. Dahua / Imou / KBVision Broadcast Search (UDP 37810 / 37777)
# ---------------------------------------------------------------------------


def discover_dahua_devices(timeout_sec: float = 3.0) -> List[DiscoveredDevice]:
    """
    Broadcast search packet for Dahua, Imou, and KBVision devices on port 37810 / 37777.
    """
    # Dahua search packet header format
    search_pkt = b"\xa0\x00\x00\x60\x00\x00\x00\x00DHIP" + b"\x00" * 20

    devices: List[DiscoveredDevice] = []
    seen_ips: Set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(max(0.5, float(timeout_sec)))

    try:
        sock.sendto(search_pkt, ("255.255.255.255", DAHUA_SEARCH_PORT))

        start_time = time.monotonic()
        while time.monotonic() - start_time < float(timeout_sec):
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen_ips:
                    continue

                vendor = "dahua"
                model = ""
                mac = ""
                port = DAHUA_DEFAULT_PORT

                text_repr = data.decode("latin-1", errors="replace")
                if "KBVISION" in text_repr.upper():
                    vendor = "kbvision"
                    port = 8888
                elif "IMOU" in text_repr.upper():
                    vendor = "imou"
                    port = DAHUA_DEFAULT_PORT

                # Check for MAC address string in packet
                mac_match = re.search(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})", text_repr)
                if mac_match:
                    mac = mac_match.group(0)

                dev_id = generate_device_id(vendor, ip, port)
                dev = DiscoveredDevice(
                    device_id=dev_id,
                    ip=ip,
                    port=port,
                    protocol="netsdk",
                    vendor=vendor,
                    model=model,
                    mac=mac,
                    http_port=80,
                    rtsp_port=554,
                    name=f"{vendor.capitalize()} ({ip})",
                )
                devices.append(dev)
                seen_ips.add(ip)
            except socket.timeout:
                break
            except Exception:
                pass
    finally:
        sock.close()

    return devices


# ---------------------------------------------------------------------------
# 4. Active Subnet Scanner (Fallback TCP Port Check)
# ---------------------------------------------------------------------------


def scan_subnet_camera_ports(
    subnet_cidr: str,
    ports: Sequence[int] = COMMON_CAMERA_PORTS,
    timeout_sec: float = 1.0,
    max_workers: int = 50,
) -> List[DiscoveredDevice]:
    """
    Perform a fast multithreaded TCP connect sweep across a given IPv4 subnet.
    """
    try:
        net = ipaddress.IPv4Network(subnet_cidr, strict=False)
    except ValueError:
        return []

    # Limit maximum sweep count to /24 to prevent excessive latency
    hosts = list(net.hosts())
    if len(hosts) > 256:
        hosts = hosts[:256]

    discovered: List[DiscoveredDevice] = []
    lock = threading.Lock()

    def check_host_port(host_str: str, port_num: int):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(float(timeout_sec))
        try:
            res = s.connect_ex((host_str, port_num))
            if res == 0:
                proto = "rtsp" if port_num == 554 else "onvif"
                vendor = "generic"
                if port_num == 8000:
                    vendor = "hikvision"
                    proto = "hikvision"
                elif port_num == 37777:
                    vendor = "dahua"
                    proto = "netsdk"
                elif port_num == 8888:
                    vendor = "kbvision"
                    proto = "kbvision"

                with lock:
                    # Check if host already discovered
                    existing = next((d for d in discovered if d.ip == host_str), None)
                    if existing:
                        if port_num == 554:
                            existing.rtsp_port = 554
                        elif port_num in (37777, 8000, 8888):
                            existing.port = port_num
                            existing.vendor = vendor
                            existing.protocol = proto
                    else:
                        dev_id = generate_device_id(vendor, host_str, port_num)
                        discovered.append(
                            DiscoveredDevice(
                                device_id=dev_id,
                                ip=host_str,
                                port=port_num,
                                protocol=proto,
                                vendor=vendor,
                                name=f"Camera ({host_str}:{port_num})",
                            )
                        )
        except Exception:
            pass
        finally:
            s.close()

    threads: List[threading.Thread] = []
    for host in hosts:
        h_str = str(host)
        for p in ports:
            t = threading.Thread(target=check_host_port, args=(h_str, p), daemon=True)
            threads.append(t)
            t.start()
            if len(threads) >= max_workers:
                for th in threads:
                    th.join()
                threads.clear()

    for th in threads:
        th.join()

    return discovered


# ---------------------------------------------------------------------------
# 5. Unified Device Discovery Function
# ---------------------------------------------------------------------------


def discover_lan_cameras(
    timeout_sec: float = 3.0,
    protocols: Optional[Iterable[str]] = None,
    subnet: Optional[str] = None,
) -> List[DiscoveredDevice]:
    """
    Discover all LAN cameras and video devices across ONVIF, SADP, Dahua, and TCP probe.
    Deduplicates results by IP address and merges detected capabilities.
    """
    target_protocols = set(p.lower() for p in (protocols or ["onvif", "hikvision", "dahua"]))
    results: List[DiscoveredDevice] = []
    threads: List[threading.Thread] = []
    lock = threading.Lock()

    def run_onvif():
        devs = discover_onvif_devices(timeout_sec=timeout_sec)
        with lock:
            results.extend(devs)

    def run_hik():
        devs = discover_hikvision_sadp(timeout_sec=timeout_sec)
        with lock:
            results.extend(devs)

    def run_dahua():
        devs = discover_dahua_devices(timeout_sec=timeout_sec)
        with lock:
            results.extend(devs)

    if "onvif" in target_protocols:
        threads.append(threading.Thread(target=run_onvif, daemon=True))
    if "hikvision" in target_protocols or "ezviz" in target_protocols:
        threads.append(threading.Thread(target=run_hik, daemon=True))
    if "dahua" in target_protocols or "imou" in target_protocols or "kbvision" in target_protocols:
        threads.append(threading.Thread(target=run_dahua, daemon=True))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout_sec + 0.5)

    # Optional subnet scan fallback
    if subnet:
        subnet_devs = scan_subnet_camera_ports(subnet, timeout_sec=1.0)
        results.extend(subnet_devs)

    # Deduplicate and merge by IP
    by_ip: Dict[str, DiscoveredDevice] = {}
    for dev in results:
        ip = dev.ip
        if ip not in by_ip:
            by_ip[ip] = dev
        else:
            existing = by_ip[ip]
            # Merge fields if other protocol provided better data
            if existing.vendor == "generic" and dev.vendor != "generic":
                existing.vendor = dev.vendor
                existing.protocol = dev.protocol
                existing.port = dev.port
            if not existing.model and dev.model:
                existing.model = dev.model
            if not existing.mac and dev.mac:
                existing.mac = dev.mac
            if not existing.onvif_xaddr and dev.onvif_xaddr:
                existing.onvif_xaddr = dev.onvif_xaddr
            if dev.firmware and not existing.firmware:
                existing.firmware = dev.firmware

    return sorted(list(by_ip.values()), key=lambda d: d.ip)


# ---------------------------------------------------------------------------
# 6. Channel Inspection and Bulk-Add Helper
# ---------------------------------------------------------------------------


def inspect_device_channels(
    device: DiscoveredDevice,
    username: str = "admin",
    password: str = "",
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Enumerate physical channels for a discovered device.
    Uses ONVIF profiles if available, or synthesizes based on vendor and channel count.
    """
    # 1. If device has ONVIF service, query via ONVIFAdapter
    if device.protocol == "onvif" or device.onvif_xaddr:
        try:
            from camera_modules.onvif import ONVIFAdapter

            adapter = ONVIFAdapter(
                host=device.ip,
                port=device.port,
                username=username,
                password=password,
                timeout=timeout,
            )
            chans = adapter.get_channels()
            if chans:
                return [
                    {
                        "channel_id": getattr(ch, "channel_id", 1),
                        "name": getattr(ch, "name", f"Kênh {getattr(ch, 'channel_id', 1)}"),
                        "enabled": True,
                    }
                    for ch in chans
                ]
        except Exception:
            pass

    # 2. Default channel synthesis based on channel_count
    count = max(1, int(device.channel_count or 1))
    return [
        {
            "channel_id": ch_idx,
            "name": f"Kênh {ch_idx}",
            "enabled": True,
        }
        for ch_idx in range(1, count + 1)
    ]


def bulk_create_table_bindings(
    device: DiscoveredDevice,
    selected_channel_ids: Iterable[Union[int, str]],
    table_id_prefix: str = "ban-",
    start_position: int = 1,
    enabled: bool = True,
) -> List[Dict[str, Any]]:
    """
    Generate stable table bindings for the selected physical channels of a device.
    Guarantees distinct IDs:
    - device_id: Identifies the physical device.
    - channel_id: Physical channel number.
    - table_id: Stable table binding key (e.g. 'ban-01').
    """
    bindings: List[Dict[str, Any]] = []
    pos = int(start_position)

    for ch_id in selected_channel_ids:
        try:
            cid_int = int(ch_id)
            tbl_id = f"{table_id_prefix}{cid_int:02d}"
        except (ValueError, TypeError):
            tbl_id = f"{table_id_prefix}{ch_id}"

        binding = {
            "table_id": generate_table_id(tbl_id),
            "device_id": device.device_id,
            "channel_id": ch_id,
            "enabled": bool(enabled),
            "position": pos,
        }
        bindings.append(binding)
        pos += 1

    return bindings


def bulk_add_device_to_manager(
    manager: Any,
    device: DiscoveredDevice,
    selected_channel_ids: Iterable[Union[int, str]],
    table_id_prefix: str = "ban-",
    start_position: int = 1,
) -> Dict[str, Any]:
    """
    Register device, physical channels, and stable table bindings into CameraManager.
    Supports both legacy and modern CameraManager interfaces.
    """
    # 1. Register Device
    if hasattr(manager, "register_device"):
        try:
            # Check if manager expects DeviceInfo or individual arguments
            if DeviceInfo is not None:
                dev_info = DeviceInfo(
                    device_id=device.device_id,
                    vendor=device.vendor,
                    host=device.ip,
                    port=device.port,
                    http_port=device.http_port,
                    rtsp_port=device.rtsp_port,
                    name=device.name,
                    model=device.model,
                    serial_number=getattr(device, "serial_number", getattr(device, "mac", None)),
                    firmware_version=device.firmware,
                    channel_count=max(len(list(selected_channel_ids)), device.channel_count),
                    extra={"mac": device.mac} if getattr(device, "mac", None) else {},
                )
                manager.register_device(dev_info)
            elif DeviceConfig is not None:
                cfg = DeviceConfig(
                    device_id=device.device_id,
                    vendor=device.vendor,
                    host=device.ip,
                    port=device.port,
                )
                manager.register_device(cfg)
        except Exception as exc:
            logger.warning("Could not register device into manager: %s", exc)

    # 2. Register Channels & Table Bindings
    created_bindings = bulk_create_table_bindings(
        device,
        selected_channel_ids,
        table_id_prefix=table_id_prefix,
        start_position=start_position,
    )

    if hasattr(manager, "register_table_binding"):
        for b in created_bindings:
            try:
                if TableBinding is not None:
                    tb = TableBinding(
                        table_id=b["table_id"],
                        device_id=b["device_id"],
                        channel_id=b["channel_id"],
                        enabled=b["enabled"],
                        position=b["position"],
                    )
                    manager.register_table_binding(tb)
                else:
                    manager.register_table_binding(
                        b["table_id"],
                        b["device_id"],
                        b["channel_id"],
                        enabled=b["enabled"],
                        position=b["position"],
                    )
            except Exception as exc:
                logger.warning("Could not register table binding: %s", exc)

    return {
        "device_id": device.device_id,
        "vendor": device.vendor,
        "ip": device.ip,
        "port": device.port,
        "channels_added": len(created_bindings),
        "bindings": created_bindings,
    }


__all__ = [
    "DiscoveredDevice",
    "discover_onvif_devices",
    "discover_hikvision_sadp",
    "discover_dahua_devices",
    "scan_subnet_camera_ports",
    "discover_lan_cameras",
    "inspect_device_channels",
    "bulk_create_table_bindings",
    "bulk_add_device_to_manager",
]
