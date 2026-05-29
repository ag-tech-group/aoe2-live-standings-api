"""Broadcast-live client + URL parsing tests (#112): respx-mocked Twitch/YouTube."""

import httpx
import respx

from app.poller.broadcast import (
    _TWITCH_STREAMS_URL,
    _TWITCH_TOKEN_URL,
    _YOUTUBE_CHANNELS_URL,
    _YOUTUBE_SEARCH_URL,
    TwitchLiveClient,
    YouTubeLiveClient,
    extract_stream_urls,
    parse_twitch_login,
    parse_youtube_ref,
)


class TestParseTwitchLogin:
    def test_extracts_lowercase_login(self):
        assert parse_twitch_login("https://www.twitch.tv/Grubby") == "grubby"
        assert parse_twitch_login("https://twitch.tv/lowkotv") == "lowkotv"

    def test_rejects_reserved_paths(self):
        assert parse_twitch_login("https://twitch.tv/videos/12345") is None
        assert parse_twitch_login("https://www.twitch.tv/directory") is None

    def test_rejects_non_twitch_and_empty(self):
        assert parse_twitch_login("https://youtube.com/@x") is None
        assert parse_twitch_login("https://twitch.tv/") is None
        assert parse_twitch_login("not a url") is None


class TestParseYouTubeRef:
    def test_channel_id(self):
        assert parse_youtube_ref("https://www.youtube.com/channel/UCabc123") == (
            "channel_id",
            "UCabc123",
        )

    def test_handle(self):
        assert parse_youtube_ref("https://www.youtube.com/@TheSpiffingBrit/streams") == (
            "handle",
            "@TheSpiffingBrit",
        )

    def test_legacy_username(self):
        assert parse_youtube_ref("https://youtube.com/user/morelowko") == (
            "username",
            "morelowko",
        )

    def test_unresolvable_and_non_youtube(self):
        # Legacy /c/ and bare-vanity URLs aren't cheaply resolvable.
        assert parse_youtube_ref("https://www.youtube.com/c/SomeName") is None
        assert parse_youtube_ref("https://www.youtube.com/FollowGrubby") is None
        assert parse_youtube_ref("https://twitch.tv/grubby") is None


class TestExtractStreamUrls:
    def test_reads_list_and_flat_string_values(self):
        bag = {"streamUrls": ["https://twitch.tv/a", "https://youtube.com/@b"], "twitch": "x"}
        assert extract_stream_urls(bag) == ["https://twitch.tv/a", "https://youtube.com/@b", "x"]

    def test_ignores_non_strings_and_empty(self):
        assert extract_stream_urls({"n": 1, "list": [2, "u"], "obj": {"k": "v"}}) == ["u"]
        assert extract_stream_urls({}) == []


def _twitch_client() -> tuple[TwitchLiveClient, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    return TwitchLiveClient("cid", "secret", http), http


class TestTwitchLiveClient:
    async def test_returns_only_live_logins(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_TWITCH_TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
            mock.get(_TWITCH_STREAMS_URL).respond(
                json={"data": [{"user_login": "Grubby", "type": "live"}]}
            )
            twitch, http = _twitch_client()
            try:
                live = await twitch.get_live_logins(["grubby", "day9tv"])
            finally:
                await http.aclose()
        # user_login normalized lowercase; the offline channel is absent.
        assert live == {"grubby"}

    async def test_reauths_once_on_401(self):
        with respx.mock(assert_all_called=False) as mock:
            token = mock.post(_TWITCH_TOKEN_URL).respond(
                json={"access_token": "tok", "expires_in": 3600}
            )
            mock.get(_TWITCH_STREAMS_URL).mock(
                side_effect=[
                    httpx.Response(401),
                    httpx.Response(200, json={"data": [{"user_login": "grubby", "type": "live"}]}),
                ]
            )
            twitch, http = _twitch_client()
            try:
                live = await twitch.get_live_logins(["grubby"])
            finally:
                await http.aclose()
        assert live == {"grubby"}
        # Token minted once normally, then force-re-minted after the 401.
        assert token.call_count == 2


class TestYouTubeLiveClient:
    async def test_resolves_handle_then_detects_live(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_YOUTUBE_CHANNELS_URL).respond(json={"items": [{"id": "UC1"}]})
            mock.get(_YOUTUBE_SEARCH_URL).respond(json={"items": [{"id": {"videoId": "v"}}]})
            http = httpx.AsyncClient()
            youtube = YouTubeLiveClient("key", http)
            try:
                live = await youtube.get_live_refs([("handle", "@spiff")])
            finally:
                await http.aclose()
        assert live == {("handle", "@spiff")}

    async def test_channel_id_skips_resolution(self):
        with respx.mock(assert_all_called=False) as mock:
            channels = mock.get(_YOUTUBE_CHANNELS_URL).respond(json={"items": [{"id": "UC1"}]})
            mock.get(_YOUTUBE_SEARCH_URL).respond(json={"items": [{"id": {"videoId": "v"}}]})
            http = httpx.AsyncClient()
            youtube = YouTubeLiveClient("key", http)
            try:
                live = await youtube.get_live_refs([("channel_id", "UCdirect")])
            finally:
                await http.aclose()
        assert live == {("channel_id", "UCdirect")}
        assert channels.call_count == 0  # id used directly, no lookup

    async def test_offline_when_no_live_video(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_YOUTUBE_CHANNELS_URL).respond(json={"items": [{"id": "UC1"}]})
            mock.get(_YOUTUBE_SEARCH_URL).respond(json={"items": []})
            http = httpx.AsyncClient()
            youtube = YouTubeLiveClient("key", http)
            try:
                live = await youtube.get_live_refs([("handle", "@spiff")])
            finally:
                await http.aclose()
        assert live == set()

    async def test_channel_id_cached_across_calls(self):
        with respx.mock(assert_all_called=False) as mock:
            channels = mock.get(_YOUTUBE_CHANNELS_URL).respond(json={"items": [{"id": "UC1"}]})
            mock.get(_YOUTUBE_SEARCH_URL).respond(json={"items": [{"id": {"videoId": "v"}}]})
            http = httpx.AsyncClient()
            youtube = YouTubeLiveClient("key", http)
            try:
                await youtube.get_live_refs([("handle", "@spiff")])
                await youtube.get_live_refs([("handle", "@spiff")])
            finally:
                await http.aclose()
        assert channels.call_count == 1  # resolved once, then cached

    async def test_lookup_failure_degrades_to_offline(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_YOUTUBE_CHANNELS_URL).respond(403, json={"error": "quota"})
            http = httpx.AsyncClient()
            youtube = YouTubeLiveClient("key", http)
            try:
                live = await youtube.get_live_refs([("handle", "@spiff")])
            finally:
                await http.aclose()
        assert live == set()  # error swallowed, treated as offline
