"""Unit tests for options-blob decoding and map-name resolution (#265)."""

import base64
import json
import struct
import zlib

from app.poller.map_names import decode_match_options, resolve_map_name

# Real ``options`` blob captured from ``getRecentMatchHistory`` on
# 2026-06-10 (match 484298540, KnOfF vs Ryan Stecken). Upstream reported
# ``mapname="Marketplace.rms"`` for it, but the game was played on Black
# Forest (locstring id 10878 in the blob; confirmed by the replay) — the
# exact mislabel reported in #265.
REAL_OPTIONS_BLACK_FOREST = (
    "eNpFU9tqwzAM/Zd+Qa5+20OCs5EH2cvqMLK3LQwzh9aFMdL46ydbtlsoCB/pHOlIOZ33qcNfBcq6bseIdwx0fDO3"
    "v96HfNgRv+eYp/ftkAZ2qhvYF9U1wt3a3kd82aXpBh8KtTHoCJfq9kc1ExNvv/6tBGNbuPZnUd2kqmE/l0v7sd3f"
    "1wscn6q/vqqCE4/dSXu+S7MWUaeQlyboSLWw7yn24XMDvpXSDITzjJeS2yPWM+FW4nc4Z3ibK2lG6t3MqaYSBmcL"
    "+pqBGuPsfjbt8Ttwi39dS5q1BWermF/InyJ6qF2ISbtOvQmjmYg6UtmS8BU9HJ6ptwE1uxCjNhOKvAUFDKY56Atu"
    "a+gWinFnYOaYs2DORO/ONsQ9NMg9EvfouXG3iTPv6/AzUd8jer5ET0fc/Ui9GGDCdC/EA96XMuYXmJPyyzQHGPQ7"
    "egtmZUA3WGPvTa9DbuPror4LnpMXpbxucc/53hzOk/bcpp1JtaY7rh/1c64XeK8Rb8M9E3+dcafznTz41zbfmf9O"
    "4j2HbyPg+sh3pnz909PpHzYmAzk="
)


def encode_options(pairs: dict[str, str]) -> str:
    """Build a syntactically valid options blob (inverse of the decoder)."""
    body = b""
    for key, value in pairs.items():
        entry = f"{key}:{value}".encode()
        body += struct.pack("<I", len(entry)) + entry
    inner = bytes([len(pairs)]) + body
    raw = json.dumps(base64.b64encode(inner).decode()).encode()
    return base64.b64encode(zlib.compress(raw)).decode()


class TestDecodeMatchOptions:
    def test_decodes_real_payload(self):
        pairs = decode_match_options(REAL_OPTIONS_BLACK_FOREST)

        assert pairs is not None
        assert pairs["10"] == "10878"  # Black Forest's locstring id
        # A couple of other settings from the same blob, proving the
        # record walk stays aligned end to end.
        assert pairs["8"] == "120"  # map size
        assert pairs["28"] == "200"  # population cap

    def test_roundtrips_synthetic_payload(self):
        pairs = decode_match_options(encode_options({"10": "10895", "8": "120"}))

        assert pairs == {"10": "10895", "8": "120"}

    def test_garbage_base64_returns_none(self):
        assert decode_match_options("not base64!!") is None

    def test_valid_base64_but_not_zlib_returns_none(self):
        assert decode_match_options(base64.b64encode(b"plain bytes").decode()) is None

    def test_truncated_records_return_none(self):
        # Claims 2 records but carries only 1.
        entry = b"10:10878"
        inner = bytes([2]) + struct.pack("<I", len(entry)) + entry
        raw = json.dumps(base64.b64encode(inner).decode()).encode()
        blob = base64.b64encode(zlib.compress(raw)).decode()

        assert decode_match_options(blob) is None

    def test_decompressed_payload_not_json_returns_none(self):
        blob = base64.b64encode(zlib.compress(b"\x00\x01\x02 not json")).decode()

        assert decode_match_options(blob) is None


class TestResolveMapName:
    def test_known_locstring_id_wins_over_wrong_mapname(self):
        # The #265 repro: upstream said Marketplace, options say Black Forest.
        name = resolve_map_name(REAL_OPTIONS_BLACK_FOREST, "Marketplace.rms")

        assert name == "Black Forest"

    def test_unknown_locstring_id_falls_back(self):
        blob = encode_options({"10": "999999"})

        assert resolve_map_name(blob, "Some_Map.rms") == "Some_Map.rms"

    def test_custom_rms_id_zero_falls_back(self):
        # ``0`` = lobby ran a custom RMS file; mapname is the hosted file.
        blob = encode_options({"10": "0"})

        assert resolve_map_name(blob, "megarandom.rms2") == "megarandom.rms2"

    def test_scenario_id_negative_falls_back(self):
        blob = encode_options({"10": "-2"})

        assert resolve_map_name(blob, "my map") == "my map"

    def test_missing_map_key_falls_back(self):
        # Pre-automatch2 blobs carry key 11 instead of 10.
        blob = encode_options({"11": "29"})

        assert resolve_map_name(blob, "Arena.rms") == "Arena.rms"

    def test_non_numeric_map_value_falls_back(self):
        blob = encode_options({"10": "abc"})

        assert resolve_map_name(blob, "Arabia.rms") == "Arabia.rms"

    def test_missing_options_falls_back(self):
        assert resolve_map_name(None, "Arabia.rms") == "Arabia.rms"
        assert resolve_map_name("", "Arabia.rms") == "Arabia.rms"

    def test_undecodable_options_falls_back(self):
        assert resolve_map_name("garbage", "Arabia.rms") == "Arabia.rms"
