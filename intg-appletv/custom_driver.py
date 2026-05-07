"""
Helper script to create a custom driver.json configuration in the build process.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
from pathlib import Path

from i18n import _a
from setup_flow import setup_data_schema

with open(Path(__file__).parent / ".." / "driver.json", "r", encoding="utf-8") as file:
    driver_info = json.load(file)

    driver_info["driver_id"] = "appletv_custom"
    driver_info["name"]["en"] = "Apple TV custom"
    driver_info["description"] = _a("Control your Apple TV with Remote Two/3.")
    driver_info["setup_data_schema"] = setup_data_schema()

    with open(Path(__file__).parent / ".." / "driver_custom.json", "w", encoding="utf-8") as f:
        json.dump(driver_info, f, ensure_ascii=False, indent=2)
