# coding: utf-8
"""动态发现并筛选 Azure 官方主流发行版与 Windows 镜像。"""

import concurrent.futures
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("azure_service")

MAX_UBUNTU_LTS = 3
MAX_DEBIAN_VERSIONS = 3
MAX_RHEL_CLONE_VERSIONS = 2
MAX_WINDOWS_SERVER_VERSIONS = 3

_UBUNTU_CODENAME_VERSIONS: Dict[str, Tuple[int, int]] = {
    "focal": (20, 4),
    "jammy": (22, 4),
    "noble": (24, 4),
}


def _find_exact_ci(skus: List[str], candidates: List[str]) -> Optional[str]:
    """按候选列表顺序在真实 SKU 列表中不区分大小写匹配并返回原始 SKU 名称。"""
    lower_map = {s.lower(): s for s in skus}
    for cand in candidates:
        hit = lower_map.get(cand.lower())
        if hit:
            return hit
    return None


def _pick_ubuntu_images(
    offers: List[str],
    fetch_skus,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """动态发现 Canonical Ubuntu Server LTS 镜像（按版本号倒序取最近主流版本）。"""
    pub = "Canonical"
    candidates: List[Tuple[Tuple[int, int], str]] = []
    seen_versions = set()

    for offer in offers:
        low = offer.lower()
        m_new = re.fullmatch(r"ubuntu-(\d+)_(\d+)-lts", low)
        if m_new:
            ver = (int(m_new.group(1)), int(m_new.group(2)))
            if ver not in seen_versions:
                seen_versions.add(ver)
                candidates.append((ver, offer))
            continue

        m_old = re.fullmatch(r"0001-com-ubuntu-server-([a-z]+)", low)
        if m_old:
            ver = _UBUNTU_CODENAME_VERSIONS.get(m_old.group(1))
            if ver and ver not in seen_versions:
                seen_versions.add(ver)
                candidates.append((ver, offer))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[:MAX_UBUNTU_LTS]

    x64_images: List[Dict[str, str]] = []
    arm64_images: List[Dict[str, str]] = []

    for (maj, min_v), offer in selected:
        skus = fetch_skus(pub, offer)
        if not skus:
            continue

        maj_s = f"{maj:02d}"
        min_s = f"{min_v:02d}"
        key = f"Ubuntu_{maj_s}_{min_s}_LTS"
        name = f"Ubuntu Server {maj_s}.{min_s} LTS"

        x64_sku = _find_exact_ci(
            skus,
            [
                "server",
                f"{maj_s}_{min_s}-lts-gen2",
                f"{maj_s}_{min_s}-lts",
            ],
        )
        if not x64_sku:
            fallback_x64 = [
                s for s in skus
                if "lts" in s.lower()
                and not any(bad in s.lower() for bad in ("arm64", "minimal", "fips", "cvm", "pro"))
            ]
            x64_sku = sorted(fallback_x64, key=len)[0] if fallback_x64 else None

        arm_sku = _find_exact_ci(
            skus,
            [
                "server-arm64",
                f"{maj_s}_{min_s}-lts-arm64",
            ],
        )
        if not arm_sku:
            fallback_arm = [
                s for s in skus
                if "arm64" in s.lower()
                and not any(bad in s.lower() for bad in ("minimal", "fips", "cvm", "pro"))
            ]
            arm_sku = sorted(fallback_arm, key=len)[0] if fallback_arm else None

        if x64_sku:
            x64_images.append({
                "key": key,
                "name": name,
                "os_type": "Linux",
                "urn": f"{pub}:{offer}:{x64_sku}:latest",
            })
        if arm_sku:
            arm64_images.append({
                "key": key,
                "name": name,
                "os_type": "Linux",
                "urn": f"{pub}:{offer}:{arm_sku}:latest",
            })

    return x64_images, arm64_images


def _pick_debian_images(
    offers: List[str],
    fetch_skus,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """动态发现 Debian 正式主版本镜像。"""
    pub = "Debian"
    candidates: List[Tuple[int, str]] = []
    for offer in offers:
        m = re.fullmatch(r"debian-(\d+)", offer.lower())
        if m:
            candidates.append((int(m.group(1)), offer))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[:MAX_DEBIAN_VERSIONS]

    x64_images: List[Dict[str, str]] = []
    arm64_images: List[Dict[str, str]] = []

    for ver, offer in selected:
        skus = fetch_skus(pub, offer)
        if not skus:
            continue

        name = f"Debian {ver}"
        key = f"Debian_{ver}"

        x64_sku = _find_exact_ci(skus, [f"{ver}-gen2", f"{ver}"])
        arm_sku = _find_exact_ci(skus, [f"{ver}-arm64", f"{ver}-backports-arm64-v2"])
        if not arm_sku:
            fallback_arm = [
                s for s in skus
                if "arm64" in s.lower() and "minimal" not in s.lower()
            ]
            if fallback_arm:
                fallback_arm.sort(key=lambda s: ("backports" in s.lower(), len(s)))
                arm_sku = fallback_arm[0]

        if x64_sku:
            x64_images.append({
                "key": key,
                "name": name,
                "os_type": "Linux",
                "urn": f"{pub}:{offer}:{x64_sku}:latest",
            })
        if arm_sku:
            arm64_images.append({
                "key": key,
                "name": name,
                "os_type": "Linux",
                "urn": f"{pub}:{offer}:{arm_sku}:latest",
            })

    return x64_images, arm64_images


def _pick_almalinux_images(
    offers: List[str],
    fetch_skus,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """动态发现 AlmaLinux 所有主流大版本。"""
    pub = "almalinux"
    offer_map = {o.lower(): o for o in offers}
    x64_images: List[Dict[str, str]] = []
    arm64_images: List[Dict[str, str]] = []

    x64_offer = offer_map.get("almalinux-x86_64")
    if x64_offer:
        skus = fetch_skus(pub, x64_offer)
        by_ver: Dict[int, List[str]] = {}
        for sku in skus:
            m = re.fullmatch(r"(\d+)-(gen2|gen1)", sku.lower())
            if m:
                by_ver.setdefault(int(m.group(1)), []).append(sku)
        for ver in sorted(by_ver.keys(), reverse=True)[:MAX_RHEL_CLONE_VERSIONS]:
            picked = _find_exact_ci(by_ver[ver], [f"{ver}-gen2", f"{ver}-gen1"])
            if picked:
                x64_images.append({
                    "key": f"AlmaLinux_{ver}",
                    "name": f"AlmaLinux {ver}",
                    "os_type": "Linux",
                    "urn": f"{pub}:{x64_offer}:{picked}:latest",
                })

    arm_offer = offer_map.get("almalinux-arm")
    if arm_offer:
        skus = fetch_skus(pub, arm_offer)
        by_ver_arm: Dict[int, List[str]] = {}
        for sku in skus:
            m = re.fullmatch(r"(\d+)-arm(?:-gen2)?", sku.lower())
            if m:
                by_ver_arm.setdefault(int(m.group(1)), []).append(sku)
        for ver in sorted(by_ver_arm.keys(), reverse=True)[:MAX_RHEL_CLONE_VERSIONS]:
            picked = _find_exact_ci(by_ver_arm[ver], [f"{ver}-arm-gen2", f"{ver}-arm"])
            if picked:
                arm64_images.append({
                    "key": f"AlmaLinux_{ver}",
                    "name": f"AlmaLinux {ver}",
                    "os_type": "Linux",
                    "urn": f"{pub}:{arm_offer}:{picked}:latest",
                })

    return x64_images, arm64_images


def _pick_rockylinux_images(
    offers: List[str],
    fetch_skus,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """动态发现 Rocky Linux 所有主流大版本。"""
    pub = "resf"
    offer_map = {o.lower(): o for o in offers}
    x64_images: List[Dict[str, str]] = []
    arm64_images: List[Dict[str, str]] = []

    x64_offer = offer_map.get("rockylinux-x86_64")
    if x64_offer:
        skus = fetch_skus(pub, x64_offer)
        by_ver: Dict[int, List[str]] = {}
        for sku in skus:
            m = re.fullmatch(r"(\d+)-(base|gen2|gen1)", sku.lower())
            if m:
                by_ver.setdefault(int(m.group(1)), []).append(sku)
        for ver in sorted(by_ver.keys(), reverse=True)[:MAX_RHEL_CLONE_VERSIONS]:
            picked = _find_exact_ci(by_ver[ver], [f"{ver}-base", f"{ver}-gen2", f"{ver}-gen1"])
            if picked:
                x64_images.append({
                    "key": f"RockyLinux_{ver}",
                    "name": f"Rocky Linux {ver}",
                    "os_type": "Linux",
                    "urn": f"{pub}:{x64_offer}:{picked}:latest",
                })

    arm_offer = offer_map.get("rockylinux-aarch64") or offer_map.get("rockylinux-arm")
    if arm_offer:
        skus = fetch_skus(pub, arm_offer)
        by_ver_arm: Dict[int, List[str]] = {}
        for sku in skus:
            low = sku.lower()
            m = re.fullmatch(r"rockylinux-aarch64-(\d+)(?:-base)?", low) or re.fullmatch(r"(\d+)-(base|arm(?:-gen2)?|aarch64)", low)
            if m:
                by_ver_arm.setdefault(int(m.group(1)), []).append(sku)
        for ver in sorted(by_ver_arm.keys(), reverse=True)[:MAX_RHEL_CLONE_VERSIONS]:
            picked = _find_exact_ci(
                by_ver_arm[ver],
                [
                    f"rockylinux-aarch64-{ver}",
                    f"rockylinux-aarch64-{ver}-base",
                    f"{ver}-base",
                    f"{ver}-arm-gen2",
                    f"{ver}-arm",
                    f"{ver}-aarch64",
                ],
            )
            if picked:
                arm64_images.append({
                    "key": f"RockyLinux_{ver}",
                    "name": f"Rocky Linux {ver}",
                    "os_type": "Linux",
                    "urn": f"{pub}:{arm_offer}:{picked}:latest",
                })

    return x64_images, arm64_images


def _pick_windows_desktop_image(
    win_major: int,
    offer: str,
    skus: List[str],
) -> Optional[Dict[str, str]]:
    """从 Windows 10/11 offer 中动态筛选最新标准 Pro 版本。"""
    pub = "MicrosoftWindowsDesktop"
    pattern = re.compile(rf"^win{win_major}-(\d+)h(\d+)-pro(?:-g2)?$", re.IGNORECASE)
    matches: List[Tuple[Tuple[int, int, int], str, str]] = []

    for sku in skus:
        m = pattern.fullmatch(sku.strip())
        if not m:
            continue
        ver_major = int(m.group(1))
        ver_half = int(m.group(2))
        is_g2 = 1 if sku.lower().endswith("-g2") else 0
        ver_label = f"{m.group(1)}H{m.group(2)}"
        matches.append(((ver_major, ver_half, is_g2), ver_label, sku))

    if not matches:
        return None

    matches.sort(key=lambda item: item[0], reverse=True)
    _, ver_label, best_sku = matches[0]
    return {
        "key": f"Windows_{win_major}_Pro",
        "name": f"Windows {win_major} Pro ({ver_label})",
        "os_type": "Windows",
        "urn": f"{pub}:{offer}:{best_sku}:latest",
    }


def _pick_windows_server_images(skus: List[str]) -> List[Dict[str, str]]:
    """从 WindowsServer offer 中动态发现所有年份 Datacenter 版本并按优先级选真实 SKU。"""
    pub = "MicrosoftWindowsServer"
    off = "WindowsServer"
    excluded_tokens = ("core", "container", "hotpatch", "zhcn", "chs", "ja", "ko")

    by_year: Dict[int, List[str]] = {}
    for sku in skus:
        low = sku.lower()
        m = re.match(r"^(\d{4})-datacenter\b", low)
        if not m:
            continue
        if any(tok in low for tok in excluded_tokens):
            continue
        by_year.setdefault(int(m.group(1)), []).append(sku)

    images: List[Dict[str, str]] = []
    for year in sorted(by_year.keys(), reverse=True)[:MAX_WINDOWS_SERVER_VERSIONS]:
        year_skus = by_year[year]
        picked = _find_exact_ci(
            year_skus,
            [
                f"{year}-datacenter-azure-edition-smalldisk",
                f"{year}-datacenter-azure-edition",
                f"{year}-datacenter-smalldisk-g2",
                f"{year}-datacenter-smalldisk",
                f"{year}-datacenter-g2",
                f"{year}-datacenter",
            ],
        )
        if not picked:
            picked = sorted(year_skus, key=len)[0]

        images.append({
            "key": f"Windows_Server_{year}",
            "name": f"Windows Server {year} Datacenter",
            "os_type": "Windows",
            "urn": f"{pub}:{off}:{picked}:latest",
        })

    return images


def discover_dynamic_images(compute_client: Any, location: str) -> Dict[str, Any]:
    """
    从 Azure Compute API 动态发现固定发行版品牌下当前所有活跃主版本镜像（不写死版本号）。
    """
    loc = location.lower().replace(" ", "")
    now_ts = time.time()

    def safe_list_offers(publisher: str) -> List[str]:
        try:
            return [o.name for o in compute_client.virtual_machine_images.list_offers(loc, publisher) if getattr(o, "name", None)]
        except Exception as exc:
            logger.warning("List offers error for %s in %s: %s", publisher, loc, exc)
            return []

    def safe_list_skus(publisher: str, offer: str) -> List[str]:
        try:
            return [s.name for s in compute_client.virtual_machine_images.list_skus(loc, publisher, offer) if getattr(s, "name", None)]
        except Exception as exc:
            logger.warning("List skus error for %s:%s in %s: %s", publisher, offer, loc, exc)
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        fut_canonical = executor.submit(safe_list_offers, "Canonical")
        fut_debian = executor.submit(safe_list_offers, "Debian")
        fut_alma = executor.submit(safe_list_offers, "almalinux")
        fut_rocky = executor.submit(safe_list_offers, "resf")
        fut_win11 = executor.submit(safe_list_skus, "MicrosoftWindowsDesktop", "windows-11")
        fut_win10 = executor.submit(safe_list_skus, "MicrosoftWindowsDesktop", "windows-10")
        fut_winsrv = executor.submit(safe_list_skus, "MicrosoftWindowsServer", "WindowsServer")

        canonical_offers = fut_canonical.result()
        debian_offers = fut_debian.result()
        alma_offers = fut_alma.result()
        rocky_offers = fut_rocky.result()
        win11_skus = fut_win11.result()
        win10_skus = fut_win10.result()
        winsrv_skus = fut_winsrv.result()

    # 并发获取各 Linux 发行版候选 offer 下的 SKU 列表
    sku_cache: Dict[Tuple[str, str], List[str]] = {}
    linux_tasks: List[Tuple[str, str]] = []

    for offer in canonical_offers:
        low = offer.lower()
        if re.fullmatch(r"ubuntu-\d+_\d+-lts", low) or re.fullmatch(r"0001-com-ubuntu-server-[a-z]+", low):
            linux_tasks.append(("Canonical", offer))
    for offer in debian_offers:
        if re.fullmatch(r"debian-\d+", offer.lower()):
            linux_tasks.append(("Debian", offer))
    for offer in alma_offers:
        if offer.lower() in ("almalinux-x86_64", "almalinux-arm"):
            linux_tasks.append(("almalinux", offer))
    for offer in rocky_offers:
        if offer.lower() in ("rockylinux-x86_64", "rockylinux-aarch64", "rockylinux-arm"):
            linux_tasks.append(("resf", offer))

    if linux_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(safe_list_skus, pub, off): (pub, off)
                for pub, off in linux_tasks
            }
            for fut in concurrent.futures.as_completed(future_map):
                pub_off = future_map[fut]
                sku_cache[pub_off] = fut.result()

    def get_skus(pub: str, off: str) -> List[str]:
        if (pub, off) not in sku_cache:
            sku_cache[(pub, off)] = safe_list_skus(pub, off)
        return sku_cache[(pub, off)]

    ubuntu_x64, ubuntu_arm = _pick_ubuntu_images(canonical_offers, get_skus)
    debian_x64, debian_arm = _pick_debian_images(debian_offers, get_skus)
    alma_x64, alma_arm = _pick_almalinux_images(alma_offers, get_skus)
    rocky_x64, rocky_arm = _pick_rockylinux_images(rocky_offers, get_skus)

    x64_images: List[Dict[str, str]] = []
    arm64_images: List[Dict[str, str]] = []

    x64_images.extend(ubuntu_x64)
    x64_images.extend(debian_x64)
    x64_images.extend(alma_x64)
    x64_images.extend(rocky_x64)

    arm64_images.extend(ubuntu_arm)
    arm64_images.extend(debian_arm)
    arm64_images.extend(alma_arm)
    arm64_images.extend(rocky_arm)

    win11_img = _pick_windows_desktop_image(11, "windows-11", win11_skus)
    if win11_img:
        x64_images.append(win11_img)
    win10_img = _pick_windows_desktop_image(10, "windows-10", win10_skus)
    if win10_img:
        x64_images.append(win10_img)

    x64_images.extend(_pick_windows_server_images(winsrv_skus))

    return {
        "location": loc,
        "x64": x64_images,
        "arm64": arm64_images,
        "updated_at": now_ts,
    }


def format_image_display_name(
    publisher: Optional[str],
    offer: Optional[str],
    sku: Optional[str],
) -> Optional[str]:
    """根据 Azure image_reference 的 publisher/offer/sku 格式化统一的展示名称。"""
    pub_l = (publisher or "").strip().lower()
    off_l = (offer or "").strip().lower()
    sku_l = (sku or "").strip().lower()

    if pub_l == "canonical":
        m_new = re.fullmatch(r"ubuntu-(\d+)_(\d+)-lts", off_l)
        if m_new:
            return f"Ubuntu Server {int(m_new.group(1)):02d}.{int(m_new.group(2)):02d} LTS"
        m_old = re.fullmatch(r"0001-com-ubuntu-server-([a-z]+)", off_l)
        if m_old:
            ver = _UBUNTU_CODENAME_VERSIONS.get(m_old.group(1))
            if ver:
                return f"Ubuntu Server {ver[0]:02d}.{ver[1]:02d} LTS"

    elif pub_l == "debian":
        m = re.fullmatch(r"debian-(\d+)", off_l)
        if m:
            return f"Debian {int(m.group(1))}"

    elif pub_l == "almalinux":
        m = re.fullmatch(r"(\d+)-(?:gen[12]|arm(?:-gen2)?)", sku_l)
        if m:
            return f"AlmaLinux {int(m.group(1))}"

    elif pub_l == "resf":
        m = re.fullmatch(r"rockylinux-aarch64-(\d+)(?:-base)?", sku_l) or re.fullmatch(
            r"(\d+)-(?:base|gen[12]|arm(?:-gen2)?|aarch64)", sku_l
        )
        if m:
            return f"Rocky Linux {int(m.group(1))}"

    elif pub_l == "microsoftwindowsdesktop":
        m = re.fullmatch(r"win(10|11)-(\d+h\d+)-pro(?:-g2)?", sku_l)
        if m:
            return f"Windows {m.group(1)} Pro ({m.group(2).upper()})"

    elif pub_l == "microsoftwindowsserver" and off_l == "windowsserver":
        m = re.match(r"^(\d{4})-datacenter\b", sku_l)
        if m:
            return f"Windows Server {m.group(1)} Datacenter"

    return None
