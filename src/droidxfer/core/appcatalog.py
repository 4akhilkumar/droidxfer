"""Safety ratings for Android packages: droidxfer's curated list, plus the optional community list.

The community list comes from Universal Android Debloater Next Generation (UAD-ng, GPL-3.0). It is not
bundled: it's downloaded on request and cached locally, and the curated list wins where both rate a package.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config import cache_dir

RATINGS = ("safe", "probably_safe", "advanced", "unsafe")
UAD_URL = (
    "https://raw.githubusercontent.com/Universal-Debloater-Alliance/"
    "universal-android-debloater-next-generation/main/resources/assets/uad_lists.json"
)
UAD_RATING = {"Recommended": "safe", "Advanced": "probably_safe", "Expert": "advanced", "Unsafe": "unsafe"}
MAX_DOWNLOAD = 20 * 1024 * 1024


@dataclass(slots=True)
class Rating:
    rating: str  # one of RATINGS, or "unknown"
    name: str
    description: str
    source: str  # "droidxfer" | "community" | ""
    community_rating: str = ""  # UAD's own label, for reference

    def to_dict(self) -> dict:
        return {
            "rating": self.rating, "name": self.name, "description": self.description,
            "source": self.source, "community_rating": self.community_rating,
        }


UNKNOWN = Rating("unknown", "", "", "")


def _load_curated() -> tuple[dict[str, dict], list[dict]]:
    raw = json.loads(resources.files("droidxfer.data").joinpath("app_catalog.json").read_text("utf-8"))
    return raw["packages"], raw.get("patterns", [])


def _matches(pattern: dict, package: str) -> bool:
    if "prefix" in pattern:
        return package.startswith(pattern["prefix"])
    segments = package.split(".")
    if "segment" in pattern:  # a whole dot-separated part, e.g. "resources" in com.x.wifi.resources.lls
        return pattern["segment"] in segments
    if "segment_suffix" in pattern:  # e.g. "overlay" matches safetycenteroverlay and overlay
        return any(s.endswith(pattern["segment_suffix"]) for s in segments)
    return False


def parse_uad(data: object) -> dict[str, dict]:
    """Validate and slim down a UAD-ng list ({package: {list, description, removal, ...}})."""
    if not isinstance(data, dict) or len(data) < 100:
        raise ValueError("the community list has an unexpected format")
    out: dict[str, dict] = {}
    for pkg, entry in data.items():
        if not isinstance(pkg, str) or not isinstance(entry, dict):
            continue
        removal = entry.get("removal")
        if removal not in UAD_RATING:
            continue
        description = entry.get("description") if isinstance(entry.get("description"), str) else ""
        out[pkg] = {"removal": removal, "description": description.strip(), "list": str(entry.get("list", ""))}
    if not out:
        raise ValueError("the community list contained no usable entries")
    return out


class AppCatalog:
    def __init__(self, cache: Path | None = None) -> None:
        self.curated, self.patterns = _load_curated()
        self.cache_path = (cache or cache_dir()) / "uad_lists.json"
        self.community: dict[str, dict] = {}
        self.community_fetched_at: float | None = None
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text("utf-8"))
            self.community = parse_uad(payload["packages"])
            self.community_fetched_at = float(payload["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            self.community, self.community_fetched_at = {}, None

    def status(self) -> dict:
        return {
            "curated_count": len(self.curated),
            "community_count": len(self.community),
            "community_fetched_at": self.community_fetched_at,
            "community_url": UAD_URL,
        }

    def fetch_community(self, url: str = UAD_URL, opener=urllib.request.urlopen) -> dict:
        """Download the UAD-ng list (blocking; call from a thread) and cache it."""
        request = urllib.request.Request(url, headers={"User-Agent": "droidxfer"})
        with opener(request, timeout=30) as response:
            body = response.read(MAX_DOWNLOAD + 1)
        if len(body) > MAX_DOWNLOAD:
            raise ValueError("the community list is unexpectedly large")
        packages = parse_uad(json.loads(body))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".uad-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "source": url, "packages": packages}, fh)
        os.replace(tmp, self.cache_path)
        self._load_cache()
        return self.status()

    def rate(self, package: str) -> Rating:
        community = self.community.get(package)
        uad_label = community["removal"] if community else ""
        if (entry := self.curated.get(package)) is not None:
            return Rating(entry["rating"], entry["name"], entry["description"], "droidxfer", uad_label)
        if community is not None:
            return Rating(UAD_RATING[community["removal"]], "", community["description"], "community", uad_label)
        # Generic rules (voice packs, resource overlays) come last: per-package knowledge is more specific.
        for pattern in self.patterns:
            if _matches(pattern, package):
                return Rating(pattern["rating"], pattern["name"], pattern["description"], "droidxfer")
        return UNKNOWN
