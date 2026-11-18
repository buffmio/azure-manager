# coding: utf-8
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from image_catalog import discover_dynamic_images, format_image_display_name


class _FakeVMImagesOps:
    def __init__(self, offers_by_pub, skus_by_pub_off, failing_calls=None):
        self.offers_by_pub = offers_by_pub
        self.skus_by_pub_off = skus_by_pub_off
        self.failing_calls = failing_calls or set()

    def list_offers(self, location, publisher):
        if ("offers", publisher) in self.failing_calls:
            raise RuntimeError(f"Simulated error for {publisher}")
        return [SimpleNamespace(name=name) for name in self.offers_by_pub.get(publisher, [])]

    def list_skus(self, location, publisher, offer):
        if ("skus", publisher, offer) in self.failing_calls:
            raise RuntimeError(f"Simulated error for {publisher}:{offer}")
        return [SimpleNamespace(name=name) for name in self.skus_by_pub_off.get((publisher, offer), [])]


class _FakeComputeClient:
    def __init__(self, offers_by_pub, skus_by_pub_off, failing_calls=None):
        self.virtual_machine_images = _FakeVMImagesOps(
            offers_by_pub, skus_by_pub_off, failing_calls
        )


class TestImageCatalog(unittest.TestCase):
    def test_discovers_current_and_future_versions_dynamically(self):
        offers = {
            "Canonical": [
                "0001-com-ubuntu-server-focal",
                "0001-com-ubuntu-server-jammy",
                "ubuntu-24_04-lts",
                "ubuntu-26_04-lts",
                "ubuntu-24_04-lts-daily",
            ],
            "Debian": [
                "debian-10",
                "debian-11",
                "debian-12",
                "debian-13",
                "debian-sid",
            ],
            "almalinux": ["almalinux-x86_64", "almalinux-arm"],
            "resf": ["rockylinux-x86_64", "rockylinux-aarch64"],
        }
        skus = {
            ("Canonical", "ubuntu-26_04-lts"): ["server", "server-arm64", "minimal"],
            ("Canonical", "ubuntu-24_04-lts"): ["server", "server-arm64"],
            ("Canonical", "0001-com-ubuntu-server-jammy"): ["22_04-lts-gen2", "22_04-lts", "22_04-lts-arm64"],
            ("Canonical", "0001-com-ubuntu-server-focal"): ["20_04-lts-gen2", "20_04-lts-arm64"],
            ("Debian", "debian-13"): ["13-gen2", "13", "13-arm64"],
            ("Debian", "debian-12"): ["12-gen2", "12", "12-arm64"],
            ("Debian", "debian-11"): ["11-gen2", "11-backports-arm64-v2"],
            ("almalinux", "almalinux-x86_64"): ["8-gen2", "9-gen1", "9-gen2", "10-gen2"],
            ("almalinux", "almalinux-arm"): ["9-arm-gen2", "10-arm-gen2"],
            ("resf", "rockylinux-x86_64"): ["8-base", "9-base", "10-base"],
            ("resf", "rockylinux-aarch64"): ["rockylinux-aarch64-9", "rockylinux-aarch64-10"],
            ("MicrosoftWindowsDesktop", "windows-11"): [
                "win11-23h2-pro",
                "win11-24h2-pro",
                "win11-25h2-pro-g2",
                "win11-25h2-pro-zh-cn",
                "win11-25h2-pron",
                "win11-25h2-ent",
            ],
            ("MicrosoftWindowsDesktop", "windows-10"): [
                "win10-21h2-pro",
                "win10-22h2-pro",
                "win10-22h2-pro-g2",
                "win10-22h2-pro-zh-cn",
            ],
            ("MicrosoftWindowsServer", "WindowsServer"): [
                "2019-Datacenter-smalldisk",
                "2022-datacenter-azure-edition-smalldisk",
                "2022-datacenter-core",
                "2025-datacenter-azure-edition",
                "2028-datacenter-azure-edition",
                "2028-datacenter-core",
            ],
        }

        client = _FakeComputeClient(offers, skus)
        result = discover_dynamic_images(client, "East US")

        self.assertEqual(result["location"], "eastus")
        x64_urns = [item["urn"] for item in result["x64"]]
        arm_urns = [item["urn"] for item in result["arm64"]]

        # Ubuntu 取最近 3 个 LTS (26.04, 24.04, 22.04)，不含 20.04
        self.assertIn("Canonical:ubuntu-26_04-lts:server:latest", x64_urns)
        self.assertIn("Canonical:ubuntu-24_04-lts:server:latest", x64_urns)
        self.assertIn("Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest", x64_urns)
        self.assertNotIn("Canonical:0001-com-ubuntu-server-focal:20_04-lts-gen2:latest", x64_urns)
        self.assertIn("Canonical:ubuntu-26_04-lts:server-arm64:latest", arm_urns)

        # Debian 取最近 3 个主版本 (13, 12, 11)
        self.assertIn("Debian:debian-13:13-gen2:latest", x64_urns)
        self.assertIn("Debian:debian-12:12-gen2:latest", x64_urns)
        self.assertIn("Debian:debian-11:11-gen2:latest", x64_urns)
        self.assertIn("Debian:debian-13:13-arm64:latest", arm_urns)
        debian_names = [item["name"] for item in result["x64"] if item["key"].startswith("Debian_")]
        self.assertEqual(debian_names, ["Debian 13", "Debian 12", "Debian 11"])

        # AlmaLinux & RockyLinux 取最近 2 个主版本 (10, 9)
        self.assertIn("almalinux:almalinux-x86_64:10-gen2:latest", x64_urns)
        self.assertIn("almalinux:almalinux-x86_64:9-gen2:latest", x64_urns)
        self.assertNotIn("almalinux:almalinux-x86_64:8-gen2:latest", x64_urns)
        self.assertIn("almalinux:almalinux-arm:10-arm-gen2:latest", arm_urns)

        self.assertIn("resf:rockylinux-x86_64:10-base:latest", x64_urns)
        self.assertIn("resf:rockylinux-x86_64:9-base:latest", x64_urns)
        self.assertNotIn("resf:rockylinux-x86_64:8-base:latest", x64_urns)
        self.assertIn("resf:rockylinux-aarch64:rockylinux-aarch64-10:latest", arm_urns)
        self.assertIn("resf:rockylinux-aarch64:rockylinux-aarch64-9:latest", arm_urns)

        # Windows 11/10 取最新标准 Pro 版本
        self.assertIn("MicrosoftWindowsDesktop:windows-11:win11-25h2-pro-g2:latest", x64_urns)
        self.assertIn("MicrosoftWindowsDesktop:windows-10:win10-22h2-pro-g2:latest", x64_urns)

        # Windows Server 取最近 3 个 Datacenter 年份 (2028, 2025, 2022)，不含 2019 与 core
        self.assertIn("MicrosoftWindowsServer:WindowsServer:2028-datacenter-azure-edition:latest", x64_urns)
        self.assertIn("MicrosoftWindowsServer:WindowsServer:2025-datacenter-azure-edition:latest", x64_urns)
        self.assertIn("MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition-smalldisk:latest", x64_urns)
        self.assertNotIn("MicrosoftWindowsServer:WindowsServer:2019-Datacenter-smalldisk:latest", x64_urns)

    def test_partial_api_failure_does_not_break_other_distros(self):
        offers = {
            "Canonical": ["ubuntu-24_04-lts"],
            "Debian": ["debian-12"],
        }
        skus = {
            ("Canonical", "ubuntu-24_04-lts"): ["server"],
            ("MicrosoftWindowsServer", "WindowsServer"): ["2022-Datacenter"],
        }
        client = _FakeComputeClient(
            offers,
            skus,
            failing_calls={("offers", "almalinux"), ("skus", "Debian", "debian-12")},
        )
        result = discover_dynamic_images(client, "japaneast")
        x64_urns = [item["urn"] for item in result["x64"]]

        self.assertIn("Canonical:ubuntu-24_04-lts:server:latest", x64_urns)
        self.assertIn("MicrosoftWindowsServer:WindowsServer:2022-Datacenter:latest", x64_urns)

    def test_format_image_display_name_matches_catalog_naming(self):
        self.assertEqual(
            format_image_display_name("Canonical", "ubuntu-26_04-lts", "server"),
            "Ubuntu Server 26.04 LTS",
        )
        self.assertEqual(
            format_image_display_name("Canonical", "0001-com-ubuntu-server-jammy", "22_04-lts-gen2"),
            "Ubuntu Server 22.04 LTS",
        )
        self.assertEqual(
            format_image_display_name("Debian", "debian-12", "12-gen2"),
            "Debian 12",
        )
        self.assertEqual(
            format_image_display_name("Debian", "debian-13", "13-arm64"),
            "Debian 13",
        )
        self.assertEqual(
            format_image_display_name("almalinux", "almalinux-x86_64", "10-gen2"),
            "AlmaLinux 10",
        )
        self.assertEqual(
            format_image_display_name("resf", "rockylinux-aarch64", "rockylinux-aarch64-9"),
            "Rocky Linux 9",
        )
        self.assertEqual(
            format_image_display_name("MicrosoftWindowsDesktop", "windows-11", "win11-26h2-pro"),
            "Windows 11 Pro (26H2)",
        )
        self.assertEqual(
            format_image_display_name("MicrosoftWindowsServer", "WindowsServer", "2025-datacenter-azure-edition-smalldisk"),
            "Windows Server 2025 Datacenter",
        )
        self.assertIsNone(format_image_display_name("CustomPublisher", "custom-offer", "custom-sku"))


if __name__ == "__main__":
    unittest.main()
