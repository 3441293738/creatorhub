"""Shared display branding; technical identifiers and data paths remain stable."""
import json
from pathlib import Path
BRAND = json.loads(Path(__file__).with_name("brand.json").read_text(encoding="utf-8"))
PRODUCT_NAME = BRAND["name"]
