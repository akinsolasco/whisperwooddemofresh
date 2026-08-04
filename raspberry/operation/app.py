from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps


HOST = os.getenv("WHISPERWOOD_TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.getenv("WHISPERWOOD_TCP_PORT", "5000"))
DATA_DIR = os.getenv("WHISPERWOOD_DATA_DIR", "/opt/whisperwood/data")
SCHEDULE_FILE = os.path.join(DATA_DIR, "lcd_schedule.json")
IMAGE_CACHE_DIR = os.path.join(DATA_DIR, "lcd_images")

LCD_W = 320
LCD_H = 240
LCD_BYTES = LCD_W * LCD_H * 2
ONLINE_TIMEOUT_S = int(os.getenv("WHISPERWOOD_ONLINE_TIMEOUT_S", "30"))
HEARTBEAT_INTERVAL_S = int(os.getenv("WHISPERWOOD_HEARTBEAT_INTERVAL_S", "5"))
ACK_TIMEOUT_S = int(os.getenv("WHISPERWOOD_ACK_TIMEOUT_S", "90"))
IMAGE_RESYNC_COOLDOWN_S = int(os.getenv("WHISPERWOOD_IMAGE_RESYNC_COOLDOWN_S", "60"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

app = FastAPI(title="Whisperwood Operation Manager", version="0.3.5")


def utc_now() -> str:
    return datetime.utcnow().isoformat()


def wall_time() -> str:
    return time.strftime("%H:%M:%S")


def parse_kv_line(line: str) -> Dict[str, str]:
    parts = line.strip().split()
    if not parts:
        return {}
    out = {"_cmd": parts[0]}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


def enc_spaces(value: Any) -> str:
    return str(value or "").replace(" ", "_")


def list_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def join_pipe(value: Any) -> str:
    return "|".join(enc_spaces(item) for item in list_value(value))


def encode_highlights(highlights: List[dict]) -> str:
    parts: List[str] = []
    for highlight in highlights or []:
        htype = str(highlight.get("type", "")).strip().lower()
        section = str(highlight.get("section", "")).strip().upper()
        bg = str(highlight.get("bg", "")).strip().upper()
        fg = str(highlight.get("fg", "")).strip().upper()
        if not section or not bg:
            continue
        if htype == "section":
            parts.append(f"SEC:{section}:BG={bg}:FG={fg}" if fg else f"SEC:{section}:BG={bg}")
            continue
        if htype == "value":
            value = str(highlight.get("value", "")).strip()
            if not value:
                continue
            encoded = enc_spaces(value.upper())
            parts.append(f"VAL:{section}:{encoded}:BG={bg}:FG={fg}" if fg else f"VAL:{section}:{encoded}:BG={bg}")
    return ";".join(parts)


def image_to_rgb565_bytes(file_bytes: bytes, width: int = LCD_W, height: int = LCD_H) -> bytes:
    image = Image.open(io.BytesIO(file_bytes))
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.height > image.width:
        image = image.rotate(-90, expand=True)
    try:
        resample_method = Image.Resampling.LANCZOS
    except AttributeError:
        resample_method = Image.LANCZOS
    image = ImageOps.fit(image, (width, height), method=resample_method, centering=(0.5, 0.5))

    pixels = image.load()
    out = bytearray(width * height * 2)
    idx = 0
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[idx] = rgb565 & 0xFF
            out[idx + 1] = (rgb565 >> 8) & 0xFF
            idx += 2
    return bytes(out)


def parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "ok"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return None


def parse_int(value: Any, minimum: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = int(float(str(value).strip()))
    except Exception:
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def parse_float(value: Any, minimum: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except Exception:
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def safe_device_filename(device_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(device_id))


def cached_image_path(device_id: str) -> str:
    return os.path.join(IMAGE_CACHE_DIR, f"{safe_device_filename(device_id)}.rgb565")


def has_cached_device_image(device_id: str) -> bool:
    path = cached_image_path(device_id)
    try:
        return os.path.exists(path) and os.path.getsize(path) == LCD_BYTES
    except OSError:
        return False


def cache_device_image(device_id: str, rgb565: bytes) -> None:
    if len(rgb565) != LCD_BYTES:
        return
    path = cached_image_path(device_id)
    temp_path = f"{path}.tmp"
    with open(temp_path, "wb") as fh:
        fh.write(rgb565)
    os.replace(temp_path, path)


def load_cached_device_image(device_id: str) -> Optional[bytes]:
    path = cached_image_path(device_id)
    if not os.path.exists(path) or os.path.getsize(path) != LCD_BYTES:
        return None
    with open(path, "rb") as fh:
        return fh.read()


@dataclass
class ConnState:
    sock: socket.socket
    addr: Tuple[str, int]
    device_id: Optional[str] = None
    fw: Optional[str] = None
    last_seen: float = field(default_factory=time.time)
    first_seen: float = field(default_factory=time.time)
    last_status_at: float = 0.0
    disconnected_at: Optional[float] = None
    offline_reason: str = ""
    pending_seq: Optional[int] = None
    pending_img_seq: Optional[int] = None
    pending_lcd_seq: Optional[int] = None
    battery_level: Optional[int] = None
    battery_ok: Optional[bool] = None
    battery_mv: Optional[int] = None
    battery_voltage: Optional[float] = None
    battery_raw_percent: Optional[float] = None
    battery_low: Optional[bool] = None
    battery_alert: Optional[bool] = None
    battery_plugged: Optional[bool] = None
    battery_charging: Optional[bool] = None
    battery_full: Optional[bool] = None
    heap: Optional[int] = None
    rssi: Optional[int] = None
    uptime_ms: Optional[int] = None
    lcd_image_cached: Optional[bool] = None
    epaper_busy: Optional[bool] = None
    last_image_resync_at: float = 0.0
    wifi: str = ""
    ip: str = ""
    closed: bool = False
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    ack_events: Dict[str, threading.Event] = field(default_factory=dict)
    ack_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return not self.closed and (time.time() - self.last_seen) <= ONLINE_TIMEOUT_S


DEVICES: Dict[str, ConnState] = {}
CONNS: List[ConnState] = []
LOCK = threading.RLock()
SEQ = 1
SERVER_STARTED = False
SERVER_SOCKET: Optional[socket.socket] = None
SCHEDULE_STATE: Dict[str, Any] = {}


def next_seq() -> int:
    global SEQ
    with LOCK:
        seq = SEQ
        SEQ += 1
        return seq


def ack_key(kind: str, seq: int) -> str:
    return f"{kind}:{seq}"


def create_ack(st: ConnState, kind: str, seq: int) -> threading.Event:
    event = threading.Event()
    with LOCK:
        st.ack_events[ack_key(kind, seq)] = event
        st.ack_results.pop(ack_key(kind, seq), None)
    return event


def record_ack(st: ConnState, kind: str, seq: int, line: str) -> None:
    key = ack_key(kind, seq)
    kv = parse_kv_line(line)
    ok = str(kv.get("ok", "")).lower() in {"1", "true", "yes", "ok"}
    with LOCK:
        event = st.ack_events.get(key)
        if event:
            st.ack_results[key] = {"ok": ok, "line": line, "values": kv, "time": utc_now()}
            event.set()


def pop_ack_result(st: ConnState, kind: str, seq: int, timed_out: bool = False) -> Dict[str, Any]:
    key = ack_key(kind, seq)
    with LOCK:
        result = st.ack_results.pop(key, None)
        st.ack_events.pop(key, None)
    if result:
        return result
    return {"ok": False, "timeout": timed_out, "line": "", "values": {}, "time": utc_now()}


def send_all(st: ConnState, data: bytes, timeout: float = 20.0) -> None:
    with st.send_lock:
        st.sock.settimeout(timeout)
        total = 0
        while total < len(data):
            sent = st.sock.send(data[total:])
            if sent <= 0:
                raise ConnectionError("socket send returned 0")
            total += sent
        st.sock.settimeout(1.0)


def send_line(st: ConnState, line: str, timeout: float = 8.0) -> None:
    if not line.endswith("\n"):
        line += "\n"
    send_all(st, line.encode("utf-8"), timeout=timeout)


def send_image_frame(st: ConnState, header: str, rgb565: bytes) -> None:
    if not header.endswith("\n"):
        header += "\n"
    with st.send_lock:
        st.sock.settimeout(25.0)
        for chunk in (header.encode("utf-8"), rgb565):
            total = 0
            while total < len(chunk):
                sent = st.sock.send(chunk[total:])
                if sent <= 0:
                    raise ConnectionError("socket send returned 0")
                total += sent
        st.sock.settimeout(1.0)


def close_conn(st: ConnState, reason: str = "") -> None:
    with LOCK:
        if st.closed:
            return
        st.closed = True
        st.disconnected_at = time.time()
        st.offline_reason = reason or "connection closed"
        st.pending_seq = None
        st.pending_img_seq = None
        st.pending_lcd_seq = None
        if st in CONNS:
            CONNS.remove(st)
        for event in st.ack_events.values():
            event.set()
    try:
        st.sock.close()
    except Exception:
        pass
    print(f"[{wall_time()}] - close {st.addr} id={st.device_id} {reason}", flush=True)


def register_device(st: ConnState, dev_id: str, fw: Optional[str]) -> None:
    with LOCK:
        old = DEVICES.get(dev_id)
        if old and old is not st:
            close_conn(old, "replaced")
        st.device_id = dev_id
        st.fw = fw
        st.last_seen = time.time()
        st.first_seen = st.last_seen
        st.closed = False
        st.disconnected_at = None
        st.offline_reason = ""
        st.ip = st.addr[0]
        DEVICES[dev_id] = st
    send_line(st, "OK", timeout=4.0)
    print(f"[{wall_time()}]   registered {dev_id} fw={fw}", flush=True)


def handle_line(st: ConnState, line: str) -> None:
    if not line:
        return
    st.last_seen = time.time()
    print(f"[{wall_time()}] RX {st.addr}: {line}", flush=True)

    if line.startswith("HELLO"):
        kv = parse_kv_line(line)
        dev_id = kv.get("id")
        if dev_id:
            register_device(st, dev_id, kv.get("fw"))
        return

    if line.startswith("ACKIMG"):
        kv = parse_kv_line(line)
        try:
            seq = int(kv.get("seq", "0"))
        except ValueError:
            seq = 0
        if seq:
            st.pending_img_seq = None
            record_ack(st, "image", seq, line)
        return

    if line.startswith("ACKLCD"):
        kv = parse_kv_line(line)
        try:
            seq = int(kv.get("seq", "0"))
        except ValueError:
            seq = 0
        if seq:
            st.pending_lcd_seq = None
            record_ack(st, "lcd", seq, line)
        return

    if line.startswith("ACK"):
        kv = parse_kv_line(line)
        try:
            seq = int(kv.get("seq", "0"))
        except ValueError:
            seq = 0
        if seq:
            st.pending_seq = None
            record_ack(st, "text", seq, line)
        return

    if line.startswith("STATUS"):
        kv = parse_kv_line(line)
        st.battery_level = parse_int(kv.get("battery"), minimum=0)
        st.battery_ok = parse_bool(kv.get("battery_ok"))
        st.battery_mv = parse_int(kv.get("battery_mv"), minimum=0)
        st.battery_voltage = round(st.battery_mv / 1000.0, 3) if st.battery_mv is not None else parse_float(kv.get("battery_voltage"), minimum=0)
        raw_x10 = parse_int(kv.get("battery_raw_x10"), minimum=0)
        st.battery_raw_percent = round(raw_x10 / 10.0, 1) if raw_x10 is not None else parse_float(kv.get("battery_raw"), minimum=0)
        st.battery_low = parse_bool(kv.get("battery_low"))
        st.battery_alert = parse_bool(kv.get("battery_alert"))
        st.battery_plugged = parse_bool(kv.get("battery_plugged"))
        st.battery_charging = parse_bool(kv.get("battery_charging"))
        st.battery_full = parse_bool(kv.get("battery_full"))
        st.heap = parse_int(kv.get("heap"), minimum=0)
        st.rssi = parse_int(kv.get("rssi"))
        st.uptime_ms = parse_int(kv.get("uptime_ms"), minimum=0)
        lcd_image = parse_bool(kv.get("lcd_image"))
        if lcd_image is not None:
            st.lcd_image_cached = lcd_image
            if not lcd_image:
                queue_cached_image_resync(st, "esp reported no lcd image")
        st.epaper_busy = parse_bool(kv.get("epaper_busy"))
        st.last_status_at = time.time()
        st.wifi = kv.get("wifi", "")
        st.ip = kv.get("ip", st.ip or st.addr[0])
        return

    if line == "PONG":
        return


def client_loop(st: ConnState) -> None:
    buffer = b""
    try:
        st.sock.settimeout(1.0)
        while not st.closed:
            try:
                data = st.sock.recv(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                close_conn(st, "reset")
                return
            except Exception as exc:
                close_conn(st, f"recv error {exc}")
                return
            if not data:
                close_conn(st, "eof")
                return
            buffer += data
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode(errors="ignore").strip()
                if line:
                    handle_line(st, line)
    finally:
        close_conn(st, "client loop ended")


def tcp_server_loop() -> None:
    global SERVER_SOCKET
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    SERVER_SOCKET = server
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, TCP_PORT))
    server.listen(50)
    server.settimeout(1.0)
    print(f"[{wall_time()}] TCP listening on {HOST}:{TCP_PORT}", flush=True)
    while True:
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        conn.settimeout(1.0)
        st = ConnState(sock=conn, addr=addr, ip=addr[0])
        with LOCK:
            CONNS.append(st)
        print(f"[{wall_time()}] + conn {addr}", flush=True)
        threading.Thread(target=client_loop, args=(st,), daemon=True).start()


def heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_INTERVAL_S)
        with LOCK:
            states = list(DEVICES.values())
        for st in states:
            if st.closed:
                continue
            if time.time() - st.last_seen > ONLINE_TIMEOUT_S:
                close_conn(st, "heartbeat timeout")
                continue
            try:
                send_line(st, "PING", timeout=3.0)
            except Exception as exc:
                close_conn(st, f"ping failed {exc}")


def load_schedule_state() -> Dict[str, Any]:
    if not os.path.exists(SCHEDULE_FILE):
        return {}
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_schedule_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    global SCHEDULE_STATE
    state = {
        "enabled": bool(payload.get("enabled")),
        "lcd_on_time": payload.get("lcd_on_time") or payload.get("on_time") or "07:00",
        "lcd_off_time": payload.get("lcd_off_time") or payload.get("off_time") or "20:00",
        "sleep_if_no_image": bool(payload.get("sleep_if_no_image", True)),
        "device_id": payload.get("device_id") or payload.get("id") or "all",
        "resident_id": payload.get("resident_id"),
        "updated_at": utc_now(),
    }
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    SCHEDULE_STATE = state
    return state


def parse_schedule_time(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower().replace(".", "")
    if not raw:
        return None
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


def schedule_loop() -> None:
    last_fire = ""
    while True:
        time.sleep(20)
        state = SCHEDULE_STATE or load_schedule_state()
        if not state.get("enabled"):
            continue
        now_hm = datetime.now().strftime("%H:%M")
        date_key = datetime.now().strftime("%Y-%m-%d")
        command = None
        if parse_schedule_time(state.get("lcd_on_time")) == now_hm:
            command = "on"
        elif parse_schedule_time(state.get("lcd_off_time")) == now_hm:
            command = "off"
        if not command:
            continue
        fire_key = f"{date_key}:{now_hm}:{command}"
        if fire_key == last_fire:
            continue
        last_fire = fire_key
        try:
            send_lcd_to_target(state.get("device_id") or "all", command)
        except Exception as exc:
            print(f"[{wall_time()}] schedule LCD {command} failed: {exc}", flush=True)


def start_background_threads() -> None:
    global SERVER_STARTED, SCHEDULE_STATE
    if SERVER_STARTED:
        return
    SERVER_STARTED = True
    SCHEDULE_STATE = load_schedule_state()
    threading.Thread(target=tcp_server_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()


@app.on_event("startup")
def startup_event() -> None:
    start_background_threads()


def get_device_state(device_id: str) -> ConnState:
    with LOCK:
        st = DEVICES.get(device_id)
    if not st or not st.online:
        raise HTTPException(status_code=404, detail="device not connected")
    return st


def device_to_json(st: ConnState) -> Dict[str, Any]:
    age = max(0, int(time.time() - st.last_seen))
    online = st.online
    return {
        "id": st.device_id,
        "device_id": st.device_id,
        "ip": st.ip or st.addr[0],
        "port": st.addr[1],
        "fw": st.fw,
        "firmware": st.fw,
        "pending_seq": st.pending_seq,
        "pending_img_seq": st.pending_img_seq,
        "pending_lcd_seq": st.pending_lcd_seq,
        "last_seen_s": age,
        "last_seen": age,
        "last_seen_at": datetime.utcfromtimestamp(st.last_seen).isoformat(),
        "first_seen_at": datetime.utcfromtimestamp(st.first_seen).isoformat(),
        "last_status_at": datetime.utcfromtimestamp(st.last_status_at).isoformat() if st.last_status_at else "",
        "disconnected_at": datetime.utcfromtimestamp(st.disconnected_at).isoformat() if st.disconnected_at else "",
        "is_online": online,
        "online": online,
        "status": "online" if online else "offline",
        "connection_state": "online" if online else "offline",
        "offline_reason": "" if online else (st.offline_reason or "stale"),
        "battery_level": st.battery_level,
        "battery": st.battery_level,
        "battery_ok": st.battery_ok,
        "battery_mv": st.battery_mv,
        "battery_voltage": st.battery_voltage,
        "battery_raw_percent": st.battery_raw_percent,
        "battery_low": st.battery_low,
        "battery_alert": st.battery_alert,
        "battery_plugged": st.battery_plugged,
        "battery_charging": st.battery_charging,
        "battery_full": st.battery_full,
        "heap": st.heap,
        "rssi": st.rssi,
        "uptime_ms": st.uptime_ms,
        "lcd_image_cached": st.lcd_image_cached,
        "epaper_busy": bool(st.epaper_busy or st.pending_seq is not None),
        "pi_cached_image": bool(st.device_id and has_cached_device_image(st.device_id)),
        "wifi": st.wifi,
        "reported_ip": st.ip,
    }


def build_update_line(seq: int, body: Dict[str, Any]) -> str:
    fields = [f"UPDATE seq={seq}"]
    if body.get("name") is not None:
        fields.append(f"name={enc_spaces(body.get('name'))}")
    if body.get("room") is not None:
        fields.append(f"room={enc_spaces(body.get('room'))}")
    if body.get("diet") is not None:
        fields.append(f"diet={join_pipe(body.get('diet'))}")

    texture = body.get("texture")
    if texture is None:
        texture = body.get("allergies")
    if texture is not None:
        texture_wire = join_pipe(texture)
        fields.append(f"texture={texture_wire}")
        fields.append(f"allergies={texture_wire}")

    fluids = body.get("fluids")
    if fluids is None:
        fluids = body.get("schedule")
    if fluids is not None:
        fluids_wire = join_pipe(fluids)
        fields.append(f"fluids={fluids_wire}")
        fields.append(f"schedule={fluids_wire}")

    if body.get("note") is not None:
        fields.append(f"note={enc_spaces(body.get('note'))}")
    if body.get("drinks") is not None:
        fields.append(f"drinks={enc_spaces(body.get('drinks'))}")

    highlights = body.get("highlights", [])
    if isinstance(highlights, list) and highlights:
        encoded = encode_highlights(highlights)
        if encoded:
            fields.append(f"hl={encoded}")
    return " ".join(fields)


def send_text_to_device(body: Dict[str, Any]) -> Dict[str, Any]:
    dev_id = body.get("id") or body.get("device_id")
    if not dev_id:
        raise HTTPException(status_code=400, detail="missing id")
    st = get_device_state(str(dev_id))
    if st.pending_seq is not None:
        raise HTTPException(status_code=409, detail=f"device busy: pending_seq={st.pending_seq}")
    if st.pending_img_seq is not None:
        raise HTTPException(status_code=409, detail=f"LCD photo is updating; wait before sending e-paper text. pending_img_seq={st.pending_img_seq}")
    seq = next_seq()
    line = build_update_line(seq, body)
    event = create_ack(st, "text", seq)
    try:
        st.pending_seq = seq
        send_line(st, line, timeout=8.0)
        print(f"[{wall_time()}] TX -> {dev_id}: {line}", flush=True)
    except Exception as exc:
        st.pending_seq = None
        close_conn(st, f"send text failed {exc}")
        raise HTTPException(status_code=500, detail=f"send failed: {exc}") from exc

    acked = event.wait(ACK_TIMEOUT_S)
    result = pop_ack_result(st, "text", seq, timed_out=not acked)
    with LOCK:
        if st.pending_seq == seq:
            st.pending_seq = None
    if not result.get("ok"):
        raise HTTPException(status_code=504 if result.get("timeout") else 502, detail=result)
    return {"ok": True, "seq": seq, "ack": result, "line": line}


def send_rgb565_to_device(device_id: str, rgb565: bytes, cache_after_ack: bool = True) -> Dict[str, Any]:
    if not device_id:
        raise HTTPException(status_code=400, detail="missing id")
    st = get_device_state(device_id)
    if st.pending_seq is not None:
        raise HTTPException(status_code=409, detail=f"E-paper is updating; wait before sending LCD photo. pending_seq={st.pending_seq}")
    if st.epaper_busy:
        raise HTTPException(status_code=409, detail="E-paper is updating; wait before sending LCD photo.")
    if st.pending_img_seq is not None:
        raise HTTPException(status_code=409, detail=f"image channel busy: pending_img_seq={st.pending_img_seq}")
    if len(rgb565) != LCD_BYTES:
        raise HTTPException(status_code=400, detail=f"bad image size: {len(rgb565)}")

    seq = next_seq()
    header = f"IMAGE seq={seq} size={len(rgb565)}"
    event = create_ack(st, "image", seq)
    try:
        st.pending_img_seq = seq
        send_image_frame(st, header, rgb565)
        print(f"[{wall_time()}] TXIMG -> {device_id}: {header}", flush=True)
    except Exception as exc:
        st.pending_img_seq = None
        close_conn(st, f"send image failed {exc}")
        raise HTTPException(status_code=500, detail=f"send image failed: {exc}") from exc

    acked = event.wait(ACK_TIMEOUT_S)
    result = pop_ack_result(st, "image", seq, timed_out=not acked)
    with LOCK:
        if st.pending_img_seq == seq:
            st.pending_img_seq = None
    if not result.get("ok"):
        raise HTTPException(status_code=504 if result.get("timeout") else 502, detail=result)
    if cache_after_ack:
        try:
            cache_device_image(device_id, rgb565)
        except Exception as exc:
            print(f"[{wall_time()}] cache image failed for {device_id}: {exc}", flush=True)
    st.lcd_image_cached = True
    return {
        "ok": True,
        "seq": seq,
        "ack": result,
        "size": len(rgb565),
        "width": LCD_W,
        "height": LCD_H,
        "format": "RGB565_LE",
    }


def send_image_to_device(device_id: str, raw_file: bytes) -> Dict[str, Any]:
    try:
        rgb565 = image_to_rgb565_bytes(raw_file, LCD_W, LCD_H)
        if len(rgb565) != LCD_BYTES:
            raise ValueError(f"bad converted size: {len(rgb565)}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"image convert failed: {exc}") from exc
    return send_rgb565_to_device(device_id, rgb565, cache_after_ack=True)


def send_cached_image_to_device(device_id: str) -> Dict[str, Any]:
    rgb565 = load_cached_device_image(device_id)
    if not rgb565:
        raise HTTPException(status_code=404, detail="No cached LCD image for device")
    return send_rgb565_to_device(device_id, rgb565, cache_after_ack=False)


def firmware_major(st: ConnState) -> Optional[int]:
    raw = str(st.fw or "").strip()
    if not raw:
        return None
    head = raw.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def queue_cached_image_resync(st: ConnState, reason: str) -> None:
    device_id = st.device_id or ""
    if not device_id or not has_cached_device_image(device_id):
        return
    now_ts = time.time()
    if now_ts - st.last_image_resync_at < IMAGE_RESYNC_COOLDOWN_S:
        return
    if st.pending_img_seq is not None or not st.online:
        return
    st.last_image_resync_at = now_ts

    def worker() -> None:
        try:
            # Let any just-arrived status/HELLO traffic settle before sending a large frame.
            time.sleep(1.0)
            send_cached_image_to_device(device_id)
            print(f"[{wall_time()}] cached LCD image resent to {device_id}: {reason}", flush=True)
        except Exception as exc:
            print(f"[{wall_time()}] cached LCD image resync skipped for {device_id}: {exc}", flush=True)

    threading.Thread(target=worker, daemon=True).start()


def send_lcd_to_device(device_id: str, command: str) -> Dict[str, Any]:
    st = get_device_state(device_id)
    seq = next_seq()
    command = str(command or "").strip().lower()
    if command not in {"on", "off"}:
        raise HTTPException(status_code=400, detail="command must be on or off")
    line = f"LCD seq={seq} cmd={enc_spaces(command)}"
    event = create_ack(st, "lcd", seq)
    try:
        st.pending_lcd_seq = seq
        send_line(st, line, timeout=8.0)
        print(f"[{wall_time()}] TXLCD -> {device_id}: {line}", flush=True)
    except Exception as exc:
        st.pending_lcd_seq = None
        close_conn(st, f"lcd send failed {exc}")
        raise HTTPException(status_code=500, detail=f"lcd send failed: {exc}") from exc
    acked = event.wait(ACK_TIMEOUT_S)
    result = pop_ack_result(st, "lcd", seq, timed_out=not acked)
    with LOCK:
        if st.pending_lcd_seq == seq:
            st.pending_lcd_seq = None
    if not result.get("ok"):
        raise HTTPException(status_code=504 if result.get("timeout") else 502, detail=result)
    return {"ok": True, "seq": seq, "ack": result, "line": line}


def send_lcd_to_target(device_id: str, command: str) -> Dict[str, Any]:
    target = str(device_id or "all")
    if target.lower() == "all":
        with LOCK:
            ids = [dev_id for dev_id, st in DEVICES.items() if st.online]
        results = []
        errors = []
        for dev_id in ids:
            try:
                results.append({"device_id": dev_id, **send_lcd_to_device(dev_id, command)})
            except HTTPException as exc:
                errors.append({"device_id": dev_id, "detail": exc.detail, "status_code": exc.status_code})
        return {"ok": not errors, "target": "all", "sent": results, "errors": errors}
    return {"ok": True, "target": target, "sent": [send_lcd_to_device(target, command)], "errors": []}


@app.get("/health")
def health() -> Dict[str, Any]:
    start_background_threads()
    with LOCK:
        connected = sum(1 for st in DEVICES.values() if st.online)
    return {
        "ok": True,
        "service": "operation",
        "version": "0.3.5",
        "time": utc_now(),
        "tcp_host": HOST,
        "tcp_port": TCP_PORT,
        "connected_devices": connected,
    }


@app.get("/devices")
def devices() -> Dict[str, Any]:
    start_background_threads()
    with LOCK:
        rows = [device_to_json(st) for st in DEVICES.values()]
    rows.sort(key=lambda row: str(row.get("device_id") or ""))
    return {"ok": True, "devices": rows}


@app.post("/send")
def send(body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    return send_text_to_device(body or {})


@app.post("/send_image")
async def send_image(id: str = Form(default=""), image: UploadFile = File(...)) -> Dict[str, Any]:
    raw = await image.read()
    return send_image_to_device(id.strip(), raw)


@app.post("/lcd")
def lcd(body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    device_id = body.get("id") or body.get("device_id") or "all"
    command = body.get("command") or body.get("cmd")
    return send_lcd_to_target(str(device_id), str(command or ""))


@app.post("/schedule")
def schedule(body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    state = save_schedule_state(body or {})
    return {"ok": True, "schedule": state, "message": "Global LCD schedule saved in Operation Manager"}


@app.get("/schedules")
def schedules() -> Dict[str, Any]:
    state = SCHEDULE_STATE or load_schedule_state()
    return {"ok": True, "schedules": [state] if state else []}


@app.post("/resident_display")
def resident_display(body: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    payload = dict(body or {})
    device_id = payload.get("id") or payload.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="missing device_id")
    payload["id"] = device_id

    image_result: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "No resident photo available"}
    image_path = str(payload.get("image_path") or "").strip()
    if image_path and os.path.exists(image_path):
        st = get_device_state(str(device_id))
        fw = firmware_major(st)
        if fw is not None and fw < 7:
            image_result = {
                "ok": True,
                "skipped": True,
                "reason": "LCD photo transfer skipped until ESP32 firmware 7 is installed.",
                "firmware": st.fw,
            }
        else:
            try:
                with open(image_path, "rb") as fh:
                    image_result = send_image_to_device(str(device_id), fh.read())
            except HTTPException as exc:
                image_result = {
                    "ok": False,
                    "skipped": False,
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                    "message": "LCD photo could not be sent; e-paper text will be attempted next.",
                }
            except Exception as exc:
                image_result = {
                    "ok": False,
                    "skipped": False,
                    "detail": str(exc),
                    "message": "LCD photo could not be sent; e-paper text will be attempted next.",
                }

    text_result: Dict[str, Any]
    try:
        text_result = send_text_to_device(payload)
    except HTTPException as exc:
        return {
            "ok": False,
            "partial": bool(image_result.get("ok", False)),
            "device_id": device_id,
            "text": {
                "ok": False,
                "status_code": exc.status_code,
                "detail": exc.detail,
            },
            "image": image_result,
            "message": "Resident photo step finished, but the e-paper text could not be sent.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "partial": bool(image_result.get("ok", False)),
            "device_id": device_id,
            "text": {
                "ok": False,
                "detail": str(exc),
            },
            "image": image_result,
            "message": "Resident photo step finished, but the e-paper text could not be sent.",
        }

    if image_result.get("skipped"):
        detail = image_result.get("reason") or "Resident photo was skipped."
        display_message = f"{detail}; resident text sent after the photo step."
    elif not bool(image_result.get("ok", False)):
        display_message = "Resident photo could not be sent; resident text was sent after the photo attempt."
    else:
        display_message = "Resident photo sent first; resident text sent after photo ACK."

    return {
        "ok": True,
        "partial": not bool(image_result.get("ok", False)),
        "device_id": device_id,
        "text": text_result,
        "image": image_result,
        "message": display_message,
    }
