"""Saved-locations config: load/save app/config.json."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def _slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "loc"


def _load_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {"locations": []}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"locations": []}


def _save_raw(data: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def list_locations() -> list[dict]:
    return _load_raw().get("locations", [])


def get_location(loc_id: str) -> dict | None:
    for loc in list_locations():
        if loc["id"] == loc_id:
            return loc
    return None


def add_location(name: str, path: str) -> dict:
    name = name.strip()
    path = path.strip()
    if not name or not path:
        raise ValueError("Name and path are required.")

    folder = Path(path)
    if folder.exists() and not folder.is_dir():
        raise ValueError(f"{path} exists but is not a folder.")
    if not folder.exists():
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"Could not create folder {path}: {e}") from e

    data = _load_raw()
    locations = data.setdefault("locations", [])

    base_id = _slugify(name)
    existing_ids = {loc["id"] for loc in locations}
    new_id = base_id
    n = 2
    while new_id in existing_ids:
        new_id = f"{base_id}-{n}"
        n += 1

    loc = {"id": new_id, "name": name, "path": path}
    locations.append(loc)
    _save_raw(data)
    return loc


def delete_location(loc_id: str) -> None:
    data = _load_raw()
    data["locations"] = [
        loc for loc in data.get("locations", []) if loc["id"] != loc_id
    ]
    _save_raw(data)
