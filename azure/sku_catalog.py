"""Pure helpers for normalising Azure VM SKU, price and compatibility data.

This module deliberately has no Flask or Azure SDK imports so the rules can be
tested deterministically and reused by API and background refresh code.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional


MONTHLY_HOURS = 730


def _get(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: Optional[int] = None) -> Optional[int]:
    number = _number(value)
    return default if number is None else int(number)


def _boolean(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if str(value).strip().lower() in {"false", "0", "no"}:
        return False
    return default


def normalize_architecture(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().lower().replace("-", "")
    if normalized in {"x64", "amd64", "x86_64"}:
        return "x64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    return None


def supports_trusted_launch(catalog: Mapping[str, Any]) -> Optional[bool]:
    """Return explicit Trusted Launch support, or None when Azure omitted it."""
    explicit = catalog.get("trusted_launch_supported")
    if explicit is not None:
        return _boolean(explicit)

    capabilities = catalog.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        capabilities = _capability_map(capabilities)
    normalized = {str(key).lower(): value for key, value in capabilities.items()}

    disabled = _boolean(normalized.get("trustedlaunchdisabled"))
    if disabled is not None:
        return not disabled
    supported = _boolean(normalized.get("trustedlaunchsupported"))
    if supported is not None:
        return supported
    return None


def _capability_map(capabilities: Iterable[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for capability in capabilities or []:
        name = _get(capability, "name")
        if name:
            result[str(name)] = _get(capability, "value", default="")
    return result


def _restriction_dict(restriction: Any) -> dict[str, Any]:
    info = _get(restriction, "restriction_info", "restrictionInfo")
    info_dict = None
    if info is not None:
        info_dict = {
            "locations": list(_get(info, "locations", default=[]) or []),
            "zones": list(_get(info, "zones", default=[]) or []),
        }
    return {
        "reason_code": _get(restriction, "reason_code", "reasonCode", default=""),
        "type": _get(restriction, "type", default=""),
        "values": list(_get(restriction, "values", default=[]) or []),
        "restriction_info": info_dict,
    }


def _location_info(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _location_info(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_location_info(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {
        key: _location_info(getattr(value, key))
        for key in ("location", "zones", "extended_locations", "type", "zone_details")
        if hasattr(value, key)
    }


def _restriction_applies(restriction: Mapping[str, Any], location: Optional[str] = None,
                         zone: Optional[str] = None) -> bool:
    restriction_type = str(restriction.get("type") or "").strip().lower()
    info = restriction.get("restriction_info") or {}
    locations = {str(item).lower().replace(" ", "") for item in info.get("locations", []) or []}
    zones = {str(item).lower() for item in info.get("zones", []) or []}
    # A Zone restriction cannot be promoted to a region-wide restriction when
    # the caller did not select a zone. Keep it in the detail payload instead.
    if restriction_type == "zone" and not zone:
        return False
    if location and locations and location.lower().replace(" ", "") not in locations:
        return False
    if zone and zones and zone.lower() not in zones:
        return False
    return True


def parse_resource_sku(sku: Any, location: Optional[str] = None,
                       zone: Optional[str] = None) -> dict[str, Any]:
    capabilities = _capability_map(_get(sku, "capabilities", default=[]))
    restrictions = [
        _restriction_dict(item)
        for item in (_get(sku, "restrictions", default=[]) or [])
    ]
    restriction_reasons = []
    for restriction in restrictions:
        reason = restriction["reason_code"]
        if reason and reason not in restriction_reasons:
            restriction_reasons.append(reason)

    vcpus = _integer(capabilities.get("vCPUs"), 1)
    vcpus_available = _integer(capabilities.get("vCPUsAvailable"), vcpus)
    memory_gib = _number(capabilities.get("MemoryGB"), 1.0)
    temp_disk_mb = _number(capabilities.get("MaxResourceVolumeMB"))
    os_disk_mb = _number(capabilities.get("OSVhdSizeMB"))

    applies = [
        _restriction_applies(item, location=location, zone=zone)
        for item in restrictions
    ]
    applicable_restrictions = [
        item for item, is_applicable in zip(restrictions, applies) if is_applicable
    ]
    applicable_reasons = []
    for restriction in applicable_restrictions:
        reason = restriction["reason_code"]
        if reason and reason not in applicable_reasons:
            applicable_reasons.append(reason)

    result = {
        "name": _get(sku, "name", default=""),
        "family": _get(sku, "family", default="") or "",
        "tier": _get(sku, "tier", default="") or "",
        "architecture": normalize_architecture(capabilities.get("CpuArchitectureType")),
        "vcpus": vcpus,
        "vcpus_available": vcpus_available,
        "memory_gib": memory_gib,
        "temp_disk_gib": round(temp_disk_mb / 1024, 2) if temp_disk_mb is not None else None,
        "os_disk_gib": round(os_disk_mb / 1024, 2) if os_disk_mb is not None else None,
        "max_data_disk_count": _integer(capabilities.get("MaxDataDiskCount")),
        "max_nics": _integer(capabilities.get("MaxNetworkInterfaces")),
        "gpu_count": _integer(capabilities.get("GPUs"), 0),
        "rdma_enabled": _boolean(capabilities.get("RdmaEnabled")),
        "premium_io": _boolean(capabilities.get("PremiumIO"), False),
        "accelerated_networking_supported": _boolean(
            capabilities.get("AcceleratedNetworkingEnabled"), False
        ),
        "accelerated_networking_required": _boolean(
            capabilities.get("AcceleratedNetworkingRequired")
        ),
        "spot_capable": _boolean(capabilities.get("LowPriorityCapable"), False),
        "ephemeral_os_disk_supported": _boolean(
            capabilities.get("EphemeralOSDiskSupported"), False
        ),
        "hyperv_generations": [
            item.strip()
            for item in str(capabilities.get("HyperVGenerations", "")).split(",")
            if item.strip()
        ],
        "disk_controller_types": [
            item.strip()
            for item in str(capabilities.get("DiskControllerTypes", "")).split(",")
            if item.strip()
        ],
        "availability_zones": [],
        "capabilities": capabilities,
        "restrictions": restrictions,
        "restriction_reasons": applicable_reasons,
        "restricted": bool(applicable_restrictions),
        "selectable": not bool(applicable_restrictions),
        "restriction_applies_to_current_location": bool(applicable_restrictions),
        "trusted_launch_supported": supports_trusted_launch({"capabilities": capabilities}),
        "locations": list(_get(sku, "locations", default=[]) or []),
        "location_info": _location_info(_get(sku, "location_info", "locationInfo", default=[])),
    }

    for location_info in result["location_info"] or []:
        if isinstance(location_info, Mapping):
            result["availability_zones"].extend(location_info.get("zones", []) or [])
    result["availability_zones"] = sorted(set(result["availability_zones"]))
    return result


def parse_vm_size(size: Any) -> dict[str, Any]:
    memory_mb = _number(_get(size, "memory_in_mb", "memoryInMB"))
    return {
        "name": _get(size, "name", default=""),
        "vcpus": _integer(_get(size, "number_of_cores", "numberOfCores")),
        "vcpus_available": _integer(_get(size, "number_of_cores", "numberOfCores")),
        "memory_gib": round(memory_mb / 1024, 2) if memory_mb is not None else None,
        "memory_in_mb": _integer(_get(size, "memory_in_mb", "memoryInMB")),
        "resource_disk_size_in_mb": _integer(
            _get(size, "resource_disk_size_in_mb", "resourceDiskSizeInMB")
        ),
        "os_disk_size_in_mb": _integer(
            _get(size, "os_disk_size_in_mb", "osDiskSizeInMB")
        ),
        "max_data_disk_count": _integer(
            _get(size, "max_data_disk_count", "maxDataDiskCount")
        ),
    }


def filter_resize_candidates(
    available_sizes: Iterable[Any],
    current_architecture: Any,
    catalog_by_name: Mapping[str, Mapping[str, Any]],
    current_nic_accelerated: Optional[bool] = None,
    current_billing_mode: str = "on_demand",
    current_trusted_launch: bool = False,
) -> list[dict[str, Any]]:
    current_architecture = normalize_architecture(current_architecture)
    if not current_architecture:
        return []

    result = []
    for size in available_sizes or []:
        parsed_size = parse_vm_size(size)
        name = str(parsed_size.get("name") or "")
        catalog = catalog_by_name.get(name.lower())
        if not catalog:
            continue
        if normalize_architecture(catalog.get("architecture")) != current_architecture:
            continue

        merged = dict(catalog)
        merged.update(parsed_size)
        # List Available Sizes 的 numberOfCores 对受限 vCPU 规格可能是配额消耗量；
        # ResourceSku 的 vCPUsAvailable 是更适合展示和校验的能力值。
        if catalog.get("vcpus_available") is not None:
            merged["vcpus_available"] = catalog["vcpus_available"]
        merged["restricted"] = bool(catalog.get("restricted"))
        merged["selectable"] = not merged["restricted"]
        merged.setdefault("restriction_reasons", [])

        trusted_launch_support = supports_trusted_launch(catalog)
        if current_trusted_launch and trusted_launch_support is not True:
            merged["restricted"] = True
            merged["selectable"] = False
            reasons = list(merged.get("restriction_reasons") or [])
            reason_code = (
                "TrustedLaunchUnsupported"
                if trusted_launch_support is False
                else "TrustedLaunchSupportUnknown"
            )
            if reason_code not in reasons:
                reasons.append(reason_code)
            merged["restriction_reasons"] = reasons

        # 调整规格只更新硬件规格，不能同时修改 VM 的 Spot 或网卡状态。
        # 因此不应列出无法继承当前运行状态的可选规格；受限规格仍保留用于展示。
        if merged["selectable"]:
            if (str(current_billing_mode).lower() == "spot"
                    and merged.get("spot_capable") is not True):
                continue
            if (current_nic_accelerated is True
                    and merged.get("accelerated_networking_supported") is not True):
                continue
            if (current_nic_accelerated is False
                    and merged.get("accelerated_networking_required") is True):
                continue
        result.append(merged)

    # Azure List Available Sizes 可能主动省略受限规格，但产品要求同架构受限项仍可见。
    # 这些补充项只用于展示，必须保持 disabled，不能被当成 Azure 已确认的可调整项。
    available_names = {str(item.get("name") or "").lower() for item in result}
    for name, catalog in catalog_by_name.items():
        if name in available_names:
            continue
        if normalize_architecture(catalog.get("architecture")) != current_architecture:
            continue
        if not catalog.get("restricted"):
            continue
        merged = dict(catalog)
        merged["selectable"] = False
        merged["display_only"] = True
        merged["source"] = "resource_skus_restricted"
        result.append(merged)
    return result


def monthly_estimate(hourly_price: Any, hours: int = MONTHLY_HOURS) -> Optional[float]:
    if hourly_price is None or hourly_price == "":
        return None
    try:
        hourly = Decimal(str(hourly_price))
        total = hourly * Decimal(str(hours))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not hourly.is_finite() or not total.is_finite():
        return None
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _price_value(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return default


def _is_spot_price(record: Mapping[str, Any]) -> bool:
    meter = str(_price_value(record, "meterName", "meter_name", default="")).lower()
    sku = str(_price_value(record, "skuName", "sku_name", default="")).lower()
    return "spot" in meter or "spot" in sku


def _is_low_priority_price(record: Mapping[str, Any]) -> bool:
    values = (
        _price_value(record, "meterName", "meter_name", default=""),
        _price_value(record, "skuName", "sku_name", default=""),
        _price_value(record, "productName", "product_name", default=""),
    )
    normalized = " ".join(str(value or "").lower() for value in values)
    return any(marker in normalized for marker in ("low priority", "lowpriority", "low_priority"))


def _is_windows_price(record: Mapping[str, Any]) -> bool:
    product = str(_price_value(record, "productName", "product_name", default="")).lower()
    return "windows" in product


def is_retail_price_compatible(
    record: Mapping[str, Any], os_type: str, billing_mode: str
) -> bool:
    """判断缓存的 Retail Prices 记录是否仍匹配页面价格维度。"""
    if not record:
        return False
    expected_windows = str(os_type).strip().lower() == "windows"
    expected_spot = str(billing_mode).strip().lower() == "spot"
    if _is_spot_price(record) != expected_spot:
        return False
    if not expected_spot and _is_low_priority_price(record):
        return False
    # 以 serviceName 区分产品。Virtual Machines 的 productName 并不固定以
    # “Virtual Machines” 开头，例如 DCdsv3 返回的是 “DCdsv3 Series Linux”。
    product_name = str(
        _price_value(record, "productName", "product_name", default="")
    ).strip().lower()
    # Retail Prices 将旧版 Cloud Services 计价项也归在 serviceName=Virtual Machines
    # 下；它们不是 ARM VM 的计算价格，必须在 serviceName 判断之后再次排除。
    if "cloud services" in product_name:
        return False
    service_name = str(
        _price_value(record, "serviceName", "service_name", default="")
    ).strip().lower()
    if service_name:
        if service_name != "virtual machines":
            return False
    elif "cloud services" in product_name:
        # 兼容旧测试/旧缓存中缺少 serviceName 的记录，仍排除明显的云服务报价。
        return False
    if _is_windows_price(record) != expected_windows:
        return False
    price_type = str(_price_value(record, "type", "priceType", default="Consumption"))
    if price_type.lower() != "consumption":
        return False
    return _number(_price_value(record, "retailPrice", "retail_price")) is not None


def select_retail_price(
    records: Iterable[Mapping[str, Any]],
    sku_name: str,
    os_type: str,
    billing_mode: str,
) -> Optional[dict[str, Any]]:
    expected_sku = str(sku_name).strip().lower()
    expected_spot = str(billing_mode).strip().lower() == "spot"
    candidates = []
    for record in records or []:
        arm_sku = str(_price_value(record, "armSkuName", "arm_sku_name", default="")).lower()
        if arm_sku != expected_sku:
            continue
        if not is_retail_price_compatible(record, os_type, billing_mode):
            continue
        is_primary = _boolean(
            _price_value(record, "isPrimaryMeterRegion", "is_primary_meter_region"),
            True,
        ) is not False
        candidates.append((is_primary, record))

    def sort_key(record: Mapping[str, Any]):
        effective = str(_price_value(record, "effectiveStartDate", "effective_start_date", default=""))
        tier = _number(_price_value(record, "tierMinimumUnits", "tier_minimum_units"), 0) or 0
        return (effective, -tier)

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], *sort_key(item[1])), reverse=True)
    return dict(candidates[0][1])


def resolve_accelerated_networking(
    size_supported: Any,
    size_required: Any,
    image_supported: Any,
    requested: Any,
) -> dict[str, Any]:
    supported = bool(size_supported)
    required = bool(size_required)
    requested = bool(requested)

    if not supported:
        return {
            "state": "unsupported",
            "enabled": False,
            "selectable": False,
            "error": "规格不支持网络加速",
        }
    if required:
        if image_supported is False:
            return {
                "state": "required",
                "enabled": False,
                "selectable": False,
                "error": "镜像不支持必须的网络加速",
            }
        return {"state": "required", "enabled": True, "selectable": False, "error": None}
    if requested and image_supported is not True:
        return {
            "state": "optional",
            "enabled": False,
            "selectable": True,
            "error": "镜像不支持网络加速",
        }
    return {"state": "optional", "enabled": requested, "selectable": True, "error": None}
