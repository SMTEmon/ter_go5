import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Any

from common import (
    MESH_HMAC_MAX_SKEW,
    MESH_HEARTBEAT_INTERVAL,
    PEER_TIMEOUT,
    mesh_pack,
    mesh_unpack,
)

log = logging.getLogger("client")

@dataclass
class PeerInfo:
    uuid: str
    username: str
    ip: str
    port: int
    last_heard: float = float('-inf')
    last_ts: float = 0.0
    armed: bool = False
    game_running: bool = False
    server_ok: bool = False
    disconnect_kill: bool = False
    mesh_kill_fired: bool = False

    def alive(self, now: float) -> bool:
        return (now - self.last_heard) <= PEER_TIMEOUT


class MeshTransport(asyncio.DatagramProtocol):
    def __init__(self, own_uuid: str, username: str, password: str, bind_port: int,
                 on_kill: Callable[[str, str, str, str], None],
                 get_heartbeat_body: Callable[[], dict]):
        self.own_uuid = own_uuid
        self.username = username
        self.password = password
        self.bind_port = bind_port
        self.on_kill = on_kill
        self.get_heartbeat_body = get_heartbeat_body
        
        self.peers: Dict[str, PeerInfo] = {}
        self.transport = None
        self.loop = None
        
        self.last_bad_sig_warning = 0.0

    def upsert_peer(self, uuid: str, ip: str, port: int, username: str):
        if uuid == self.own_uuid:
            return
        if uuid not in self.peers:
            self.peers[uuid] = PeerInfo(uuid=uuid, username=username, ip=ip, port=port)
        else:
            p = self.peers[uuid]
            p.username = username
            # We don't overwrite ip/port here unless we want to.
            # The prompt says: "never overwrite a fresher ip learned from a live datagram"
            # So upsert_peer is mainly used to seed from config or roster, and we shouldn't overwrite if it's fresher?
            # Actually, we can just set them if it's a new peer, and let datagrams update it.
            if p.last_heard == float('-inf'):
                p.ip = ip
                p.port = port

    def alive_peers(self) -> List[PeerInfo]:
        now = time.monotonic()
        return [p for p in self.peers.values() if p.alive(now)]

    def seen_uuids(self) -> List[str]:
        return [p.uuid for p in self.alive_peers()]

    async def start(self, loop):
        self.loop = loop
        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: self,
                local_addr=("0.0.0.0", self.bind_port)
            )
        except OSError as e:
            log.error(f"Mesh bind failed on port {self.bind_port} ({e}). Mesh fallback disabled.")
            self.transport = None

    def stop(self):
        if self.transport:
            self.transport.close()
            self.transport = None

    def send(self, body: dict, addr: Tuple[str, int]):
        if not self.transport:
            return
        body["from"] = self.own_uuid
        body["name"] = self.username
        body["ts"] = time.time()
        
        datagram = mesh_pack(body, self.password)
        self.transport.sendto(datagram, addr)

    def broadcast(self, body: dict):
        if not self.transport:
            return
        # dedupe
        addrs = set((p.ip, p.port) for p in self.peers.values() if p.ip and p.port)
        
        body["from"] = self.own_uuid
        body["name"] = self.username
        body["ts"] = time.time()
        
        datagram = mesh_pack(body, self.password)
        for addr in addrs:
            self.transport.sendto(datagram, addr)

    def send_hello_all(self):
        self.broadcast({"type": "peer_hello", "port": self.bind_port})

    async def heartbeat_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            if self.transport:
                body = self.get_heartbeat_body()
                body["type"] = "peer_heartbeat"
                body["port"] = self.bind_port
                self.broadcast(body)
            await asyncio.sleep(MESH_HEARTBEAT_INTERVAL)

    def connection_made(self, transport):
        pass

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        body = mesh_unpack(data, self.password)
        if body is None:
            now = time.monotonic()
            if now - self.last_bad_sig_warning > 30.0:
                log.warning(f"Dropped mesh datagram from {addr} (bad signature or malformed). Wrong password?")
                self.last_bad_sig_warning = now
            return

        sender_uuid = body.get("from")
        if not sender_uuid or sender_uuid == self.own_uuid:
            return

        ts = body.get("ts", 0.0)
        local_ts = time.time()
        if abs(local_ts - ts) > MESH_HMAC_MAX_SKEW:
            now = time.monotonic()
            if now - getattr(self, "last_skew_warning", 0.0) > 30.0:
                log.warning(f"Dropped mesh datagram from {addr} (clock skew > {MESH_HMAC_MAX_SKEW}s). Check system time.")
                self.last_skew_warning = now
            return

        if sender_uuid not in self.peers:
            self.peers[sender_uuid] = PeerInfo(
                uuid=sender_uuid,
                username=body.get("name", "Unknown"),
                ip=addr[0],
                port=body.get("port", addr[1])
            )
        
        p = self.peers[sender_uuid]
        
        # replay guard
        if ts < p.last_ts - 2.0:
            return

        p.ip = addr[0]
        p.port = body.get("port", addr[1])
        p.username = body.get("name", p.username)
        p.last_heard = time.monotonic()
        p.last_ts = ts
        p.mesh_kill_fired = False

        mtype = body.get("type")
        if mtype == "peer_hello":
            # reply with one heartbeat
            reply = self.get_heartbeat_body()
            reply["type"] = "peer_heartbeat"
            reply["port"] = self.bind_port
            self.send(reply, addr)
        elif mtype == "peer_heartbeat":
            p.armed = body.get("armed", False)
            p.game_running = body.get("game", False)
            p.server_ok = body.get("srv", False)
            p.disconnect_kill = body.get("dk", False)
        elif mtype == "peer_kill":
            cause = body.get("cause", "unknown")
            reason = body.get("reason", "unknown")
            eid = body.get("eid")
            self.on_kill(cause, reason, eid, p.username)

    def error_received(self, exc):
        pass
