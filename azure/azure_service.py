# coding: utf-8
import os
import time
import base64
import hashlib
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from azure.identity import ClientSecretCredential
from azure.mgmt.subscription import SubscriptionClient
try:
    from azure.mgmt.resource.resources import ResourceManagementClient
except ImportError:
    from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import QueryDefinition, TimeframeType, QueryDataset, QueryAggregation
from azure.mgmt.consumption import ConsumptionManagementClient
try:
    from .sku_catalog import parse_resource_sku, filter_resize_candidates, normalize_architecture
except ImportError:
    from sku_catalog import parse_resource_sku, filter_resize_candidates, normalize_architecture

logger = logging.getLogger("azure_service")


def _resolve_azure_timeout():
    try:
        value = int(os.getenv("AZURE_OPERATION_TIMEOUT_SECONDS", "1800"))
    except ValueError:
        value = 1800
    return min(max(value, 60), 7200)


AZURE_OPERATION_TIMEOUT_SECONDS = _resolve_azure_timeout()
VM_RESIZE_OPTIONS_CACHE_TTL_SECONDS = 60
RETAIL_PRICES_CACHE_TTL_SECONDS = 6 * 60 * 60
AZURE_RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"


def wait_for_azure_poller(poller):
    """所有 Azure 长任务都必须有应用层超时，避免后台线程永久占用。"""
    if hasattr(poller, "result"):
        return poller.result(timeout=AZURE_OPERATION_TIMEOUT_SECONDS)
    return poller.wait(timeout=AZURE_OPERATION_TIMEOUT_SECONDS)


# 官方主流系统镜像元数据定义 (严格标注架构支持，绝不进行虚假跨架构重定向)
PRESET_IMAGES = {
    "Ubuntu_24_04_LTS": {
        "name": "Ubuntu Server 24.04 LTS",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"},
        "arm64": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server-arm64", "version": "latest"}
    },
    "Ubuntu_22_04_LTS": {
        "name": "Ubuntu Server 22.04 LTS",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy", "sku": "22_04-lts-gen2", "version": "latest"},
        "arm64": {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy", "sku": "22_04-lts-arm64", "version": "latest"}
    },
    "Ubuntu_20_04_LTS": {
        "name": "Ubuntu Server 20.04 LTS",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-focal", "sku": "20_04-lts-gen2", "version": "latest"},
        "arm64": {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-focal", "sku": "20_04-lts-arm64", "version": "latest"}
    },
    "Debian_12": {
        "name": "Debian 12 (Bookworm)",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "Debian", "offer": "debian-12", "sku": "12-gen2", "version": "latest"},
        "arm64": {"publisher": "Debian", "offer": "debian-12", "sku": "12-arm64", "version": "latest"}
    },
    "Debian_11": {
        "name": "Debian 11 (Bullseye)",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "Debian", "offer": "debian-11", "sku": "11-gen2", "version": "latest"},
        "arm64": {"publisher": "Debian", "offer": "debian-11", "sku": "11-backports-arm64-v2", "version": "latest"}
    },
    "AlmaLinux_9": {
        "name": "AlmaLinux 9",
        "os_type": "Linux",
        "supported_archs": ["x64", "arm64"],
        "x64": {"publisher": "almalinux", "offer": "almalinux-x86_64", "sku": "9-gen2", "version": "latest"},
        "arm64": {"publisher": "almalinux", "offer": "almalinux-arm", "sku": "9-arm-gen2", "version": "latest"}
    },
    "RockyLinux_9": {
        "name": "Rocky Linux 9",
        "os_type": "Linux",
        "supported_archs": ["x64"],
        "x64": {"publisher": "resf", "offer": "rockylinux-x86_64", "sku": "9-base", "version": "latest"}
    },
    "Windows_11_Pro": {
        "name": "Windows 11 Pro (23H2)",
        "os_type": "Windows",
        "supported_archs": ["x64"],
        "x64": {"publisher": "MicrosoftWindowsDesktop", "offer": "windows-11", "sku": "win11-23h2-pro", "version": "latest"}
    },
    "Windows_10_Pro": {
        "name": "Windows 10 Pro (22H2)",
        "os_type": "Windows",
        "supported_archs": ["x64"],
        "x64": {"publisher": "MicrosoftWindowsDesktop", "offer": "windows-10", "sku": "win10-22h2-pro-g2", "version": "latest"}
    },
    "Windows_Server_2025": {
        "name": "Windows Server 2025 Datacenter",
        "os_type": "Windows",
        "supported_archs": ["x64"],
        "x64": {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer", "sku": "2025-datacenter-azure-edition", "version": "latest"}
    },
    "Windows_Server_2022": {
        "name": "Windows Server 2022 Datacenter",
        "os_type": "Windows",
        "supported_archs": ["x64"],
        "x64": {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer", "sku": "2022-datacenter-azure-edition-smalldisk", "version": "latest"}
    },
    "Windows_Server_2019": {
        "name": "Windows Server 2019 Datacenter",
        "os_type": "Windows",
        "supported_archs": ["x64"],
        "x64": {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer", "sku": "2019-Datacenter-smalldisk", "version": "latest"}
    }
}

class AzureService:
    @staticmethod
    def is_arm_vm_size(vm_size: str) -> bool:
        """
        严格匹配 Azure 官方目前所有 ARM64 (Ampere Altra) 机型系列：
        1. B-series v2 ARM (突发型): Standard_B*ps_v2, Standard_B*pds_v2, Standard_B*pts_v2
        2. D-series v5/v6 ARM (通用计算): Standard_D*ps_v5, Standard_D*pds_v5, Standard_D*ps_v6, Standard_D*pds_v6, Standard_D*plds_v5, Standard_D*pls_v5
        3. E-series v5/v6 ARM (内存优化): Standard_E*ps_v5, Standard_E*pds_v5, Standard_E*ps_v6, Standard_E*pds_v6
        """
        if not vm_size:
            return False
        import re
        s = vm_size.strip()
        pattern = r"(?i)^Standard_([BDE])\d+(p|ps|pds|pts|pls|plds)_v\d+$"
        if re.match(pattern, s):
            return True
        # 兼容其他包含 p 系列关键字的 ARM 模式
        s_lower = s.lower()
        if any(keyword in s_lower for keyword in ["_b1ps_", "_b2ps_", "_b4ps_", "_b8ps_", "_b2pts_", "_b4pts_", "_b8pts_", "_b16pts_", "_d2ps_", "_d4ps_", "_d8ps_", "_d16ps_", "_d32ps_", "_d48ps_", "_d64ps_", "_d2pds_", "_d4pds_", "_d8pds_", "_d16pds_", "_d32pds_", "_d48pds_", "_d64pds_", "_e2ps_", "_e4ps_", "_e8ps_", "_e16ps_", "_e20ps_", "_e32ps_", "_e2pds_", "_e4pds_", "_e8pds_", "_e16pds_", "_e20pds_", "_e32pds_"]):
            return True
        return False

    # 全局客户端连接池缓存，避免每次请求重复创建 TLS 握手与认证开销
    _clients_pool = {}
    _vm_cache = {}
    _resize_options_cache = {}
    _resize_options_cache_lock = threading.Lock()
    _retail_prices_cache = {}
    _retail_prices_cache_locks = {}
    _retail_prices_cache_lock = threading.Lock()

    @staticmethod
    def secret_fingerprint(client_secret: str) -> str:
        return hashlib.sha256(client_secret.strip().encode("utf-8")).hexdigest()[:16]

    @classmethod
    def get_credential(cls, tenant_id: str, client_id: str, client_secret: str) -> ClientSecretCredential:
        tenant = tenant_id.strip()
        client = client_id.strip()
        secret_fingerprint = cls.secret_fingerprint(client_secret)
        key = f"cred_{tenant}_{client}_{secret_fingerprint}"
        if key not in cls._clients_pool:
            cls._clients_pool[key] = ClientSecretCredential(
                tenant_id=tenant,
                client_id=client,
                client_secret=client_secret.strip()
            )
        return cls._clients_pool[key]

    @classmethod
    def get_compute_client(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> ComputeManagementClient:
        key = f"compute_{tenant_id}_{client_id}_{cls.secret_fingerprint(client_secret)}_{subscription_id}"
        if key not in cls._clients_pool:
            cred = cls.get_credential(tenant_id, client_id, client_secret)
            cls._clients_pool[key] = ComputeManagementClient(cred, subscription_id)
        return cls._clients_pool[key]

    @classmethod
    def get_network_client(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> NetworkManagementClient:
        key = f"net_{tenant_id}_{client_id}_{cls.secret_fingerprint(client_secret)}_{subscription_id}"
        if key not in cls._clients_pool:
            cred = cls.get_credential(tenant_id, client_id, client_secret)
            cls._clients_pool[key] = NetworkManagementClient(cred, subscription_id)
        return cls._clients_pool[key]

    @classmethod
    def get_monitor_client(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> MonitorManagementClient:
        key = f"mon_{tenant_id}_{client_id}_{cls.secret_fingerprint(client_secret)}_{subscription_id}"
        if key not in cls._clients_pool:
            cred = cls.get_credential(tenant_id, client_id, client_secret)
            cls._clients_pool[key] = MonitorManagementClient(cred, subscription_id)
        return cls._clients_pool[key]

    @classmethod
    def get_resource_client(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> ResourceManagementClient:
        key = f"res_{tenant_id}_{client_id}_{subscription_id}"
        if key not in cls._clients_pool:
            cred = cls.get_credential(tenant_id, client_id, client_secret)
            cls._clients_pool[key] = ResourceManagementClient(cred, subscription_id)
        return cls._clients_pool[key]

    @classmethod
    def list_subscriptions(cls, tenant_id: str, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
        """自动识别并列出此凭证可访问的所有订阅"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        sub_client = SubscriptionClient(cred)
        results = []
        for sub in sub_client.subscriptions.list():
            spending_limit = "None"
            if hasattr(sub, 'subscription_policies') and sub.subscription_policies:
                spending_limit = getattr(sub.subscription_policies, 'spending_limit', 'None') or "None"
            results.append({
                "subscription_id": sub.subscription_id,
                "display_name": sub.display_name or sub.subscription_id,
                "state": sub.state.value if hasattr(sub.state, 'value') else str(sub.state),
                "spending_limit": str(spending_limit)
            })
        return results

    @classmethod
    def list_locations(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> List[Dict[str, str]]:
        """动态获取订阅可用的 Azure 地域及其官方显示名称"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        sub_client = SubscriptionClient(cred)
        results = []
        for location in sub_client.subscriptions.list_locations(subscription_id):
            name = getattr(location, "name", None)
            if not name:
                continue
            location_type = getattr(location, "type", None)
            if location_type:
                location_type = getattr(location_type, "value", location_type)
                if str(location_type).lower() != "region":
                    continue
            results.append({
                "name": name,
                "display_name": getattr(location, "display_name", None) or name
            })
        results.sort(key=lambda item: item["display_name"].lower())
        for index, item in enumerate(results):
            if item["name"].lower() == "eastasia":
                results.insert(0, results.pop(index))
                break
        return results

    @classmethod
    def fetch_and_cache_skus(cls, tenant_id: str, client_id: str, client_secret: str,
                             subscription_id: str, location: str) -> List[Dict[str, Any]]:
        """从 Azure 动态拉取指定地域的完整虚拟机规格能力。"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)

        # 过滤 location
        loc = location.lower().replace(" ", "")
        skus_iter = compute_client.resource_skus.list(filter=f"location eq '{loc}'")

        sku_list = []
        for s in skus_iter:
            if str(getattr(s, "resource_type", "")).lower() != "virtualmachines":
                continue

            # 只应用当前地域的限制；否则一个 SKU 在其他地域的限制会被
            # 错误地带入当前地域，导致可用规格被整体标成受限。
            parsed = parse_resource_sku(s, location=loc)
            # 保留旧字段别名，页面和旧数据库迁移期间可以平滑切换。
            parsed["memory_gb"] = parsed["memory_gib"]
            parsed["accelerated_networking"] = parsed["accelerated_networking_supported"]
            parsed["restrictions"] = ", ".join(parsed["restriction_reasons"])
            sku_list.append(parsed)

        # 排序：优先按 vCPU, 内存, 名称
        sku_list.sort(key=lambda x: (x['vcpus'] or 0, x['memory_gib'] or 0, x['name']))
        return sku_list

    @staticmethod
    def _retail_price_filter(location: str, sku_names=None) -> str:
        escaped_location = str(location).strip().lower().replace("'", "''")
        # Retail Prices API 不接受多个 armSkuName 的 OR 组合过滤。
        # 一次拉取指定地域的 Virtual Machines Consumption 目录，随后在本地
        # 按 SKU、系统和计费模式选择，并由进程缓存复用目录结果。
        return (
            "serviceName eq 'Virtual Machines' and "
            "priceType eq 'Consumption' and "
            f"armRegionName eq '{escaped_location}'"
        )

    @classmethod
    def fetch_retail_prices(cls, location: str, currency: str = "USD",
                            sku_names=None) -> List[Dict[str, Any]]:
        """按地域读取 Azure Retail Prices 目录，并按请求 SKU 在本地筛选。"""
        normalized_location = str(location).strip().lower().replace(" ", "")
        normalized_currency = str(currency or "USD").upper()
        cache_key = (normalized_location, normalized_currency)
        with cls._retail_prices_cache_lock:
            fetch_lock = cls._retail_prices_cache_locks.setdefault(cache_key, threading.Lock())

        with fetch_lock:
            now = time.monotonic()
            with cls._retail_prices_cache_lock:
                cached = cls._retail_prices_cache.get(cache_key)
            if cached and now - cached[0] < RETAIL_PRICES_CACHE_TTL_SECONDS:
                return [dict(item) for item in cached[1]]

            url = AZURE_RETAIL_PRICES_URL
            params = {
                "api-version": "2023-01-01-preview",
                "currencyCode": normalized_currency,
                "$filter": cls._retail_price_filter(normalized_location),
            }
            records = []
            while url:
                response = None
                for attempt in range(3):
                    response = requests.get(url, params=params, timeout=30)
                    if response.status_code != 429 or attempt == 2:
                        break
                    retry_after = response.headers.get("Retry-After", "1")
                    try:
                        delay = min(max(float(retry_after), 0.5), 5.0)
                    except (TypeError, ValueError):
                        delay = 1.0
                    time.sleep(delay)
                response.raise_for_status()
                payload = response.json() or {}
                records.extend(payload.get("Items") or payload.get("items") or [])
                url = payload.get("NextPageLink") or payload.get("nextPageLink")
                params = None  # NextPageLink 已包含分页参数，不能重复拼接筛选器。

            with cls._retail_prices_cache_lock:
                cls._retail_prices_cache[cache_key] = (time.monotonic(), records)
            return [dict(item) for item in records]

    # 镜像缓存由 Flask 接口持久化到 ImageCache，服务层始终负责动态探测

    @classmethod
    def fetch_dynamic_images(cls, tenant_id: str, client_id: str, client_secret: str,
                             subscription_id: str, location: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        从 Azure Compute API 动态探测指定地域下所有官方主流发行版当前真实活跃的 SKUs (区分 x64 / arm64)
        自动发现最新版本并自动剔除已废弃/下架的无效版本，杜绝后台重定向
        """
        loc = location.lower().replace(" ", "")
        now_ts = time.time()
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)

        # 待探测的目标官方发布者与产品线
        discovery_targets = [
            ("Ubuntu_24_04_LTS", "Canonical", "ubuntu-24_04-lts"),
            ("Ubuntu_22_04_LTS", "Canonical", "0001-com-ubuntu-server-jammy"),
            ("Ubuntu_20_04_LTS", "Canonical", "0001-com-ubuntu-server-focal"),
            ("Debian_12", "Debian", "debian-12"),
            ("Debian_11", "Debian", "debian-11"),
            ("AlmaLinux_9", "almalinux", "almalinux-x86_64"),
            ("AlmaLinux_9_ARM", "almalinux", "almalinux-arm"),
            ("RockyLinux_9", "resf", "rockylinux-x86_64"),
            ("Windows_11_Pro", "MicrosoftWindowsDesktop", "windows-11"),
            ("Windows_10_Pro", "MicrosoftWindowsDesktop", "windows-10"),
            ("Windows_Server", "MicrosoftWindowsServer", "WindowsServer")
        ]

        import concurrent.futures
        discovered_skus = {}

        def scan_target(item):
            key, pub, off = item
            try:
                skus = [s.name for s in compute_client.virtual_machine_images.list_skus(loc, pub, off)]
                return key, (pub, off, skus)
            except Exception as e:
                logger.warning(f"Scan image error for {pub}:{off} in {loc}: {e}")
                return key, (pub, off, [])

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            for k, val in executor.map(scan_target, discovery_targets):
                discovered_skus[k] = val

        # 动态组装支持的 x64 与 arm64 真实镜像清单
        x64_images = []
        arm64_images = []

        # 1. Ubuntu 24.04
        pub, off, skus = discovered_skus.get("Ubuntu_24_04_LTS", ("", "", []))
        if "server" in skus:
            x64_images.append({"key": "Ubuntu_24_04_LTS", "name": "Ubuntu Server 24.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:server:latest"})
        if "server-arm64" in skus:
            arm64_images.append({"key": "Ubuntu_24_04_LTS", "name": "Ubuntu Server 24.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:server-arm64:latest"})

        # 2. Ubuntu 22.04
        pub, off, skus = discovered_skus.get("Ubuntu_22_04_LTS", ("", "", []))
        if "22_04-lts-gen2" in skus or "22_04-lts" in skus:
            s_sku = "22_04-lts-gen2" if "22_04-lts-gen2" in skus else "22_04-lts"
            x64_images.append({"key": "Ubuntu_22_04_LTS", "name": "Ubuntu Server 22.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:{s_sku}:latest"})
        if "22_04-lts-arm64" in skus:
            arm64_images.append({"key": "Ubuntu_22_04_LTS", "name": "Ubuntu Server 22.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:22_04-lts-arm64:latest"})

        # 3. Ubuntu 20.04
        pub, off, skus = discovered_skus.get("Ubuntu_20_04_LTS", ("", "", []))
        if "20_04-lts-gen2" in skus or "20_04-lts" in skus:
            s_sku = "20_04-lts-gen2" if "20_04-lts-gen2" in skus else "20_04-lts"
            x64_images.append({"key": "Ubuntu_20_04_LTS", "name": "Ubuntu Server 20.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:{s_sku}:latest"})
        if "20_04-lts-arm64" in skus:
            arm64_images.append({"key": "Ubuntu_20_04_LTS", "name": "Ubuntu Server 20.04 LTS", "os_type": "Linux", "urn": f"{pub}:{off}:20_04-lts-arm64:latest"})

        # 4. Debian 12
        pub, off, skus = discovered_skus.get("Debian_12", ("", "", []))
        if "12-gen2" in skus or "12" in skus:
            s_sku = "12-gen2" if "12-gen2" in skus else "12"
            x64_images.append({"key": "Debian_12", "name": "Debian 12 (Bookworm)", "os_type": "Linux", "urn": f"{pub}:{off}:{s_sku}:latest"})
        if "12-arm64" in skus:
            arm64_images.append({"key": "Debian_12", "name": "Debian 12 (Bookworm)", "os_type": "Linux", "urn": f"{pub}:{off}:12-arm64:latest"})

        # 5. Debian 11
        pub, off, skus = discovered_skus.get("Debian_11", ("", "", []))
        if "11-gen2" in skus or "11" in skus:
            s_sku = "11-gen2" if "11-gen2" in skus else "11"
            x64_images.append({"key": "Debian_11", "name": "Debian 11 (Bullseye)", "os_type": "Linux", "urn": f"{pub}:{off}:{s_sku}:latest"})
        if "11-backports-arm64-v2" in skus:
            arm64_images.append({"key": "Debian_11", "name": "Debian 11 (Bullseye)", "os_type": "Linux", "urn": f"{pub}:{off}:11-backports-arm64-v2:latest"})

        # 6. AlmaLinux 9
        pub_x, off_x, skus_x = discovered_skus.get("AlmaLinux_9", ("", "", []))
        if "9-gen2" in skus_x or "9-gen1" in skus_x:
            x64_images.append({"key": "AlmaLinux_9", "name": "AlmaLinux 9", "os_type": "Linux", "urn": f"{pub_x}:{off_x}:9-gen2:latest"})
        pub_a, off_a, skus_a = discovered_skus.get("AlmaLinux_9_ARM", ("", "", []))
        if "9-arm-gen2" in skus_a:
            arm64_images.append({"key": "AlmaLinux_9", "name": "AlmaLinux 9", "os_type": "Linux", "urn": f"{pub_a}:{off_a}:9-arm-gen2:latest"})

        # 7. Rocky Linux 9 (仅 x64 官方存在)
        pub, off, skus = discovered_skus.get("RockyLinux_9", ("", "", []))
        if "9-base" in skus:
            x64_images.append({"key": "RockyLinux_9", "name": "Rocky Linux 9", "os_type": "Linux", "urn": f"{pub}:{off}:9-base:latest"})

        # 8. Windows 11 / 10
        pub, off, skus = discovered_skus.get("Windows_11_Pro", ("", "", []))
        win11_pro_skus = [s for s in skus if "pro" in s.lower() and "zh-cn" not in s.lower() and "pron" not in s.lower()]
        if win11_pro_skus:
            latest_w11_sku = sorted(win11_pro_skus, reverse=True)[0]
            x64_images.append({"key": "Windows_11_Pro", "name": f"Windows 11 Pro ({latest_w11_sku.replace('win11-', '').replace('-pro', '').upper()})", "os_type": "Windows", "urn": f"{pub}:{off}:{latest_w11_sku}:latest"})

        pub, off, skus = discovered_skus.get("Windows_10_Pro", ("", "", []))
        if "win10-22h2-pro-g2" in skus or "win10-22h2-pro" in skus:
            s_sku = "win10-22h2-pro-g2" if "win10-22h2-pro-g2" in skus else "win10-22h2-pro"
            x64_images.append({"key": "Windows_10_Pro", "name": "Windows 10 Pro (22H2)", "os_type": "Windows", "urn": f"{pub}:{off}:{s_sku}:latest"})

        # 9. Windows Server
        pub, off, skus = discovered_skus.get("Windows_Server", ("", "", []))
        if any("2025-datacenter" in s for s in skus):
            x64_images.append({"key": "Windows_Server_2025", "name": "Windows Server 2025 Datacenter", "os_type": "Windows", "urn": f"{pub}:{off}:2025-datacenter-azure-edition:latest"})
        if any("2022-datacenter" in s for s in skus):
            x64_images.append({"key": "Windows_Server_2022", "name": "Windows Server 2022 Datacenter", "os_type": "Windows", "urn": f"{pub}:{off}:2022-datacenter-azure-edition-smalldisk:latest"})
        if any("2019-datacenter" in s.lower() for s in skus):
            x64_images.append({"key": "Windows_Server_2019", "name": "Windows Server 2019 Datacenter", "os_type": "Windows", "urn": f"{pub}:{off}:2019-Datacenter-smalldisk:latest"})

        result = {
            "location": loc,
            "x64": x64_images,
            "arm64": arm64_images,
            "updated_at": now_ts
        }
        return result

    @classmethod
    def list_vms(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
        cache_key = f"vms_{subscription_id}"
        now_ts = time.time()
        if not force_refresh and cache_key in cls._vm_cache:
            cache_time, cached_data = cls._vm_cache[cache_key]
            if now_ts - cache_time < 30:
                return cached_data

        import concurrent.futures
        compute_client = cls.get_compute_client(tenant_id, client_id, client_secret, subscription_id)
        network_client = cls.get_network_client(tenant_id, client_id, client_secret, subscription_id)

        # 1. 并行拉取 VM 基础列表、Public IP 和 NIC
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_vms = executor.submit(lambda: list(compute_client.virtual_machines.list_all()))
            fut_pips = executor.submit(lambda: list(network_client.public_ip_addresses.list_all()))
            fut_nics = executor.submit(lambda: list(network_client.network_interfaces.list_all()))

            vms_raw = fut_vms.result()
            pips_raw = fut_pips.result()
            nics_raw = fut_nics.result()

        # 并发补充获取各 VM 的实时电源状态 (精准提取 PowerState/deallocated 关机状态)
        vm_iv_map = {}
        if vms_raw:
            def fetch_single_iv(v):
                v_rg = v.id.split("/resourceGroups/")[1].split("/")[0]
                try:
                    iv = compute_client.virtual_machines.instance_view(v_rg, v.name)
                    return (v.id.lower(), iv)
                except Exception:
                    return (v.id.lower(), None)

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(vms_raw))) as iv_exec:
                iv_pairs = list(iv_exec.map(fetch_single_iv, vms_raw))
                for vid, iv_obj in iv_pairs:
                    if iv_obj:
                        vm_iv_map[vid] = iv_obj

        # 建立不区分大小写 (case-insensitive) 的 Public IP 和 NIC 字典映射
        public_ips_map = {}
        for pip in pips_raw:
            if pip.id:
                ip_addr = pip.ip_address or "Allocating"
                public_ips_map[pip.id.lower()] = ip_addr
                public_ips_map[pip.id] = ip_addr

        nics_map = {}
        for nic in nics_raw:
            if nic.id:
                nics_map[nic.id.lower()] = nic
                nics_map[nic.id] = nic

        vm_list = []
        for vm in vms_raw:
            rg = vm.id.split("/resourceGroups/")[1].split("/")[0]

            # 解析真实实时状态 (优先从 instance_view 获取精准电源状态)
            power_state = "Running"
            provisioning_state = vm.provisioning_state or "Succeeded"
            iv_data = vm_iv_map.get(vm.id.lower()) or vm.instance_view
            if iv_data and iv_data.statuses:
                for st in iv_data.statuses:
                    if st.code and st.code.startswith("PowerState/"):
                        power_state = st.display_status or st.code.split("/")[1]

            private_ip = "-"
            public_ip = "-"
            nic_id = None
            nic_name = "-"
            nsg_name = "-"
            if vm.network_profile and vm.network_profile.network_interfaces:
                # 遍历可能存在的多网卡或首块网卡
                for net_if in vm.network_profile.network_interfaces:
                    target_nic_id = net_if.id
                    if not target_nic_id:
                        continue
                    nic = nics_map.get(target_nic_id.lower()) or nics_map.get(target_nic_id)
                    if not nic:
                        # 尝试通过 Resource Group 和 NIC Name 补充查询
                        try:
                            rg_name = target_nic_id.split("/resourceGroups/")[1].split("/")[0]
                            n_name = target_nic_id.split("/")[-1]
                            nic = network_client.network_interfaces.get(rg_name, n_name)
                            if nic:
                                nics_map[target_nic_id.lower()] = nic
                        except Exception:
                            pass

                    if nic:
                        nic_id = target_nic_id
                        nic_name = nic.name or target_nic_id.split("/")[-1]
                        if nic.network_security_group and nic.network_security_group.id:
                            nsg_name = nic.network_security_group.id.split("/")[-1]

                        if nic.ip_configurations:
                            for ip_cfg in nic.ip_configurations:
                                if ip_cfg.private_ip_address and private_ip == "-":
                                    private_ip = ip_cfg.private_ip_address
                                if ip_cfg.public_ip_address:
                                    if hasattr(ip_cfg.public_ip_address, 'ip_address') and ip_cfg.public_ip_address.ip_address:
                                        public_ip = ip_cfg.public_ip_address.ip_address
                                    elif ip_cfg.public_ip_address.id:
                                        pip_id_val = ip_cfg.public_ip_address.id
                                        public_ip = public_ips_map.get(pip_id_val.lower(), public_ips_map.get(pip_id_val, "-"))
                                        if public_ip == "-":
                                            try:
                                                p_rg = pip_id_val.split("/resourceGroups/")[1].split("/")[0]
                                                p_name = pip_id_val.split("/")[-1]
                                                pip_obj = network_client.public_ip_addresses.get(p_rg, p_name)
                                                if pip_obj and pip_obj.ip_address:
                                                    public_ip = pip_obj.ip_address
                                                    public_ips_map[pip_id_val.lower()] = public_ip
                                            except Exception:
                                                pass
                            if private_ip != "-" or public_ip != "-":
                                break

            # 解析操作系统显示名称
            os_display = "Linux"
            image_ref = "-"
            if vm.storage_profile:
                if vm.storage_profile.image_reference:
                    ir = vm.storage_profile.image_reference
                    matched = False
                    for _, preset in PRESET_IMAGES.items():
                        x64_c = preset.get("x64", {})
                        arm_c = preset.get("arm64", {})
                        if (x64_c.get("offer") == ir.offer and x64_c.get("sku") == ir.sku) or \
                           (arm_c.get("offer") == ir.offer and arm_c.get("sku") == ir.sku):
                            os_display = preset["name"]
                            matched = True
                            break
                    if not matched:
                        if ir.offer and ir.sku:
                            os_display = f"{ir.offer} ({ir.sku})"
                        elif ir.id:
                            os_display = ir.id.split("/")[-1]
                    if ir.publisher:
                        image_ref = f"{ir.publisher}:{ir.offer}:{ir.sku}:{ir.version}"
                elif vm.storage_profile.os_disk and vm.storage_profile.os_disk.os_type:
                    raw_type = str(vm.storage_profile.os_disk.os_type.value if hasattr(vm.storage_profile.os_disk.os_type, 'value') else vm.storage_profile.os_disk.os_type)
                    os_display = "Windows" if "windows" in raw_type.lower() else "Linux"

            # 探测认证方式
            is_win = "windows" in os_display.lower()
            auth_mode = "password"
            if not is_win and vm.os_profile:
                if vm.os_profile.linux_configuration:
                    lcfg = vm.os_profile.linux_configuration
                    if lcfg.disable_password_authentication is True or (lcfg.ssh and lcfg.ssh.public_keys and len(lcfg.ssh.public_keys) > 0):
                        auth_mode = "ssh_key"

            admin_user = vm.os_profile.admin_username if (vm.os_profile and vm.os_profile.admin_username) else "azureuser"

            vm_list.append({
                "name": vm.name,
                "resource_group": rg,
                "location": vm.location,
                "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else "-",
                "os_type": os_display,
                "power_state": power_state,
                "provisioning_state": provisioning_state,
                "private_ip": private_ip,
                "public_ip": public_ip,
                "nic_id": nic_id,
                "nic_name": nic_name,
                "nsg_name": nsg_name,
                "image_ref": image_ref,
                "admin_username": admin_user,
                "auth_mode": auth_mode,
                "os_disk_id": vm.storage_profile.os_disk.managed_disk.id if (vm.storage_profile and vm.storage_profile.os_disk and vm.storage_profile.os_disk.managed_disk) else None,
                "os_disk_name": vm.storage_profile.os_disk.name if (vm.storage_profile and vm.storage_profile.os_disk) else None
            })

        cls._vm_cache[cache_key] = (now_ts, vm_list)
        return vm_list

        cls._vm_cache[cache_key] = (now_ts, vm_list)
        return vm_list

    @classmethod
    def get_vm_detail(cls, tenant_id: str, client_id: str, client_secret: str,
                      subscription_id: str, resource_group: str, vm_name: str) -> Dict[str, Any]:
        """获取单个 VM 详细信息"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)
        network_client = NetworkManagementClient(cred, subscription_id)

        vm = compute_client.virtual_machines.get(resource_group, vm_name, expand="instanceView")

        power_state = "Unknown"
        if vm.instance_view and vm.instance_view.statuses:
            for st in vm.instance_view.statuses:
                if st.code and st.code.startswith("PowerState/"):
                    power_state = st.display_status or st.code.split("/")[1]

        # 网络信息
        nic_name = "-"
        private_ip = "-"
        public_ip = "-"
        public_ip_name = "-"
        nsg_name = "-"
        accelerated_networking = None

        if vm.network_profile and vm.network_profile.network_interfaces:
            nic_id = vm.network_profile.network_interfaces[0].id
            nic_name = nic_id.split("/")[-1]
            try:
                nic = network_client.network_interfaces.get(resource_group, nic_name)
                accelerated_networking = getattr(nic, "enable_accelerated_networking", None)
                if nic.network_security_group:
                    nsg_name = nic.network_security_group.id.split("/")[-1]
                if nic.ip_configurations:
                    ip_config = nic.ip_configurations[0]
                    private_ip = ip_config.private_ip_address or "-"
                    if ip_config.public_ip_address:
                        public_ip_name = ip_config.public_ip_address.id.split("/")[-1]
                        pip = network_client.public_ip_addresses.get(resource_group, public_ip_name)
                        public_ip = pip.ip_address or "-"
            except Exception as e:
                logger.warning(f"Error fetching NIC details: {e}")

        # OS 镜像信息与操作系统友好名称
        image_ref = "-"
        os_display = "Linux"
        if vm.storage_profile:
            if vm.storage_profile.image_reference:
                ir = vm.storage_profile.image_reference
                matched = False
                for _, preset in PRESET_IMAGES.items():
                    x64_c = preset.get("x64", {})
                    arm_c = preset.get("arm64", {})
                    if (x64_c.get("offer") == ir.offer and x64_c.get("sku") == ir.sku) or \
                       (arm_c.get("offer") == ir.offer and arm_c.get("sku") == ir.sku):
                        os_display = preset["name"]
                        matched = True
                        break
                if not matched:
                    if ir.offer and ir.sku:
                        os_display = f"{ir.offer} ({ir.sku})"
                if ir.publisher:
                    image_ref = f"{ir.publisher}:{ir.offer}:{ir.sku}:{ir.version}"
                elif ir.id:
                    image_ref = ir.id
            elif vm.storage_profile.os_disk and vm.storage_profile.os_disk.os_type:
                raw_type = str(vm.storage_profile.os_disk.os_type.value if hasattr(vm.storage_profile.os_disk.os_type, 'value') else vm.storage_profile.os_disk.os_type)
                os_display = "Windows" if "windows" in raw_type.lower() else "Linux"

        # 动态探测 VM 操作系统类型与当前认证模式
        is_windows = "windows" in os_display.lower() or (vm.storage_profile and vm.storage_profile.os_disk and "windows" in str(vm.storage_profile.os_disk.os_type).lower())

        current_auth_mode = "password"
        if not is_windows and vm.os_profile:
            # 严格依据 Azure 官方规则判定：
            # 1. 如果 disable_password_authentication 为 True，则必定是纯 SSH 模式
            # 2. 如果 disable_password_authentication 为 False，且未配置 public_keys，或者设置了 admin_password，则为密码模式
            # 3. 检查是否有明确配置的 ssh public_keys
            if vm.os_profile.linux_configuration:
                linux_cfg = vm.os_profile.linux_configuration
                if linux_cfg.disable_password_authentication is True:
                    current_auth_mode = "ssh_key"
                elif linux_cfg.ssh and hasattr(linux_cfg.ssh, 'public_keys') and linux_cfg.ssh.public_keys and len(linux_cfg.ssh.public_keys) > 0:
                    current_auth_mode = "ssh_key"
                else:
                    current_auth_mode = "password"
            else:
                current_auth_mode = "password"

        # 动态探测 VM 现有的管理员用户名
        admin_username = "azureuser"
        if vm.os_profile and vm.os_profile.admin_username:
            admin_username = vm.os_profile.admin_username

        # 尝试拉取 VM 扩展中配置过的用户及历史凭据类型
        detected_users = [admin_username] if admin_username else ["azureuser"]
        try:
            exts = compute_client.virtual_machine_extensions.list(resource_group, vm_name)
            for ext in exts.value if hasattr(exts, 'value') else exts:
                if ext.name == "VMAccessForLinux" and not is_windows:
                    if ext.protected_settings:
                        if ext.protected_settings.get("ssh_key"):
                            current_auth_mode = "ssh_key"
                        elif ext.protected_settings.get("password"):
                            current_auth_mode = "password"
                if ext.settings and "UserName" in ext.settings:
                    u = ext.settings["UserName"]
                    if u not in detected_users:
                        detected_users.append(u)
        except Exception:
            pass

        return {
            "name": vm.name,
            "resource_group": resource_group,
            "location": vm.location,
            "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else "-",
            "os_type": "Windows" if is_windows else os_display,
            "admin_username": admin_username,
            "detected_users": detected_users,
            "auth_mode": current_auth_mode,
            "os_disk_name": vm.storage_profile.os_disk.name if (vm.storage_profile and vm.storage_profile.os_disk) else "-",
            "os_disk_size_gb": vm.storage_profile.os_disk.disk_size_gb if (vm.storage_profile and vm.storage_profile.os_disk) else "-",
            "power_state": power_state,
            "provisioning_state": vm.provisioning_state,
            "image_ref": image_ref,
            "nic_name": nic_name,
            "private_ip": private_ip,
            "public_ip": public_ip,
            "public_ip_name": public_ip_name,
            "nsg_name": nsg_name,
            "accelerated_networking": accelerated_networking,
            "billing_mode": "spot" if "spot" in str(getattr(vm, "priority", "") or "").lower() else "on_demand"
        }

    # ========================== VM 生命周期管理 ==========================

    @classmethod
    def _vm_resize_options_cache_key(cls, tenant_id: str, client_id: str,
                                     client_secret: str, subscription_id: str,
                                     resource_group: str, vm_name: str):
        return (
            tenant_id.strip(),
            client_id.strip(),
            cls.secret_fingerprint(client_secret),
            subscription_id.strip(),
            resource_group.strip(),
            vm_name.strip(),
        )

    @staticmethod
    def _vm_power_state(instance_view) -> str:
        for status in getattr(instance_view, "statuses", None) or []:
            code = getattr(status, "code", "") or ""
            if code.lower().startswith("powerstate/"):
                return getattr(status, "display_status", None) or code.split("/", 1)[1]
        return "Unknown"

    @staticmethod
    def _vm_is_running(power_state: str) -> bool:
        return power_state.strip().lower() in ("running", "vm running")

    @staticmethod
    def _vm_is_deallocated(power_state: str) -> bool:
        return power_state.strip().lower() in ("deallocated", "vm deallocated")

    @classmethod
    def _read_vm_resize_state(cls, compute_client, resource_group: str, vm_name: str):
        vm = compute_client.virtual_machines.get(resource_group, vm_name)
        instance_view = compute_client.virtual_machines.instance_view(resource_group, vm_name)
        current_size = getattr(getattr(vm, "hardware_profile", None), "vm_size", None)
        return vm, current_size, cls._vm_power_state(instance_view)

    @staticmethod
    def _resize_size_payload(size: Dict[str, Any]) -> Dict[str, Any]:
        """将统一规格模型转换成详情页兼容的字段，同时保留完整悬停详情。"""
        memory_gib = size.get("memory_gib")
        memory_in_mb = size.get("memory_in_mb")
        if memory_in_mb is None and memory_gib is not None:
            memory_in_mb = round(float(memory_gib) * 1024)
        cores = size.get("vcpus")
        return {
            **size,
            "name": size.get("name") or "",
            "number_of_cores": cores,
            "vcpus": cores,
            "vcpus_available": size.get("vcpus_available", cores),
            "memory_in_mb": memory_in_mb,
            "memory_gib": memory_gib,
            "resource_disk_size_in_mb": (
                size.get("resource_disk_size_in_mb")
                if size.get("resource_disk_size_in_mb") is not None
                else (round(float(size["temp_disk_gib"]) * 1024)
                      if size.get("temp_disk_gib") is not None else None)
            ),
            "os_disk_size_in_mb": (
                size.get("os_disk_size_in_mb")
                if size.get("os_disk_size_in_mb") is not None
                else (round(float(size["os_disk_gib"]) * 1024)
                      if size.get("os_disk_gib") is not None else None)
            ),
            "max_data_disk_count": size.get("max_data_disk_count"),
        }

    @classmethod
    def invalidate_vm_resize_options_cache(cls, tenant_id: str, client_id: str,
                                           client_secret: str, subscription_id: str,
                                           resource_group: str, vm_name: str) -> None:
        cache_key = cls._vm_resize_options_cache_key(
            tenant_id, client_id, client_secret, subscription_id, resource_group, vm_name
        )
        with cls._resize_options_cache_lock:
            cls._resize_options_cache.pop(cache_key, None)

    @classmethod
    def list_vm_resize_options(cls, tenant_id: str, client_id: str, client_secret: str,
                               subscription_id: str, resource_group: str, vm_name: str,
                               use_cache: bool = True,
                               resource_sku_provider=None) -> dict:
        cache_key = cls._vm_resize_options_cache_key(
            tenant_id, client_id, client_secret, subscription_id, resource_group, vm_name
        )
        now_ts = time.time()
        if use_cache:
            with cls._resize_options_cache_lock:
                cached = cls._resize_options_cache.get(cache_key)
                if cached and now_ts - cached[0] < VM_RESIZE_OPTIONS_CACHE_TTL_SECONDS:
                    cached_result = dict(cached[1])
                    cached_result["source"] = "cache"
                    cached_result["cached_at"] = cached[0]
                    return cached_result

        compute_client = cls.get_compute_client(
            tenant_id, client_id, client_secret, subscription_id
        )
        vm, current_size, power_state = cls._read_vm_resize_state(
            compute_client, resource_group, vm_name
        )
        available_sizes = compute_client.virtual_machines.list_available_sizes(
            resource_group, vm_name
        )

        unsupported_reason = None
        if getattr(vm, "availability_set", None):
            unsupported_reason = "可用性集中的虚拟机暂不支持调整规格"
        elif getattr(vm, "virtual_machine_scale_set", None):
            unsupported_reason = "虚拟机规模集中的实例暂不支持调整规格"
        elif getattr(vm, "host", None):
            unsupported_reason = "专用主机上的虚拟机暂不支持调整规格"
        elif not (cls._vm_is_running(power_state) or cls._vm_is_deallocated(power_state)):
            unsupported_reason = (
                f"虚拟机当前电源状态 {power_state} 不支持调整规格，"
                "仅支持 Running 或 Deallocated"
            )

        # List Available Sizes 只描述当前 VM 的候选集合；架构和限制必须以同地域
        # Resource SKUs 为准，不能通过规格名称猜测 ARM64。
        enriched_sizes = None
        resize_metadata = {}
        resource_skus = getattr(compute_client, "resource_skus", None)
        vm_location = getattr(vm, "location", None)
        if (
            (resource_sku_provider is not None or resource_skus is not None)
            and vm_location
            and current_size
        ):
            location = str(vm_location).lower().replace(" ", "")
            catalog_by_name = {}
            if resource_sku_provider is not None:
                provided_catalog = resource_sku_provider(location) or {}
                catalog_by_name = {
                    str(name).lower(): dict(item)
                    for name, item in provided_catalog.items()
                    if name and item
                }
            else:
                for sku in resource_skus.list(filter=f"location eq '{location}'"):
                    if getattr(sku, "resource_type", None) != "virtualMachines":
                        continue
                    parsed = parse_resource_sku(sku, location=location)
                    name = str(parsed.get("name") or "").lower()
                    if name:
                        catalog_by_name[name] = parsed

            current_catalog = catalog_by_name.get(str(current_size).lower())
            current_architecture = normalize_architecture(
                (current_catalog or {}).get("architecture")
            )
            if not current_architecture:
                unsupported_reason = "无法从 Azure Resource SKUs 确认当前虚拟机架构，已停止提供调整规格选项"
            else:
                current_nic_accelerated = None
                network_profile = getattr(vm, "network_profile", None)
                network_interfaces = getattr(network_profile, "network_interfaces", None) or []
                if network_interfaces:
                    try:
                        nic_id = getattr(network_interfaces[0], "id", "") or ""
                        nic_name = nic_id.split("/")[-1]
                        id_parts = [part for part in nic_id.split("/") if part]
                        nic_resource_group = resource_group
                        for index, part in enumerate(id_parts[:-1]):
                            if part.lower() == "resourcegroups":
                                nic_resource_group = id_parts[index + 1]
                                break
                        nic = cls.get_network_client(
                            tenant_id, client_id, client_secret, subscription_id
                        ).network_interfaces.get(nic_resource_group, nic_name)
                        current_nic_accelerated = getattr(
                            nic, "enable_accelerated_networking", None
                        )
                    except Exception as exc:
                        logger.warning("读取 VM 网卡网络加速状态失败：%s", exc)
                billing_raw = str(getattr(vm, "priority", "") or "").lower()
                current_billing_mode = "spot" if "spot" in billing_raw else "on_demand"
                candidates = filter_resize_candidates(
                    available_sizes,
                    current_architecture,
                    catalog_by_name,
                    current_nic_accelerated=current_nic_accelerated,
                    current_billing_mode=current_billing_mode,
                )
                enriched_sizes = [cls._resize_size_payload(item) for item in candidates]
                disk = getattr(getattr(vm, "storage_profile", None), "os_disk", None)
                raw_os = getattr(disk, "os_type", None)
                raw_os = getattr(raw_os, "value", raw_os)
                resize_metadata = {
                    "current_architecture": current_architecture,
                    "location": location,
                    "catalog_count": len(catalog_by_name),
                    "current_nic_accelerated_networking": current_nic_accelerated,
                    "current_os_type": "Windows" if "windows" in str(raw_os).lower() else "Linux",
                    "current_billing_mode": "spot" if "spot" in billing_raw else "on_demand",
                }

        result = {
            "current_size": current_size,
            "power_state": power_state,
            "supported": unsupported_reason is None,
            "unsupported_reason": unsupported_reason,
            "source": "fresh",
            "cached_at": now_ts,
            "sizes": [{
                "name": size.name,
                "number_of_cores": size.number_of_cores,
                "memory_in_mb": size.memory_in_mb,
                "resource_disk_size_in_mb": size.resource_disk_size_in_mb,
                "os_disk_size_in_mb": size.os_disk_size_in_mb,
                "max_data_disk_count": size.max_data_disk_count,
            } for size in available_sizes] if enriched_sizes is None else enriched_sizes,
        }
        result.update(resize_metadata)
        with cls._resize_options_cache_lock:
            cls._resize_options_cache[cache_key] = (now_ts, result)
        return result

    @classmethod
    def resize_vm(cls, tenant_id: str, client_id: str, client_secret: str,
                  subscription_id: str, resource_group: str, vm_name: str,
                  target_vm_size: str, progress_callback=None) -> dict:
        def update_progress(message):
            if progress_callback:
                try:
                    progress_callback(message)
                except Exception:
                    pass

        compute_client = cls.get_compute_client(
            tenant_id, client_id, client_secret, subscription_id
        )
        was_running = False
        operation_started = False
        try:
            options = cls.list_vm_resize_options(
                tenant_id, client_id, client_secret, subscription_id,
                resource_group, vm_name, use_cache=False
            )
            old_size = options["current_size"]
            was_running = cls._vm_is_running(options["power_state"])
            target_vm_size = target_vm_size.strip()

            if not options["supported"]:
                raise ValueError(options["unsupported_reason"])
            if not target_vm_size:
                raise ValueError("目标虚拟机规格不能为空")
            if old_size and target_vm_size.lower() == old_size.lower():
                raise ValueError("目标虚拟机规格与当前规格相同")

            available_sizes = {
                size["name"].lower(): size["name"] for size in options["sizes"]
            }
            canonical_target_size = available_sizes.get(target_vm_size.lower())
            if not canonical_target_size:
                raise ValueError("目标虚拟机规格不在该虚拟机的可调整规格列表中")
            target_item = next(
                (size for size in options["sizes"]
                 if str(size.get("name", "")).lower() == target_vm_size.lower()),
                None,
            )
            if target_item and target_item.get("selectable") is False:
                raise ValueError("目标规格当前受限，仅展示不可选择")

            from azure.mgmt.compute.models import HardwareProfile, VirtualMachineUpdate

            operation_started = True
            if was_running:
                update_progress("正在解除分配虚拟机...")
                wait_for_azure_poller(
                    compute_client.virtual_machines.begin_deallocate(resource_group, vm_name)
                )

            update_progress("正在更新虚拟机规格...")
            update = VirtualMachineUpdate(
                hardware_profile=HardwareProfile(vm_size=canonical_target_size)
            )
            wait_for_azure_poller(
                compute_client.virtual_machines.begin_update(resource_group, vm_name, update)
            )

            if was_running:
                update_progress("正在启动虚拟机...")
                wait_for_azure_poller(
                    compute_client.virtual_machines.begin_start(resource_group, vm_name)
                )

            _, confirmed_size, final_power_state = cls._read_vm_resize_state(
                compute_client, resource_group, vm_name
            )
            return {
                "old_size": old_size,
                "new_size": confirmed_size,
                "was_running": was_running,
                "final_power_state": final_power_state,
            }
        except Exception as error:
            confirmed_size = None
            confirmed_power_state = None
            if operation_started:
                try:
                    _, confirmed_size, confirmed_power_state = cls._read_vm_resize_state(
                        compute_client, resource_group, vm_name
                    )
                    if was_running and not cls._vm_is_running(confirmed_power_state):
                        wait_for_azure_poller(
                            compute_client.virtual_machines.begin_start(resource_group, vm_name)
                        )
                        _, confirmed_size, confirmed_power_state = cls._read_vm_resize_state(
                            compute_client, resource_group, vm_name
                        )
                except Exception as recovery_error:
                    logger.error(
                        "Failed to restore running VM %s after resize error: %s",
                        vm_name, recovery_error
                    )
            error.confirmed_size = confirmed_size
            error.confirmed_power_state = confirmed_power_state
            raise
        finally:
            cls.invalidate_vm_resize_options_cache(
                tenant_id, client_id, client_secret, subscription_id, resource_group, vm_name
            )

    @classmethod
    def start_vm(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, resource_group: str, vm_name: str):
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)
        poller = compute_client.virtual_machines.begin_start(resource_group, vm_name)
        wait_for_azure_poller(poller)

    @classmethod
    def stop_vm(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, resource_group: str, vm_name: str):
        """停止并解除分配 (释放计费)"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)
        poller = compute_client.virtual_machines.begin_deallocate(resource_group, vm_name)
        wait_for_azure_poller(poller)

    @classmethod
    def restart_vm(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, resource_group: str, vm_name: str):
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)
        poller = compute_client.virtual_machines.begin_restart(resource_group, vm_name)
        wait_for_azure_poller(poller)

    @classmethod
    def delete_vm_and_resources(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, resource_group: str, vm_name: str):
        """删除资源组（完整销毁关联的 VM/网卡/IP/磁盘）"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        resource_client = ResourceManagementClient(cred, subscription_id)
        poller = resource_client.resource_groups.begin_delete(resource_group)
        wait_for_azure_poller(poller)

    # ========================== 创建虚拟机 ==========================

    @classmethod
    def create_vm_complete(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                           resource_group: str, location: str, vm_name: str, vm_size: str,
                           image_key_or_urn: str, admin_username: str, admin_password: Optional[str] = None,
                           ssh_public_key: Optional[str] = None, disk_size_gb: int = 30,
                           custom_data: Optional[str] = None, accelerated_networking: bool = False,
                           spot_instance: bool = False, open_ports: Optional[List[int]] = None,
                           progress_callback: Optional[Any] = None,
                           image_architecture: Optional[str] = None):
        """
        完整流程创建虚拟机：RG -> VNet -> Subnet -> Public IP -> NSG -> NIC -> VM
        支持详细进度回调
        """
        def update_progress(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass

        is_arm_sku = cls.is_arm_vm_size(vm_size)
        expected_architecture = "arm64" if is_arm_sku else "x64"
        if image_architecture and image_architecture.strip().lower() != expected_architecture:
            raise Exception(f"镜像架构 {image_architecture} 与目标规格架构 {expected_architecture} 不匹配")

        cred = cls.get_credential(tenant_id, client_id, client_secret)
        resource_client = ResourceManagementClient(cred, subscription_id)
        network_client = NetworkManagementClient(cred, subscription_id)
        compute_client = ComputeManagementClient(cred, subscription_id)

        # 1. 确保资源组存在
        update_progress("1/6 正在创建资源组...")
        resource_client.resource_groups.create_or_update(resource_group, {"location": location})

        # 2. 创建 VNet 与 Subnet (使用 Azure SDK 强类型模型避免字典反序列化报错)
        update_progress("2/6 正在配置虚拟网络与子网 (10.0.0.0/16)...")
        from azure.mgmt.network.models import (
            VirtualNetwork, AddressSpace, Subnet,
            PublicIPAddress, PublicIPAddressSku,
            NetworkSecurityGroup, SecurityRule,
            NetworkInterface, NetworkInterfaceIPConfiguration
        )

        vnet_name = f"vnet-{vm_name}"
        subnet_name = f"subnet-{vm_name}"

        vnet_params = VirtualNetwork(
            location=location,
            address_space=AddressSpace(address_prefixes=["10.0.0.0/16"])
        )
        vnet_poller = network_client.virtual_networks.begin_create_or_update(
            resource_group, vnet_name, vnet_params
        )
        wait_for_azure_poller(vnet_poller)

        subnet_params = Subnet(address_prefix="10.0.0.0/24")
        subnet_poller = network_client.subnets.begin_create_or_update(
            resource_group, vnet_name, subnet_name, subnet_params
        )
        subnet = wait_for_azure_poller(subnet_poller)

        # 3. 创建 Public IP
        update_progress("3/6 正在申请独立公网 IPv4...")
        pip_name = f"pip-{vm_name}"
        pip_params = PublicIPAddress(
            location=location,
            sku=PublicIPAddressSku(name="Standard"),
            public_ip_allocation_method="Static"
        )
        pip_poller = network_client.public_ip_addresses.begin_create_or_update(
            resource_group, pip_name, pip_params
        )
        public_ip = wait_for_azure_poller(pip_poller)

        # 4. 创建 NSG (网络安全组) 并根据系统类型自适应放行端口
        # Linux: 22 (SSH), 80 (HTTP), 443 (HTTPS)
        # Windows: 3389 (RDP), 80 (HTTP), 443 (HTTPS)
        update_progress("4/6 正在创建防火墙安全组并自适应配置放行规则...")
        nsg_name = f"nsg-{vm_name}"

        # 预先判断系统类型
        is_win = False
        if image_key_or_urn in PRESET_IMAGES:
            is_win = (PRESET_IMAGES[image_key_or_urn].get("os_type") == "Windows")
        elif "windows" in image_key_or_urn.lower():
            is_win = True

        if not open_ports:
            if is_win:
                open_ports = [3389, 80, 443]  # Windows 默认放行远程桌面 RDP 与 Web 端口
            else:
                open_ports = [22, 80, 443]    # Linux 默认放行 SSH 与 Web 端口

        security_rules = []
        priority = 1000
        for port in open_ports:
            rule_title = f"Allow-{port}"
            if port == 22:
                rule_title = "Allow-SSH-22"
            elif port == 3389:
                rule_title = "Allow-RDP-3389"
            elif port == 80:
                rule_title = "Allow-HTTP-80"
            elif port == 443:
                rule_title = "Allow-HTTPS-443"

            security_rules.append(SecurityRule(
                name=rule_title,
                protocol="Tcp",
                source_port_range="*",
                destination_port_range=str(port),
                source_address_prefix="*",
                destination_address_prefix="*",
                access="Allow",
                priority=priority,
                direction="Inbound"
            ))
            priority += 10

        nsg_params = NetworkSecurityGroup(
            location=location,
            security_rules=security_rules
        )
        nsg_poller = network_client.network_security_groups.begin_create_or_update(
            resource_group, nsg_name, nsg_params
        )
        nsg = wait_for_azure_poller(nsg_poller)

        # 5. 创建 NIC (网卡)
        update_progress("5/6 正在绑定网络接口与公网 IP...")
        nic_name = f"nic-{vm_name}"
        nic_params = NetworkInterface(
            location=location,
            enable_accelerated_networking=accelerated_networking,
            network_security_group=nsg,
            ip_configurations=[NetworkInterfaceIPConfiguration(
                name=f"ipconfig-{vm_name}",
                subnet=subnet,
                public_ip_address=public_ip,
                private_ip_allocation_method="Dynamic"
            )]
        )
        nic_poller = network_client.network_interfaces.begin_create_or_update(
            resource_group, nic_name, nic_params
        )
        nic = wait_for_azure_poller(nic_poller)

        # 6. 解析 Image 镜像与 ARM 架构适配
        update_progress("6/6 正在向 Azure 下发虚拟机创建指令并部署系统...")
        from azure.mgmt.compute.models import (
            VirtualMachine, HardwareProfile, StorageProfile,
            ImageReference, OSDisk, DiskCreateOptionTypes,
            ManagedDiskParameters, StorageAccountTypes,
            OSProfile, LinuxConfiguration, SshConfiguration, SshPublicKey,
            NetworkProfile, NetworkInterfaceReference,
            BillingProfile, VirtualMachinePriorityTypes, VirtualMachineEvictionPolicyTypes
        )

        # 检测所选规格是否为 Azure ARM 架构规格 (Ampere Altra)
        is_arm_sku = cls.is_arm_vm_size(vm_size)

        os_type = "Linux"
        pub, off, sk, ver = "Canonical", "ubuntu-24_04-lts", "server", "latest"

        if image_key_or_urn in PRESET_IMAGES:
            preset = PRESET_IMAGES[image_key_or_urn]
            os_type = preset["os_type"]
            arch_key = "arm64" if is_arm_sku else "x64"

            # 严格架构支持校验 (拒绝在 ARM 上使用仅限 x64 的镜像)
            if is_arm_sku and "arm64" not in preset.get("supported_archs", []):
                raise Exception(f"系统镜像【{preset['name']}】不支持 ARM64 架构机型，请选择支持 ARM64 的 Linux 发行版！")

            img_conf = preset.get(arch_key) or preset.get("x64")
            pub = img_conf["publisher"]
            off = img_conf["offer"]
            sk = img_conf["sku"]
            ver = img_conf.get("version", "latest")
        else:
            parts = image_key_or_urn.split(":")
            if len(parts) == 4:
                pub, off, sk, ver = parts[0], parts[1], parts[2], parts[3]
                if "windows" in parts[1].lower() or "windows" in parts[2].lower():
                    os_type = "Windows"

        # OS Profile
        os_prof = OSProfile(
            computer_name=vm_name[:15] if os_type == "Windows" else vm_name,
            admin_username=admin_username
        )
        if admin_password:
            os_prof.admin_password = admin_password

        if ssh_public_key and os_type == "Linux":
            os_prof.linux_configuration = LinuxConfiguration(
                disable_password_authentication=False if admin_password else True,
                ssh=SshConfiguration(
                    public_keys=[SshPublicKey(
                        path=f"/home/{admin_username}/.ssh/authorized_keys",
                        key_data=ssh_public_key.strip()
                    )]
                )
            )

        if custom_data:
            os_prof.custom_data = base64.b64encode(custom_data.encode("utf-8")).decode("utf-8")

        # Storage Profile
        storage_prof = StorageProfile(
            image_reference=ImageReference(
                publisher=pub,
                offer=off,
                sku=sk,
                version=ver
            ),
            os_disk=OSDisk(
                name=f"osdisk-{vm_name}",
                caching="ReadWrite",
                create_option=DiskCreateOptionTypes.FROM_IMAGE,
                disk_size_gb=disk_size_gb,
                managed_disk=ManagedDiskParameters(storage_account_type=StorageAccountTypes.PREMIUM_LRS)
            )
        )

        # Network Profile
        net_prof = NetworkProfile(
            network_interfaces=[NetworkInterfaceReference(id=nic.id)]
        )

        vm_model = VirtualMachine(
            location=location,
            hardware_profile=HardwareProfile(vm_size=vm_size),
            storage_profile=storage_prof,
            os_profile=os_prof,
            network_profile=net_prof
        )

        if spot_instance:
            vm_model.priority = VirtualMachinePriorityTypes.SPOT
            vm_model.eviction_policy = VirtualMachineEvictionPolicyTypes.DEALLOCATE
            vm_model.billing_profile = BillingProfile(max_price=-1)

        vm_poller = compute_client.virtual_machines.begin_create_or_update(
            resource_group, vm_name, vm_model
        )
        res = wait_for_azure_poller(vm_poller)
        update_progress("虚拟机创建成功并已开机！")
        return res

    # ========================== 重装系统 (事务与回滚) ==========================

    @classmethod
    def reinstall_vm_os(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                        resource_group: str, vm_name: str, new_image_key_or_urn: str,
                        admin_username: str, admin_password: Optional[str] = None,
                        ssh_public_key: Optional[str] = None, disk_size_gb: int = 30,
                        custom_data: Optional[str] = None, progress_callback: Optional[Any] = None,
                        image_architecture: Optional[str] = None):
        """
        重装系统事务：
        1. 备份原 VM 参数 (location, vm_size, nic_id, old_os_disk_id, old_os_disk_name)
        2. 删除原 VM 实体 (保留原 NIC、公网 IP、原 OS 盘)
        3. 挂接原 NIC，创建新 OS 盘并创建新 VM 实体
        4. 如果成功：删除原 OS 盘释放费用
        5. 如果失败：清理新建失败的 VM 实体与新建 OS 盘，使用原 OS 盘和原 NIC 重新组装启动原 VM 进行回滚
        """
        def update_progress(message):
            if progress_callback:
                try:
                    progress_callback(message)
                except Exception:
                    pass

        def copy_original_vm_properties(source_vm, target_vm):
            for property_name in (
                "availability_set", "diagnostics_profile", "license_type", "plan",
                "zones", "proximity_placement_group", "capacity_reservation",
                "host", "host_group", "additional_capabilities", "tags",
                "identity", "user_data", "extended_location"
            ):
                property_value = getattr(source_vm, property_name, None)
                if property_value is not None:
                    setattr(target_vm, property_name, property_value)

        update_progress("1/7 正在校验虚拟机并备份原系统盘信息...")
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)

        # 1. 严格前置检查：确保 Azure 云端原 VM 真实存在且处于正常状态，并备份完整元数据
        old_vm = None
        target_rg = resource_group
        target_vm_name = vm_name

        try:
            old_vm = compute_client.virtual_machines.get(resource_group, vm_name)
        except Exception:
            pass

        if not old_vm:
            try:
                # 遍历订阅下全部 VM 做大小写对齐匹配
                for v in compute_client.virtual_machines.list_all():
                    v_rg = v.id.split("/resourceGroups/")[1].split("/")[0]
                    if v.name.strip().lower() == vm_name.strip().lower() and v_rg.strip().lower() == resource_group.strip().lower():
                        old_vm = v
                        target_rg = v_rg
                        target_vm_name = v.name
                        break
            except Exception as e_list:
                logger.warning(f"Error scanning VMs for reinstall: {e_list}")

        # 若云端不存在该虚拟机实体，严谨阻断并明确报错，绝不做矛盾的残余捏造
        if not old_vm:
            raise Exception(f"在 Azure 订阅中未检测到虚拟机【{vm_name}】实体，无法执行换盘重装操作。请返回列表页刷新确认！")

        resource_group = target_rg
        vm_name = target_vm_name

        location = old_vm.location
        vm_size = old_vm.hardware_profile.vm_size
        nic_id = old_vm.network_profile.network_interfaces[0].id
        old_os_disk_name = old_vm.storage_profile.os_disk.name if (old_vm.storage_profile and old_vm.storage_profile.os_disk) else f"osdisk-{vm_name}"
        old_os_disk_id = old_vm.storage_profile.os_disk.managed_disk.id if (old_vm.storage_profile and old_vm.storage_profile.os_disk and old_vm.storage_profile.os_disk.managed_disk) else None

        if not old_os_disk_id:
            raise Exception(f"虚拟机【{vm_name}】未检测到有效的托管系统盘 ID，无法执行安全换盘重装！")

        # 2. 前置参数验证与镜像解构 (在删除旧 VM 之前 100% 完成，校验不通过绝不碰旧 VM)
        from azure.mgmt.compute.models import (
            VirtualMachine, HardwareProfile, StorageProfile,
            ImageReference, OSDisk, DiskCreateOptionTypes,
            ManagedDiskParameters, StorageAccountTypes,
            OSProfile, LinuxConfiguration, SshConfiguration, SshPublicKey,
            NetworkProfile, NetworkInterfaceReference, SecurityProfile, SecurityTypes,
            VirtualMachinePriorityTypes, VirtualMachineEvictionPolicyTypes, BillingProfile
        )

        new_disk_name = f"osdisk-{vm_name}-{int(time.time())}"
        is_arm_sku = cls.is_arm_vm_size(vm_size)
        expected_architecture = "arm64" if is_arm_sku else "x64"
        if image_architecture and image_architecture.strip().lower() != expected_architecture:
            raise Exception(f"镜像架构 {image_architecture} 与原虚拟机架构 {expected_architecture} 不匹配")

        os_type = "Linux"
        pub, off, sk, ver = "Canonical", "ubuntu-24_04-lts", "server", "latest"

        if new_image_key_or_urn in PRESET_IMAGES:
            preset = PRESET_IMAGES[new_image_key_or_urn]
            os_type = preset["os_type"]
            arch_key = "arm64" if is_arm_sku else "x64"

            # 严格架构支持校验 (拒绝在 ARM 上使用仅限 x64 的镜像)
            if is_arm_sku and "arm64" not in preset.get("supported_archs", []):
                raise Exception(f"系统镜像【{preset['name']}】不支持 ARM64 架构机型，请选择支持 ARM64 的 Linux 发行版！")

            img_conf = preset.get(arch_key) or preset.get("x64")
            pub = img_conf["publisher"]
            off = img_conf["offer"]
            sk = img_conf["sku"]
            ver = img_conf.get("version", "latest")
        else:
            parts = new_image_key_or_urn.split(":")
            if len(parts) == 4:
                pub, off, sk, ver = parts[0], parts[1], parts[2], parts[3]
                if "windows" in parts[1].lower():
                    os_type = "Windows"

        # 架构合法性强校验（禁止不支持当前架构的镜像提交）
        if is_arm_sku:
            if "windows" in (pub + off + sk).lower():
                raise Exception("Windows 目前在 Azure 官方未提供公共通用 ARM64 镜像，无法在 ARM 实例上部署，请选择 Linux 官方发行版！")
            if "rocky" in (pub + off + sk).lower():
                raise Exception("Rocky Linux 官方未在 Azure 提供 ARM64 镜像，请选择 AlmaLinux 9 或 Debian/Ubuntu ARM64！")

        os_prof = OSProfile(
            computer_name=vm_name[:15] if os_type == "Windows" else vm_name,
            admin_username=admin_username
        )
        if admin_password:
            os_prof.admin_password = admin_password
        if ssh_public_key and os_type == "Linux":
            os_prof.linux_configuration = LinuxConfiguration(
                disable_password_authentication=False if admin_password else True,
                ssh=SshConfiguration(
                    public_keys=[SshPublicKey(
                        path=f"/home/{admin_username}/.ssh/authorized_keys",
                        key_data=ssh_public_key.strip()
                    )]
                )
            )
        if custom_data:
            os_prof.custom_data = base64.b64encode(custom_data.encode("utf-8")).decode("utf-8")

        new_vm_model = VirtualMachine(
            location=location,
            hardware_profile=HardwareProfile(vm_size=vm_size),
            storage_profile=StorageProfile(
                image_reference=ImageReference(
                    publisher=pub,
                    offer=off,
                    sku=sk,
                    version=ver
                ),
                os_disk=OSDisk(
                    name=new_disk_name,
                    caching="ReadWrite",
                    create_option=DiskCreateOptionTypes.FROM_IMAGE,
                    disk_size_gb=disk_size_gb,
                    managed_disk=ManagedDiskParameters(storage_account_type=StorageAccountTypes.PREMIUM_LRS)
                )
            ),
            os_profile=os_prof,
            network_profile=NetworkProfile(
                network_interfaces=[NetworkInterfaceReference(id=nic_id)]
            )
        )

        copy_original_vm_properties(old_vm, new_vm_model)
        # 保留原机器的 Spot 抢占式或 Generation 属性
        if hasattr(old_vm, 'priority') and old_vm.priority:
            new_vm_model.priority = old_vm.priority
            new_vm_model.eviction_policy = getattr(old_vm, 'eviction_policy', None)
            new_vm_model.billing_profile = getattr(old_vm, 'billing_profile', None)

        if hasattr(old_vm, 'security_profile') and old_vm.security_profile:
            new_vm_model.security_profile = old_vm.security_profile

        update_progress("2/7 参数校验完成，准备进行安全换盘...")
        old_vm_deleted = False

        # 3. 停机失败必须立即中止，不能在 VM 仍可能运行时删除实体
        try:
            update_progress("3/7 正在安全关闭原虚拟机...")
            deallocate_poller = compute_client.virtual_machines.begin_deallocate(resource_group, vm_name)
            wait_for_azure_poller(deallocate_poller)
        except Exception as deallocate_err:
            raise Exception(f"重装失败：原虚拟机停机失败，未执行删除操作：{deallocate_err}") from deallocate_err

        # 4. 删除旧 VM；若删除结果不确定，先查询确认，绝不盲目重建
        try:
            update_progress("4/7 正在删除旧虚拟机实体并保留网络资源...")
            del_vm_poller = compute_client.virtual_machines.begin_delete(resource_group, vm_name)
            wait_for_azure_poller(del_vm_poller)
            old_vm_deleted = True
        except Exception as delete_err:
            logger.error(f"Old VM deletion failed: {delete_err}")
            try:
                existing_vm = compute_client.virtual_machines.get(resource_group, vm_name)
            except Exception as verify_err:
                return False, f"重装失败：旧虚拟机删除状态无法确认，未执行后续重建，请人工核查：{verify_err}"
            if existing_vm:
                return False, f"重装失败：旧虚拟机删除未完成，原 VM 仍保留，未执行后续重建：{delete_err}"
            old_vm_deleted = True

        # 5. 执行创建新 VM；只有确认旧 VM 已删除后才允许进入失败回滚
        try:
            update_progress("5/7 正在创建新系统盘并部署新系统...")
            create_poller = compute_client.virtual_machines.begin_create_or_update(
                resource_group, vm_name, new_vm_model
            )
            wait_for_azure_poller(create_poller)

            update_progress("6/7 新系统部署完成，正在清理旧系统盘...")
            old_disk_delete_error = None
            if old_os_disk_name:
                try:
                    old_os_disk_delete_poller = compute_client.disks.begin_delete(resource_group, old_os_disk_name)
                    wait_for_azure_poller(old_os_disk_delete_poller)
                except Exception as disk_err:
                    old_disk_delete_error = str(disk_err)
                    logger.warning(f"Failed to delete old OS disk {old_os_disk_name}: {disk_err}")

            update_progress("7/7 重装完成，新系统已就绪！")
            if old_disk_delete_error:
                return True, f"重装成功，新系统已就绪，但旧系统盘清理失败：{old_disk_delete_error}"
            return True, "重装成功，新系统已就绪并开机！"
        except Exception as err:
            if not old_vm_deleted:
                return False, f"重装失败：旧 VM 未确认删除，未执行回滚，请人工核查：{err}"
            update_progress("重装失败，正在清理新建资源并回滚原系统盘...")
            logger.error(f"Reinstall failed, rolling back to original OS disk: {err}")

            rollback_cleanup_errors = []

            # 回滚步骤 A: 清理新 VM 实体
            try:
                wait_for_azure_poller(compute_client.virtual_machines.begin_delete(resource_group, vm_name))
            except Exception as cleanup_vm_err:
                rollback_cleanup_errors.append(f"新 VM 清理失败：{cleanup_vm_err}")

            # 回滚步骤 B: 清理新建失败的新磁盘
            try:
                wait_for_azure_poller(compute_client.disks.begin_delete(resource_group, new_disk_name))
            except Exception as cleanup_disk_err:
                rollback_cleanup_errors.append(f"新系统盘清理失败：{cleanup_disk_err}")

            update_progress("正在使用原系统盘重建并恢复虚拟机...")
            # 回滚步骤 C: 唯一使用原旧 OS 系统盘挂接并拉起原 VM (数据 100% 恢复)
            orig_os_type = "Linux"
            if old_vm and hasattr(old_vm, "storage_profile") and old_vm.storage_profile and old_vm.storage_profile.os_disk:
                if old_vm.storage_profile.os_disk.os_type:
                    orig_os_type = old_vm.storage_profile.os_disk.os_type
                elif os_type:
                    orig_os_type = os_type

            rollback_os_prof = None
            if old_vm and hasattr(old_vm, 'os_profile') and old_vm.os_profile:
                rollback_os_prof = old_vm.os_profile

            rollback_model = VirtualMachine(
                location=location,
                hardware_profile=HardwareProfile(vm_size=vm_size),
                storage_profile=StorageProfile(
                    os_disk=OSDisk(
                        name=old_os_disk_name,
                        caching="ReadWrite",
                        create_option=DiskCreateOptionTypes.ATTACH,
                        os_type=orig_os_type,
                        managed_disk=ManagedDiskParameters(id=old_os_disk_id)
                    )
                ),
                os_profile=rollback_os_prof,
                network_profile=NetworkProfile(
                    network_interfaces=[NetworkInterfaceReference(id=nic_id)]
                )
            )
            copy_original_vm_properties(old_vm, rollback_model)
            if hasattr(old_vm, 'priority') and old_vm.priority:
                rollback_model.priority = old_vm.priority
                rollback_model.eviction_policy = getattr(old_vm, 'eviction_policy', None)
                rollback_model.billing_profile = getattr(old_vm, 'billing_profile', None)

            if hasattr(old_vm, 'security_profile') and old_vm.security_profile:
                rollback_model.security_profile = old_vm.security_profile
            rollback_succeeded = False
            rollback_creation_error = None
            try:
                wait_for_azure_poller(compute_client.virtual_machines.begin_create_or_update(
                    resource_group, vm_name, rollback_model
                ))
                rollback_succeeded = True
            except Exception as rb_err:
                rollback_creation_error = str(rb_err)
                logger.critical(f"Fatal: Rollback creation failed: {rb_err}")

            if rollback_succeeded and not rollback_cleanup_errors:
                return False, f"重装失败：{err}（已自动安全回滚至原系统盘状态）"
            if rollback_succeeded:
                cleanup_detail = "；".join(rollback_cleanup_errors)
                return False, f"重装失败：原系统盘已恢复，但回滚资源清理失败：{cleanup_detail}"
            failure_detail = rollback_creation_error or "；".join(rollback_cleanup_errors)
            return False, f"重装失败：回滚失败，需人工处理：{failure_detail}"


    # ========================== 更换公网 IP ==========================

    @classmethod
    def change_public_ip(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                         resource_group: str, vm_name: str, progress_callback: Optional[Any] = None) -> str:
        """
        动态更换公网 IP：
        创建新 Public IP -> 绑定到网卡 -> 解绑并删除旧 Public IP
        """
        def update_progress(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass

        from azure.mgmt.network.models import PublicIPAddress, PublicIPAddressSku

        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = cls.get_compute_client(tenant_id, client_id, client_secret, subscription_id)
        network_client = cls.get_network_client(tenant_id, client_id, client_secret, subscription_id)

        update_progress("1/3 正在获取虚拟机网卡与当前 IP 配置...")
        vm = compute_client.virtual_machines.get(resource_group, vm_name)
        nic_id = vm.network_profile.network_interfaces[0].id
        nic_name = nic_id.split("/")[-1]
        nic = network_client.network_interfaces.get(resource_group, nic_name)

        old_pip_id = None
        ip_config = nic.ip_configurations[0]
        if ip_config.public_ip_address:
            old_pip_id = ip_config.public_ip_address.id

        # 1. 创建新 Public IP (强类型模型)
        update_progress("2/3 正在向 Azure 申请分配全新公网 IPv4...")
        new_pip_name = f"pip-{vm_name}-{int(time.time())}"
        pip_params = PublicIPAddress(
            location=vm.location,
            sku=PublicIPAddressSku(name="Standard"),
            public_ip_allocation_method="Static"
        )
        new_pip_poller = network_client.public_ip_addresses.begin_create_or_update(
            resource_group, new_pip_name, pip_params
        )
        new_pip = wait_for_azure_poller(new_pip_poller)

        # 2. 绑定新 Public IP 到网卡
        update_progress("3/3 正在将新 IP 绑定至网卡并释放旧 IP...")
        ip_config.public_ip_address = new_pip
        nic_poller = network_client.network_interfaces.begin_create_or_update(
            resource_group, nic_name, nic
        )
        wait_for_azure_poller(nic_poller)

        # 3. 异步安全释放并删除旧 Public IP
        if old_pip_id:
            old_pip_name = old_pip_id.split("/")[-1]
            try:
                network_client.public_ip_addresses.begin_delete(resource_group, old_pip_name)
            except Exception as e:
                logger.warning(f"Failed to delete old public IP {old_pip_name}: {e}")

        update_progress("公网 IP 更换成功！")
        return new_pip.ip_address or "Allocated"

    # ========================== 重置管理员凭据 ==========================

    @classmethod
    def reset_credentials(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                          resource_group: str, vm_name: str, os_type: str,
                          username: str, password: Optional[str] = None, ssh_key: Optional[str] = None):
        """
        使用 Azure VMAccess 扩展在线重置密码/SSH 密钥 (免重启)
        """
        from azure.mgmt.compute.models import VirtualMachineExtension, VirtualMachineExtensionProperties

        cred = cls.get_credential(tenant_id, client_id, client_secret)
        compute_client = ComputeManagementClient(cred, subscription_id)

        vm = compute_client.virtual_machines.get(resource_group, vm_name)
        location = vm.location

        if os_type.lower() == "windows":
            ext_name = "VMAccessAgent"
            publisher = "Microsoft.Compute"
            ext_type = "VMAccessAgent"
            version = "2.4"
            protected_settings = {
                "UserName": username,
                "Password": password
            }
            settings = {}
        else:
            ext_name = "VMAccessForLinux"
            publisher = "Microsoft.OSTCExtensions"
            ext_type = "VMAccessForLinux"
            version = "1.5"
            protected_settings = {
                "username": username,
                "password": password or "",
                "ssh_key": ssh_key or "",
                "reset_ssh": "True" if (password or ssh_key) else "False"
            }
            settings = {}

        ext_model = VirtualMachineExtension(
            location=location,
            properties=VirtualMachineExtensionProperties(
                publisher=publisher,
                type=ext_type,
                type_handler_version=version,
                auto_upgrade_minor_version=True,
                settings=settings,
                protected_settings=protected_settings
            )
        )

        poller = compute_client.virtual_machine_extensions.begin_create_or_update(
            resource_group, vm_name, ext_name, ext_model
        )
        wait_for_azure_poller(poller)

    # ========================== 防火墙 (NSG) 管理 ==========================

    @classmethod
    def get_nsg_rules(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                      resource_group: str, nsg_name: str) -> List[Dict[str, Any]]:
        """获取 NSG 规则列表"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        network_client = NetworkManagementClient(cred, subscription_id)

        nsg = network_client.network_security_groups.get(resource_group, nsg_name)
        rules = []
        if nsg.security_rules:
            for r in nsg.security_rules:
                # 友好格式化枚举对象 (如 SecurityRuleProtocol.ASTERISK -> * / Any)
                proto_str = str(r.protocol.value if hasattr(r.protocol, 'value') else r.protocol)
                if proto_str.upper() in ["*", "ASTERISK", "SECURITYRULEPROTOCOL.ASTERISK"]:
                    proto_str = "Any (*)"
                elif proto_str.startswith("SecurityRuleProtocol."):
                    proto_str = proto_str.replace("SecurityRuleProtocol.", "")

                access_str = str(r.access.value if hasattr(r.access, 'value') else r.access)
                if access_str.startswith("SecurityRuleAccess."):
                    access_str = access_str.replace("SecurityRuleAccess.", "")

                direction_str = str(r.direction.value if hasattr(r.direction, 'value') else r.direction)
                if direction_str.startswith("SecurityRuleDirection."):
                    direction_str = direction_str.replace("SecurityRuleDirection.", "")

                rules.append({
                    "name": r.name,
                    "priority": r.priority,
                    "direction": direction_str,
                    "access": access_str,
                    "protocol": proto_str,
                    "source_port_range": r.source_port_range or "*",
                    "destination_port_range": r.destination_port_range or "*",
                    "source_address_prefix": r.source_address_prefix or "*",
                    "destination_address_prefix": r.destination_address_prefix or "*"
                })
        rules.sort(key=lambda x: x["priority"])
        return rules

    @classmethod
    def add_nsg_rule(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                     resource_group: str, nsg_name: str, rule_name: str, priority: int,
                     protocol: str, port_range: str, source_cidr: str = "*",
                     access: str = "Allow", direction: str = "Inbound"):
        """添加或更新 NSG 规则"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        network_client = NetworkManagementClient(cred, subscription_id)

        # 最新 Azure REST API SecurityRule 要求属性包装在 properties 内或驼峰/蛇形匹配
        rule_params = {
            "protocol": protocol,
            "source_port_range": "*",
            "destination_port_range": port_range,
            "source_address_prefix": source_cidr,
            "destination_address_prefix": "*",
            "access": access,
            "priority": priority,
            "direction": direction
        }

        # 针对最新 Azure Network SDK 自动构造 model 实体或 properties 包装
        try:
            from azure.mgmt.network.models import SecurityRule
            rule_obj = SecurityRule(
                name=rule_name,
                protocol=protocol,
                source_port_range="*",
                destination_port_range=port_range,
                source_address_prefix=source_cidr,
                destination_address_prefix="*",
                access=access,
                priority=priority,
                direction=direction
            )
            poller = network_client.security_rules.begin_create_or_update(
                resource_group, nsg_name, rule_name, rule_obj
            )
        except Exception:
            # 兼容 fallback 字典包装
            wrapped_params = {
                "name": rule_name,
                "properties": rule_params
            }
            poller = network_client.security_rules.begin_create_or_update(
                resource_group, nsg_name, rule_name, wrapped_params
            )
        wait_for_azure_poller(poller)

    @classmethod
    def delete_nsg_rule(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                        resource_group: str, nsg_name: str, rule_name: str):
        """删除 NSG 规则"""
        cred = cls.get_credential(tenant_id, client_id, client_secret)
        network_client = NetworkManagementClient(cred, subscription_id)
        poller = network_client.security_rules.begin_delete(resource_group, nsg_name, rule_name)
        wait_for_azure_poller(poller)

    # ========================== 监控图表指标拉取 ==========================

    # 监控指标缓存（5 分钟过期，防止高频轮询耗尽 Azure Monitor 配额及打满 2C1G 主机）
    _metrics_cache = {}

    @classmethod
    def get_vm_metrics(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str,
                       resource_group: str, vm_name: str, hours: int = 1) -> Dict[str, Any]:
        """
        拉取 Azure Monitor 指标 (单次高效聚合请求)
        """
        cache_key = f"metrics_{subscription_id}_{resource_group}_{vm_name}_{hours}"
        now_ts = time.time()
        if cache_key in cls._metrics_cache:
            cache_time, cached_data = cls._metrics_cache[cache_key]
            if now_ts - cache_time < 300:
                return cached_data

        cred = cls.get_credential(tenant_id, client_id, client_secret)
        monitor_client = cls.get_monitor_client(tenant_id, client_id, client_secret, subscription_id)

        resource_id = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{vm_name}"

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)
        timespan = f"{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"

        interval = "PT5M" if hours <= 1 else ("PT30M" if hours <= 6 else "PT1H")
        interval_secs = 300 if hours <= 1 else (1800 if hours <= 6 else 3600)

        # 1. 查询基础宿主机平台指标
        metric_names = "Percentage CPU,Network In Total,Network Out Total,Disk Read Bytes,Disk Write Bytes"

        time_map = {}
        total_net_in = 0.0
        total_net_out = 0.0

        tz_offset = 8
        try:
            from models import SystemSetting
            tz_name = SystemSetting.get("timezone", "Asia/Shanghai")
            tz_offsets = {"Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Taipei": 8, "Asia/Singapore": 8, "Asia/Tokyo": 9, "Asia/Seoul": 9, "UTC": 0, "Europe/London": 0, "Europe/Paris": 1, "Europe/Berlin": 1, "America/New_York": -5, "America/Los_Angeles": -8}
            tz_offset = tz_offsets.get(tz_name, 8)
        except Exception:
            pass

        try:
            metrics_data = monitor_client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval=interval,
                metricnames=metric_names,
                aggregation="Average,Total"
            )
            if metrics_data and hasattr(metrics_data, 'value') and metrics_data.value:
                for item in metrics_data.value:
                    cur_name = item.name.value if (item.name and hasattr(item.name, 'value')) else ""
                    for ts in getattr(item, 'timeseries', []) or []:
                        for dp in getattr(ts, 'data', []) or []:
                            local_dt = dp.time_stamp + timedelta(hours=tz_offset)
                            t_str = local_dt.strftime("%H:%M")
                            if t_str not in time_map:
                                time_map[t_str] = {
                                    "cpu": 0.0,
                                    "mem_gb": 0.0,
                                    "net_in_rate": 0.0,
                                    "net_out_rate": 0.0,
                                    "disk_read_rate": 0.0,
                                    "disk_write_rate": 0.0
                                }
                            avg_val = dp.average if dp.average is not None else 0.0
                            tot_val = dp.total if dp.total is not None else 0.0

                            if cur_name == "Percentage CPU":
                                time_map[t_str]["cpu"] = round(avg_val, 2)
                            elif cur_name == "Network In Total":
                                time_map[t_str]["net_in_rate"] = round(tot_val / interval_secs / 1024, 2)
                                total_net_in += tot_val
                            elif cur_name == "Network Out Total":
                                time_map[t_str]["net_out_rate"] = round(tot_val / interval_secs / 1024, 2)
                                total_net_out += tot_val
                            elif cur_name == "Disk Read Bytes":
                                time_map[t_str]["disk_read_rate"] = round(tot_val / interval_secs / 1024, 2)
                            elif cur_name == "Disk Write Bytes":
                                time_map[t_str]["disk_write_rate"] = round(tot_val / interval_secs / 1024, 2)
        except Exception as e:
            logger.warning(f"Error fetching metrics: {e}")

        # 2. 独立安全查询内存指标（如果 VM 未安装 Guest Agent，不会导致整个请求失败）
        try:
            mem_data = monitor_client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval=interval,
                metricnames="Available Memory Bytes",
                aggregation="Average"
            )
            if mem_data and hasattr(mem_data, 'value') and mem_data.value:
                for item in mem_data.value:
                    for ts in getattr(item, 'timeseries', []) or []:
                        for dp in getattr(ts, 'data', []) or []:
                            local_dt = dp.time_stamp + timedelta(hours=tz_offset)
                            t_str = local_dt.strftime("%H:%M")
                            if t_str in time_map and dp.average is not None:
                                time_map[t_str]["mem_gb"] = round(dp.average / (1024 ** 3), 2)
        except Exception as e:
            pass

        # 如果当前无历史数据点，自动填充当前时间点防止前端图表空白
        if not time_map:
            now_label = (datetime.utcnow() + timedelta(hours=tz_offset)).strftime("%H:%M")
            time_map[now_label] = {
                "cpu": 0.0,
                "mem_gb": 0.0,
                "net_in_rate": 0.0,
                "net_out_rate": 0.0,
                "disk_read_rate": 0.0,
                "disk_write_rate": 0.0
            }

        # 2. 按时间升序排序对齐
        sorted_keys = sorted(time_map.keys())
        timestamps = []
        cpu_values = []
        mem_available_gb = []
        net_in_rate = []
        net_out_rate = []
        disk_read_rate = []
        disk_write_rate = []

        for k in sorted_keys:
            timestamps.append(k)
            cpu_values.append(time_map[k]["cpu"])
            mem_available_gb.append(time_map[k]["mem_gb"])
            net_in_rate.append(time_map[k]["net_in_rate"])
            net_out_rate.append(time_map[k]["net_out_rate"])
            disk_read_rate.append(time_map[k]["disk_read_rate"])
            disk_write_rate.append(time_map[k]["disk_write_rate"])

        result = {
            "timestamps": timestamps,
            "cpu": cpu_values,
            "memory_available_gb": mem_available_gb,
            "network_in_rate_kb": net_in_rate,
            "network_out_rate_kb": net_out_rate,
            "disk_read_rate_kb": disk_read_rate,
            "disk_write_rate_kb": disk_write_rate,
            "total_network_in_mb": round(total_net_in / (1024 ** 2), 2),
            "total_network_out_mb": round(total_net_out / (1024 ** 2), 2)
        }
        cls._metrics_cache[cache_key] = (now_ts, result)
        return result

    # ========================== 费用与开销概览 ==========================

    # 费用账单缓存（10 分钟过期，避免重复扫描拖垮 2C1G 主机）
    _costs_cache = {}

    @classmethod
    def get_subscription_costs(cls, tenant_id: str, client_id: str, client_secret: str, subscription_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        获取本月订阅累计实际开销（优先使用 Azure Cost Management API，降级使用 Consumption API）
        """
        cache_key = f"costs_{subscription_id}"
        now_ts = time.time()
        if not force_refresh and cache_key in cls._costs_cache:
            cache_time, cached_data = cls._costs_cache[cache_key]
            if now_ts - cache_time < 600:  # 10分钟内直接读缓存
                return cached_data

        cred = cls.get_credential(tenant_id, client_id, client_secret)
        scope = f"/subscriptions/{subscription_id}"

        result = {
            "total_cost": 0.0,
            "currency": "USD",
            "status": "Success",
            "message": ""
        }

        # 方案 1: 优先使用官方推荐的 Cost Management Query API (获取当月从月初至今的真实精确计费总额)
        try:
            cost_client = CostManagementClient(cred)
            query = QueryDefinition(
                type="ActualCost",
                timeframe=TimeframeType.MONTH_TO_DATE,
                dataset=QueryDataset(
                    aggregation={
                        "totalCost": QueryAggregation(name="Cost", function="Sum")
                    }
                )
            )
            query_result = cost_client.query.usage(scope=scope, parameters=query)
            logger.info(f"CostManagement Query result for {subscription_id}: rows={query_result.rows}, cols={[getattr(c, 'name', '') for c in (query_result.columns or [])]}")
            if query_result and query_result.rows is not None:
                cost_idx = 0
                curr_idx = 1
                if query_result.columns:
                    for i, col in enumerate(query_result.columns):
                        col_name = (getattr(col, "name", "") or "").lower()
                        if col_name in ["cost", "totalcost", "pretaxcost"]:
                            cost_idx = i
                        elif col_name == "currency":
                            curr_idx = i

                total = 0.0
                curr = "USD"
                for row in query_result.rows:
                    if len(row) > cost_idx and row[cost_idx] is not None:
                        total += float(row[cost_idx])
                    if len(row) > curr_idx and row[curr_idx]:
                        curr = str(row[curr_idx])

                result["total_cost"] = round(total, 2)
                result["currency"] = curr
                result["status"] = "Success"
                cls._costs_cache[cache_key] = (now_ts, result)
                return result
        except Exception as e_cost:
            logger.warning(f"CostManagement Query failed for sub {subscription_id}: {e_cost}, falling back to Consumption API")

        # 方案 2: 降级使用 Consumption Management API (全量精确累计当月所有实际消费条目)
        try:
            consumption_client = ConsumptionManagementClient(cred, subscription_id)
            usage_list = consumption_client.usage_details.list(
                scope=scope
            )
            total = 0.0
            currency = "USD"
            count = 0
            for item in usage_list:
                # 获取账单条目金额 (优先使用账单结算币种金额)
                cost_val = getattr(item, 'cost_in_billing_currency', None)
                if cost_val is None:
                    cost_val = getattr(item, 'pretax_cost', None)
                if cost_val is None:
                    cost_val = getattr(item, 'cost', None)
                if cost_val is None and hasattr(item, 'properties') and item.properties:
                    props = item.properties
                    cost_val = getattr(props, 'cost_in_billing_currency', getattr(props, 'pretax_cost', getattr(props, 'cost', None)))

                if cost_val is not None:
                    try:
                        total += float(cost_val)
                    except (ValueError, TypeError):
                        pass
                curr_val = getattr(item, 'billing_currency', getattr(item, 'currency', None))
                if not curr_val and hasattr(item, 'properties') and item.properties:
                    curr_val = getattr(item.properties, 'billing_currency', getattr(item.properties, 'currency', None))
                if curr_val:
                    currency = str(curr_val)
                count += 1

            logger.info(f"Consumption calculated: total={total}, count={count}, currency={currency}")
            result["total_cost"] = round(total, 2)
            result["currency"] = currency
            result["status"] = "Success"
            cls._costs_cache[cache_key] = (now_ts, result)
            return result
        except Exception as e_cons:
            logger.error(f"Both CostManagement and Consumption failed for sub {subscription_id}: {e_cons}")
            result["status"] = "Warning"
            result["message"] = "未开通账单读取权限或当前订阅无计费数据，请确认 Cost Management / Billing Reader 权限。"
            cls._costs_cache[cache_key] = (now_ts, result)
            return result
