# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Adapters against fakes: TLS fake bridge, fake REST opener, fake MQTT client."""
import asyncio
import hashlib
import io
import json
import shutil
import ssl
import subprocess
import urllib.error

import pytest

from innr_autooff import logging_util
from innr_autooff.cloud_mqtt import MqttParams, parse_mqtt_host, send_command_blocking
from innr_autooff.config import ClientProfile
from innr_autooff.innr_api import AuthError, InnrApi
from innr_autooff.local_feed import LocalFeed
from innr_autooff.protocol import encode_frame

P1 = "00000000000000000000000000000001"
GW = "000000000000000000000000000000ff"


# ---------------------------------------------------------------- fake bridge (TLS, legacy cipher)
@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl CLI not available")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"),
                    "-out", str(d / "c.pem"), "-days", "1", "-subj", "/CN=fake-bridge"],
                   check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert((d / "c.pem").read_text())
    return d / "c.pem", d / "k.pem", hashlib.sha256(der).hexdigest()


def server_ctx(cert):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("AES256-SHA256:@SECLEVEL=0")  # exactly what the BG 220 offers
    ctx.load_cert_chain(cert[0], cert[1])
    return ctx


async def fake_bridge(cert, frames_per_conn):
    """Each connection: send its frames (split awkwardly), then close (like the real bridge)."""
    conns = []

    async def handle(reader, writer):
        conns.append(1)
        frames = frames_per_conn[min(len(conns) - 1, len(frames_per_conn) - 1)]
        data = b"".join(frames)
        for i in range(0, len(data), 7):  # partial frames across writes
            writer.write(data[i:i + 7])
            await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx(cert))
    return srv, srv.sockets[0].getsockname()[1], conns


def notif(dev, attr, idx=1):
    return encode_frame({"service": "device", "method": "setDevAttrNotif", "seq": "1", "srcAddr": f"1.{GW}",
                         "payload": {"devId": dev, "idx": idx, "attr": attr}, "ack": {"code": 200, "desc": "Success."}})


def test_local_feed_receives_events_reconnects_and_pins(cert):
    async def main():
        srv, port, conns = await fake_bridge(cert, [[notif(P1, {"OnOff": 1}, 1), notif(P1, {"Watts": 5}, 2)],
                                                    [notif(P1, {"OnOff": 0}, 3)]])
        events, status, pins = [], [], []
        feed = LocalFeed("127.0.0.1", port, lambda d, a, i: events.append((d, a, i)), status.append,
                         on_new_pin=pins.append)
        task = asyncio.create_task(feed.run())
        for _ in range(100):
            if len(events) >= 3:
                break
            await asyncio.sleep(0.05)
        feed.stop()
        await asyncio.wait_for(task, 5)
        srv.close()
        return events, status, pins, conns

    events, status, pins, conns = asyncio.run(main())
    assert [e[1] for e in events[:3]] == [{"OnOff": 1}, {"Watts": 5}, {"OnOff": 0}]
    assert len(conns) >= 2 and True in status and False in status
    assert pins == [cert[2]]


def test_local_feed_refuses_wrong_pin(cert):
    async def main():
        srv, port, conns = await fake_bridge(cert, [[notif(P1, {"OnOff": 1})]])
        events = []
        feed = LocalFeed("127.0.0.1", port, lambda d, a, i: events.append(d), lambda s: None, pin_sha256="0" * 64)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.6)
        feed.stop()
        await asyncio.wait_for(task, 5)
        srv.close()
        return events

    assert asyncio.run(main()) == []


def test_local_feed_never_writes(cert):
    async def main():
        received = []

        async def handle(reader, writer):
            try:
                received.append(await asyncio.wait_for(reader.read(100), 0.5))
            except asyncio.TimeoutError:
                received.append(b"")
            writer.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx(cert))
        feed = LocalFeed("127.0.0.1", srv.sockets[0].getsockname()[1], lambda *a: None, lambda s: None)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.8)
        feed.stop()
        await asyncio.wait_for(task, 5)
        srv.close()
        return received

    assert asyncio.run(main())[0] == b""  # R4.3: no application data sent


def test_local_feed_backoff_on_unreachable_bridge():
    async def main():
        feed = LocalFeed("127.0.0.1", 1, lambda *a: None, lambda s: None)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.3)
        feed.stop()
        await asyncio.wait_for(task, 5)
        return feed.connected
    assert asyncio.run(main()) is False


# ---------------------------------------------------------------- REST client
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self):
        self.calls = []
        self.expire_once = False
        self.refresh_ok = True
        self.login_status = 200

    def __call__(self, req, timeout=None):
        path = req.full_url.split("/v17", 1)[1]
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, dict(req.header_items()), body))

        def err(code):
            return urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b'{"msg":"no"}'))
        if path.startswith("/users/loginWithEmailPassword"):
            if self.login_status != 200:
                raise err(self.login_status)
            return FakeResp(json.dumps({"id": "u" * 32, "accessToken": "ACCESS-TOKEN-1", "refreshToken": "REFRESH-TOKEN-1",
                                        "terminalIndex": "42"}).encode())
        if path.startswith("/users/refreshToken"):
            if not self.refresh_ok:
                raise err(401)
            return FakeResp(json.dumps({"accessToken": "ACCESS-TOKEN-2"}).encode())
        token = dict(req.header_items()).get("Token")
        if self.expire_once and token == "ACCESS-TOKEN-1":
            self.expire_once = False  # the token expired once; a fresh login/refresh works again
            raise err(401)
        if path.startswith("/houses"):
            return FakeResp(json.dumps([{"id": 1000001, "deviceCount": 1}, {"id": 1, "deviceCount": 0}]).encode())
        if path.startswith("/devices"):
            return FakeResp(json.dumps([{"id": P1, "parentId": GW, "type": "plug", "password": "DEVICE-PW-12"}]).encode())
        if path.startswith("/commons/mqttConfig"):
            return FakeResp(json.dumps({"host": "global-eu-mqtt.arnoo.com:8443", "password": "MQTT-PW-123456",
                                        "heartbeat": 45, "clientId": "425e14-x"}).encode())
        raise err(404)


# obviously fake identity: the real one is operator configuration and never part of this repository
PROFILE = ClientProfile(
    app_id="test-app-id-7", user_agent="TestAgent/9.9 (unit tests)", package_name="example.test.pkg",
    app_version="9.1.0", web_version="9.2.0", os_version="77", web_view_version="9.3.0",
    device_model="acme/tester-1", device_name="Test Rig", origin="https://origin.example.test:4443",
    build_number=424242)


def api(opener, clock=None, profile=PROFILE):
    t = [0.0]
    return InnrApi("a@b.c", "account-password-1", "eu", "T" * 21, profile, opener=opener,
                   clock=clock or (lambda: t[0])), t


def test_login_sends_md5_and_persistent_terminal_and_registers_secrets():
    op = Opener()
    a, _ = api(op)
    house, devices = a.devices()
    assert house == "1000001" and devices[0]["id"] == P1
    login = next(c for c in op.calls if c[1].startswith("/users/login"))
    assert login[3]["password"] == hashlib.md5(b"account-password-1").hexdigest()
    assert login[3]["terminalId"] == "T" * 21
    for s in ("account-password-1", "ACCESS-TOKEN-1", "REFRESH-TOKEN-1", "DEVICE-PW-12"):
        assert s not in logging_util.scrub(f"x {s} y")
    cfg = a.mqtt_config(house)
    assert a.client_id(cfg) == f"42-{'u' * 32}"
    assert "MQTT-PW-123456" not in logging_util.scrub("MQTT-PW-123456")
    mq = next(c for c in op.calls if c[1].startswith("/commons"))
    assert mq[2].get("Houseid") == "1000001"


def test_requests_present_exactly_the_configured_identity():
    """R13.1: headers and login body come from the profile, nothing is built in."""
    op = Opener()
    a, _ = api(op)
    a.login(force=True)
    a.mqtt_config(a.devices()[0])
    assert op.calls
    for method, path, headers, body in op.calls:
        h = {k.lower(): v for k, v in headers.items()}
        assert h["user-agent"] == PROFILE.user_agent and h["appid"] == PROFILE.app_id
        assert h["origin"] == PROFILE.origin and h["referer"] == PROFILE.origin + "/"
        assert h["x-requested-with"] == PROFILE.package_name and h["webversion"] == PROFILE.web_version
    login = next(c for c in op.calls if c[1].startswith("/users/login"))[3]
    assert login["nativeVersion"] == PROFILE.app_version and login["buildNumber"] == PROFILE.build_number
    assert login["osVersion"] == PROFILE.os_version and login["packName"] == PROFILE.package_name
    assert login["terminalModelName"] == PROFILE.device_model and login["terminalName"] == PROFILE.device_name
    assert login["webVersion"] == PROFILE.web_version and login["webViewVersion"] == PROFILE.web_view_version


def test_refresh_request_uses_the_configured_versions():
    op = Opener()
    a, _ = api(op)
    a.login(force=True)
    op.expire_once = True
    a.devices()
    refresh = next(c for c in op.calls if c[1].startswith("/users/refreshToken"))[3]
    assert refresh["nativeVersion"] == PROFILE.web_version and refresh["webVersion"] == PROFILE.web_version


def test_a_different_profile_changes_the_requests():
    other = ClientProfile(**{**PROFILE.__dict__, "app_id": "another-id", "user_agent": "Other/1", "build_number": 5})
    op = Opener()
    a, _ = api(op, profile=other)
    a.login(force=True)
    method, path, headers, body = op.calls[0]
    h = {k.lower(): v for k, v in headers.items()}
    assert h["appid"] == "another-id" and h["user-agent"] == "Other/1" and body["buildNumber"] == 5


def test_token_refresh_on_401():
    op = Opener()
    a, _ = api(op)
    a.login(force=True)
    op.expire_once = True
    a.devices()
    assert any(c[1].startswith("/users/refreshToken") for c in op.calls)


def test_relogin_when_refresh_fails():
    op = Opener()
    a, _ = api(op)
    a.login(force=True)
    op.expire_once, op.refresh_ok = True, False
    a.devices()
    assert sum(1 for c in op.calls if c[1].startswith("/users/login")) == 2


def test_login_failure_and_rate_limit():
    op = Opener()
    op.login_status = 400
    a, t = api(op)
    with pytest.raises(AuthError):
        a.login()
    assert a.last_login_ok is False and "HTTP 400" in a.last_error
    with pytest.raises(AuthError):
        a.login()  # within 60 s: refused without a request
    assert sum(1 for c in op.calls if c[1].startswith("/users/login")) == 1
    t[0] += 61
    with pytest.raises(AuthError):
        a.login()
    assert sum(1 for c in op.calls if c[1].startswith("/users/login")) == 2


def test_api_error_text_does_not_contain_url_query():
    op = Opener()
    a, _ = api(op)
    a.login(force=True)
    with pytest.raises(Exception) as ei:
        a._authed_get("/unknown?token=abc")
    assert "token=abc" not in str(ei.value)


# ---------------------------------------------------------------- MQTT command (fake paho client)
class RC:
    def __init__(self, fail=False):
        self.is_failure = fail


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload.encode()


class FakeClient:
    instances = []

    def __init__(self, params, answer="resp", refuse=False):
        self.params, self.answer, self.refuse = params, answer, refuse
        self.published, self.subscribed = [], []
        FakeClient.instances.append(self)

    def connect(self, host, port, keepalive):
        self.target = (host, port, keepalive)

    def loop_start(self):
        self.on_connect(self, None, None, RC(self.refuse))

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos):
        self.published.append((topic, json.loads(payload)))
        if topic.endswith("/device/setDevAttrReq") and self.answer == "resp":
            self.on_message(self, None, FakeMsg(f"iot/v1/c/{'u' * 32}/device/setDevAttrResp",
                                                json.dumps({"method": "setDevAttrResp", "payload": {}})))
        if topic.endswith("/device/setDevAttrReq") and self.answer == "notif":
            self.on_message(self, None, FakeMsg(f"iot/v1/cb/{GW}/device/setDevAttrNotif", json.dumps(
                {"method": "setDevAttrNotif", "payload": {"devId": P1, "attr": {"OnOff": 0}}})))


def params():
    return MqttParams("global-eu-mqtt.arnoo.com", 8443, "/mqtt", True, "42-u", "u" * 32, "MQTT-PW", 45)


@pytest.mark.parametrize("answer,expected", [("resp", True), ("notif", True), ("none", False)])
def test_command_message_format_matches_proven_prototype(answer, expected):
    FakeClient.instances.clear()
    ok = send_command_blocking(params(), "u" * 32, P1, GW, "DEVICE-PW-12", {"OnOff": 0}, wait=0.2,
                               client_factory=lambda p: FakeClient(p, answer))
    assert ok is expected
    c = FakeClient.instances[0]
    assert c.target == ("global-eu-mqtt.arnoo.com", 8443, 45)
    topics = [t for t, _ in c.published]
    assert topics[0] == f"iot/v1/cb/{'u' * 32}/user/connect"
    assert f"iot/v1/c/{GW}/device/setDevAttrReq" in topics
    assert topics[-1] == f"iot/v1/cb/{'u' * 32}/user/disconnect"
    req = next(m for t, m in c.published if t.endswith("setDevAttrReq"))
    assert req["method"] == "setDevAttrReq" and req["service"] == "device"
    assert req["payload"] == {"devId": P1, "parentId": GW, "userId": "u" * 32, "password": "DEVICE-PW-12",
                              "attr": {"OnOff": 0}}
    assert req["srcAddr"] == f"0.{'u' * 32}" and len(req["seq"]) == 9
    assert f"iot/v1/cb/{GW}/#" in c.subscribed


def test_command_connect_refused_raises():
    with pytest.raises(ConnectionError):
        send_command_blocking(params(), "u", P1, GW, "pw", {"OnOff": 0}, wait=0.1,
                              client_factory=lambda p: FakeClient(p, refuse=True))


@pytest.mark.parametrize("raw,expected", [
    ("global-eu-mqtt.arnoo.com:8443", ("global-eu-mqtt.arnoo.com", 8443, "/mqtt", True)),
    ("wss://h.example/mqtt", ("h.example", 443, "/mqtt", True)),
    ("ws://h:8083/ws", ("h", 8083, "/ws", False)),
    ("h.example", ("h.example", 443, "/mqtt", True)),
])
def test_parse_mqtt_host(raw, expected):
    assert parse_mqtt_host(raw) == expected


# ---------------------------------------------------------------- cloud state feed
class FeedClient:
    """Fake broker connection: delivers one notification, then the broker drops it (like the real one)."""
    live = 0
    max_live = 0
    connects = 0

    def __init__(self, params, drop_after=0.3):
        self.drop_after = drop_after
        self.subscribed = []

    def connect(self, host, port, keepalive):
        FeedClient.connects += 1

    def loop_start(self):
        import threading as th
        FeedClient.live += 1
        FeedClient.max_live = max(FeedClient.max_live, FeedClient.live)
        self.on_connect(self, None, None, RC(False))
        self.on_message(self, None, FakeMsg(f"iot/v1/cb/{GW}/device/setDevAttrNotif", json.dumps(
            {"method": "setDevAttrNotif", "payload": {"devId": P1, "idx": 7, "attr": {"OnOff": 1}}})))
        self._t = th.Timer(self.drop_after, lambda: self.on_disconnect(self, None, None, RC(False)))
        self._t.start()

    def loop_stop(self):
        self._t.cancel()

    def disconnect(self):
        FeedClient.live -= 1

    def subscribe(self, topic, qos):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos):
        pass


def test_cloud_feed_delivers_events_reconnects_and_pauses():
    from innr_autooff.cloud_mqtt import CloudFeed
    FeedClient.live = FeedClient.max_live = FeedClient.connects = 0

    async def main():
        loop = asyncio.get_running_loop()
        events, status = [], []
        feed = CloudFeed(lambda: (params(), "u" * 32, [GW]), lambda d, a, i: events.append((d, a, i)),
                         status.append, loop, client_factory=lambda p: FeedClient(p))
        feed.start()
        await asyncio.sleep(1.5)
        assert len(events) >= 2 and events[0] == (P1, {"OnOff": 1}, 7)  # reconnected after the drop
        assert True in status and False in status
        assert await asyncio.to_thread(feed.pause, 3.0) is True
        n = FeedClient.connects
        await asyncio.sleep(0.8)
        assert FeedClient.connects == n and FeedClient.live == 0  # stays away while paused
        feed.resume()
        await asyncio.sleep(0.8)
        assert FeedClient.connects > n
        feed.stop()
        await asyncio.sleep(0.5)

    asyncio.run(main())
    assert FeedClient.max_live == 1  # never two connections with the same client id
