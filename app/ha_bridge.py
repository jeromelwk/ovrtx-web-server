"""Home Assistant MQTT <-> USD light/sensor bridge for the web viewer.

Ported from the omni.home.assistant Kit extension: an MQTT client subscribes
to Home Assistant's MQTT statestream, and each subscribed topic prefix is
mapped to a prim in the currently open scene -- a light (kind="light") drives
UsdLux intensity/color/exposure via RenderSession.apply_light_state, a binary
sensor (kind="presence", e.g. a motion detector) drives a prim's visibility
via RenderSession.apply_presence_state.

Home Assistant's MQTT statestream publishes one sub-topic per attribute under
`<topic_prefix>/<field>` (e.g. `.../state`, `.../brightness`, `.../rgb_color`)
rather than a single JSON blob, so incoming fields are accumulated into a
mapping's `state` dict as they arrive and the prim is re-applied from the
merged dict each time.

Connection credentials and prim<->topic mappings are stored locally in
`ha_state.json` next to this file (gitignored, never committed) rather than
in Kit persistent settings, since this server has no equivalent facility.
Mappings are scoped per opened scene path so switching files doesn't mix up
unrelated mappings.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from .render_session import RenderSession

log = logging.getLogger("ovrtx_server.ha_bridge")

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_state.json")


def parse_field_value(field_name: str, payload: str) -> Any:
    """Decode one Home Assistant MQTT statestream sub-topic payload: `state`
    is a raw, unquoted string ("on"/"off"); every other attribute is
    JSON-encoded, with a fallback to the raw string if it isn't valid JSON."""
    if field_name == "state":
        return payload
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


@dataclass
class ConnectionSettings:
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls_enabled: bool = False
    topic_filter: str = "#"
    qos: int = 0


@dataclass
class TopicMapping:
    prim_path: str
    topic_prefix: str
    kind: str = "light"  # "light" or "presence"
    max_intensity: float = 31000.0
    max_exposure: float = 0.0
    state: Dict[str, Any] = field(default_factory=dict)


class HomeAssistantBridge:
    def __init__(self, session: RenderSession) -> None:
        self.session = session
        self._lock = threading.RLock()
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.mappings: Dict[str, TopicMapping] = {}

        self._state = self._load_state_file()
        c = self._state.get("connection", {})
        self.conn = ConnectionSettings(
            host=c.get("host", ""),
            port=int(c.get("port", 1883)),
            username=c.get("username", ""),
            password=c.get("password", ""),
            tls_enabled=bool(c.get("tls_enabled", False)),
            topic_filter=c.get("topic_filter", "#"),
            qos=int(c.get("qos", 0)),
        )

    # ------------------------------------------------------------------
    # Local persistence
    # ------------------------------------------------------------------
    def _load_state_file(self) -> dict:
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state_file(self) -> None:
        self._state["connection"] = asdict(self.conn)
        try:
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
        except OSError:
            log.exception("Failed to write %s", _STATE_FILE)

    def save_connection(self, conn: ConnectionSettings) -> None:
        with self._lock:
            self.conn = conn
            self._save_state_file()

    def save_mappings_for_scene(self, scene_path: str) -> None:
        with self._lock:
            entries = []
            for m in self.mappings.values():
                d = asdict(m)
                d.pop("state", None)
                entries.append(d)
            self._state.setdefault("mappings_by_scene", {})[scene_path] = entries
            self._save_state_file()

    def load_mappings_for_scene(self, scene_path: str) -> None:
        with self._lock:
            entries = self._state.get("mappings_by_scene", {}).get(scene_path, [])
            self.mappings = {}
            for e in entries:
                prim_path = e.get("prim_path", "")
                topic_prefix = e.get("topic_prefix", "")
                if not prim_path or not topic_prefix:
                    continue
                self.mappings[topic_prefix] = TopicMapping(
                    prim_path=prim_path,
                    topic_prefix=topic_prefix,
                    kind=e.get("kind", "light"),
                    max_intensity=float(e.get("max_intensity", 31000.0)),
                    max_exposure=float(e.get("max_exposure", 0.0)),
                )
            if self.connected:
                self._subscribe_all()

    # ------------------------------------------------------------------
    # MQTT connection
    # ------------------------------------------------------------------
    def connect(self) -> None:
        # loop_start()/loop_stop() below must never run while holding
        # self._lock: loop_stop() joins paho's own network thread, and that
        # thread's on_connect/on_disconnect/on_message callbacks each take
        # self._lock themselves -- doing the blocking join while still
        # holding the lock deadlocks the two threads against each other
        # (observed while testing against an unreachable host: disconnect()
        # never returned). Only the shared `self.client`/`self.connected`
        # fields are protected by the lock; the actual paho calls happen
        # outside it.
        old_client = None
        with self._lock:
            old_client = self.client
            self.client = None
        if old_client is not None:
            self._teardown_client(old_client)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True)
        if self.conn.username:
            client.username_pw_set(self.conn.username, self.conn.password or None)
        if self.conn.tls_enabled:
            client.tls_set()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        try:
            client.connect_async(self.conn.host, self.conn.port, keepalive=60)
            client.loop_start()
        except Exception:
            raise
        with self._lock:
            self.client = client

    def disconnect(self) -> None:
        with self._lock:
            client = self.client
            self.client = None
            self.connected = False
        if client is not None:
            self._teardown_client(client)

    @staticmethod
    def _teardown_client(client: "mqtt.Client") -> None:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            return {"connected": self.connected, "host": self.conn.host, "port": self.conn.port}

    def _subscribe_all(self) -> None:
        if self.client is None:
            return
        try:
            self.client.subscribe(self.conn.topic_filter, qos=self.conn.qos)
            for prefix in self.mappings:
                self.client.subscribe(prefix + "/#", qos=self.conn.qos)
        except Exception:
            log.exception("Failed to (re)subscribe MQTT topics")

    def _subscribe_one(self, prefix: str) -> None:
        if self.client is not None and self.connected:
            try:
                self.client.subscribe(prefix + "/#", qos=self.conn.qos)
            except Exception:
                log.exception("Failed to subscribe to %s", prefix)

    # ------------------------------------------------------------------
    # paho-mqtt callbacks (CallbackAPIVersion.VERSION2 signatures). on_message
    # fires on paho's own background network thread; RenderSession.apply_*
    # take their own lock internally, so no marshaling onto another thread is
    # needed for the resulting stage write to be safe.
    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        log.info("Connected to MQTT broker %s:%s", self.conn.host, self.conn.port)
        with self._lock:
            self.connected = True
            self._subscribe_all()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code=0, properties=None) -> None:
        log.info("Disconnected from MQTT broker")
        with self._lock:
            self.connected = False

    def _on_connect_fail(self, client, userdata) -> None:
        log.warning("Failed to connect to MQTT broker %s:%s", self.conn.host, self.conn.port)
        with self._lock:
            self.connected = False

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:
            return
        topic = msg.topic
        with self._lock:
            for prefix, mapping in self.mappings.items():
                if not topic.startswith(prefix + "/"):
                    continue
                field_name = topic[len(prefix) + 1:]
                mapping.state[field_name] = parse_field_value(field_name, payload)
                self._apply(mapping)
                return

    def _apply(self, mapping: TopicMapping) -> None:
        try:
            if mapping.kind == "presence":
                self.session.apply_presence_state(mapping.prim_path, mapping.state)
            else:
                self.session.apply_light_state(
                    mapping.prim_path, mapping.state,
                    max_intensity=mapping.max_intensity, max_exposure=mapping.max_exposure,
                )
        except Exception:
            log.exception("Failed to apply mapping %s -> %s", mapping.topic_prefix, mapping.prim_path)

    # ------------------------------------------------------------------
    # Mapping management
    # ------------------------------------------------------------------
    def add_mapping(
        self, prim_path: str, topic_prefix: str, kind: str, max_intensity: float, max_exposure: float = 0.0
    ) -> None:
        with self._lock:
            topic_prefix = topic_prefix.rstrip("/")
            self.mappings[topic_prefix] = TopicMapping(
                prim_path=prim_path, topic_prefix=topic_prefix, kind=kind,
                max_intensity=max_intensity, max_exposure=max_exposure,
            )
            self._subscribe_one(topic_prefix)

    def remove_mapping(self, topic_prefix: str) -> None:
        with self._lock:
            self.mappings.pop(topic_prefix, None)
            if self.client is not None and self.connected:
                try:
                    self.client.unsubscribe(topic_prefix + "/#")
                except Exception:
                    pass

    def detect_from_scene(self) -> int:
        with self._lock:
            found = self.session.scan_lights_and_sensors()
            for entry in found:
                prefix = entry["topic_prefix"]
                self.mappings[prefix] = TopicMapping(
                    prim_path=entry["prim_path"], topic_prefix=prefix, kind=entry["kind"],
                    max_intensity=entry.get("max_intensity", 31000.0),
                )
                self._subscribe_one(prefix)
            return len(found)

    def to_list(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "prim_path": m.prim_path,
                    "topic_prefix": m.topic_prefix,
                    "kind": m.kind,
                    "max_intensity": m.max_intensity,
                    "max_exposure": m.max_exposure,
                }
                for m in self.mappings.values()
            ]
