"""Resolve a match's real map name from the upstream ``options`` blob.

The ``mapname`` field on ``getRecentMatchHistory`` is unreliable for ranked
automatch games — sampling tracked players' histories against replay-derived
ground truth (aoe2insights) showed it wrong for ~half the matches (#265:
a Black Forest game surfaced as "Marketplace"). It never self-corrects.

The authoritative map travels in the match's ``options`` field instead: a
zlib-compressed, base64-wrapped bag of numeric ``key:value`` settings. Key
``10`` carries the *locstring id* of the map's display name — a stable id
from the game's string table, not a per-patch value (verified consistent
across a month of matches spanning a game patch). ``0`` means the lobby ran
a custom RMS file and ``-2`` a scenario; for those, upstream ``mapname`` is
the actual hosted file name, so falling back to it is correct.

``MAP_NAME_BY_LOCSTRING_ID`` lists **verified ids only** — each entry was
cross-checked against at least one replay-derived map name (see #265 for
the method: decode ``options`` key 10, compare with aoe2insights' match
page, which parses the actual replay). Unknown ids fall back to the raw
``mapname`` (the pre-#265 behavior) and log once per process so a ranked
map-pool rotation that introduces a new id is visible in Cloud Logging
(``event="unknown_map_locstring"``). Do not guess ids — a wrong entry
mis-labels every match on that map (mirrors the
``DEFAULT_MATCHTYPE_TO_LEADERBOARD`` rule in ``app.poller.parsers``).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

import structlog

logger = structlog.get_logger(__name__)

# Locstring id -> map display name. Every entry verified against
# replay-derived ground truth on 2026-06-10 (#265). The ids follow the
# game's string table (10875 = Arabia, then the classic map list in order;
# the 301xxx block is DLC-shipped pool maps), but entries are only added
# here once observed + verified, never derived arithmetically.
MAP_NAME_BY_LOCSTRING_ID: dict[int, str] = {
    10875: "Arabia",
    10878: "Black Forest",
    10882: "Fortress",
    10883: "Gold Rush",
    10884: "Highland",
    10886: "Mediterranean",
    10889: "Team Islands",
    10892: "Mongolia",
    10894: "Yucatan",
    10895: "Arena",
    10897: "Oasis",
    10901: "Nomad",
    10919: "Hideout",
    10920: "Hill Fort",
    10921: "Lombardia",
    10924: "MegaRandom",
    10932: "Land Nomad",
    10938: "Amazon Tunnel",
    10939: "Coastal Forest",
    10940: "African Clearing",
    10945: "Michi",
    10949: "Eruption",
    10953: "Marketplace",
    10956: "Northern Isles",
    10960: "Enclosed",
    10963: "Land Madness",
    10968: "Cliffbound",
    10969: "Isthmus",
    10973: "River Divide",
    10978: "Karsts",
    10979: "Glade",
    10980: "Fortified Clearing",
    11005: "QS Runestones",
    11012: "Border Dispute",
    11013: "Graupel",
    301100: "Kilimanjaro",
    301104: "Socotra",
    301111: "Bohemia",
}

# Unknown locstring ids already logged this process — one line per new id,
# not one per match per poll cycle. INFO on purpose: new pool maps are
# routine, not an incident (Sentry noise discipline, #262).
_logged_unknown_ids: set[int] = set()


def decode_match_options(options_b64: str) -> dict[str, str] | None:
    """Decode the ``options`` blob into its ``{key: value}`` settings dict.

    Layout: base64(zlib(JSON string)) where the JSON string is itself
    base64 of ``[u8 record_count][record_count x (u32 length, ASCII
    "key:value")]``. Returns ``None`` for anything that doesn't parse —
    truncated data, a future format change, or pre-automatch2 blobs are
    all expected in the wild and must not break a poll cycle.
    """
    try:
        raw = zlib.decompress(base64.b64decode(options_b64))
        inner = base64.b64decode(json.loads(raw))
        count = inner[0]
        pos = 1
        pairs: dict[str, str] = {}
        for _ in range(count):
            if pos + 4 > len(inner):
                return None
            (length,) = struct.unpack_from("<I", inner, pos)
            pos += 4
            if pos + length > len(inner):
                return None
            entry = inner[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
            key, _, value = entry.partition(":")
            pairs[key] = value
        return pairs
    except Exception:
        return None


def resolve_map_name(options_b64: str | None, fallback: str) -> str:
    """Best-effort real map name; ``fallback`` is the upstream ``mapname``.

    Falls back whenever the blob is missing/undecodable, the map key is
    absent or non-positive (custom RMS / scenario — where ``mapname`` is
    the actual hosted file), or the locstring id isn't in the verified
    table.
    """
    if not options_b64:
        return fallback
    pairs = decode_match_options(options_b64)
    if pairs is None:
        return fallback
    try:
        locstring_id = int(pairs.get("10", ""))
    except ValueError:
        return fallback
    if locstring_id <= 0:
        return fallback
    name = MAP_NAME_BY_LOCSTRING_ID.get(locstring_id)
    if name is None:
        if locstring_id not in _logged_unknown_ids:
            _logged_unknown_ids.add(locstring_id)
            logger.info(
                "unknown_map_locstring",
                locstring_id=locstring_id,
                fallback_mapname=fallback,
            )
        return fallback
    return name
