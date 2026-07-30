#!/usr/bin/env python3
import io
import json
import os
import selectors
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional, Tuple, List

from flask import Flask, request, jsonify
from PIL import Image, ImageOps

HOST = "0.0.0.0"
TCP_PORT = 5000
HTTP_PORT = 8080

LCD_W = 320
LCD_H = 240
LCD_BYTES = LCD_W * LCD_H * 2

SCHEDULES_FILE = os.environ.get("EPD_SCHEDULES_FILE", "epd_schedules.json")

sel = selectors.DefaultSelector()
app = Flask(__name__)


def now():
    return time.strftime("%H:%M:%S")


def parse_kv_line(line: str):
    parts = line.strip().split()
    if not parts:
        return {}
    out = {"_cmd": parts[0]}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
    return out


def enc_spaces(s: str) -> str:
    return s.replace(" ", "_")


def normalize_list(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def join_pipe(items):
    return "|".join(enc_spaces(str(x).strip()) for x in items if str(x).strip())


def encode_highlights(highlights: List[dict]) -> str:
    parts = []
    for h in highlights:
        htype = str(h.get("type", "")).strip().lower()
        section = str(h.get("section", "")).strip().upper()
        bg = str(h.get("bg", "")).strip().upper()
        fg = str(h.get("fg", "")).strip().upper()

        if not section or not bg:
            continue

        if htype == "section":
            if fg:
                parts.append(f"SEC:{section}:BG={bg}:FG={fg}")
            else:
                parts.append(f"SEC:{section}:BG={bg}")
        elif htype == "value":
            value = str(h.get("value", "")).strip()
            if not value:
                continue
            value_enc = enc_spaces(value.upper())
            if fg:
                parts.append(f"VAL:{section}:{value_enc}:BG={bg}:FG={fg}")
            else:
                parts.append(f"VAL:{section}:{value_enc}:BG={bg}")

    return ";".join(parts)


def image_to_rgb565_bytes(file_bytes: bytes, width: int = LCD_W, height: int = LCD_H) -> bytes:
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    try:
        resample_method = Image.Resampling.LANCZOS
    except AttributeError:
        resample_method = Image.LANCZOS

    img = ImageOps.fit(img, (width, height), method=resample_method)

    pixels = img.load()
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


@dataclass
class ConnState:
    sock: socket.socket
    addr: Tuple[str, int]
    buf: bytes = b""
    device_id: Optional[str] = None
    fw: Optional[str] = None
    battery_level: Optional[int] = None
    last_seen: float = field(default_factory=time.time)
    pending_seq: Optional[int] = None
    pending_sent_at: float = 0.0
    pending_img_seq: Optional[int] = None
    pending_img_sent_at: float = 0.0
    pending_lcd_seq: Optional[int] = None
    pending_lcd_sent_at: float = 0.0
    send_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class ScheduleState:
    resident_uid: str
    resident_id: Optional[int] = None
    device_id: Optional[str] = None
    enabled: bool = False
    lcd_on_time: str = "07:00"
    lcd_off_time: str = "20:00"
    sleep_if_no_image: bool = False
    has_image: bool = False
    last_on_date: Optional[str] = None
    last_off_date: Optional[str] = None
    last_on_attempt: Optional[str] = None
    last_off_attempt: Optional[str] = None
    last_forced_off_minute: Optional[str] = None
    updated_at: float = field(default_factory=time.time)


DEVICES: Dict[str, ConnState] = {}
CONNS: Dict[Tuple[str, int], ConnState] = {}
SCHEDULES: Dict[str, ScheduleState] = {}
SEQ = 1
LOCK = threading.RLock()


def next_seq():
    global SEQ
    with LOCK:
        s = SEQ
        SEQ += 1
        return s


def _schedule_key(payload: dict) -> str:
    resident_uid = str(payload.get("resident_uid") or "").strip()
    resident_id = payload.get("resident_id")
    if resident_uid:
        return resident_uid
    if resident_id is not None:
        return f"resident:{resident_id}"
    device_id = str(payload.get("device_id") or "").strip()
    if device_id:
        return f"device:{device_id}"
    return "unknown"


def _valid_hhmm(value: str) -> bool:
    try:
        time.strptime(value, "%H:%M")
        return True
    except Exception:
        return False


def save_schedules():
    with LOCK:
        rows = {k: asdict(v) for k, v in SCHEDULES.items()}
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def load_schedules():
    if not os.path.exists(SCHEDULES_FILE):
        return
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        print(f"[{now()}] failed loading schedules: {e}")
        return

    with LOCK:
        for key, data in rows.items():
            try:
                SCHEDULES[key] = ScheduleState(**data)
            except Exception:
                continue
    print(f"[{now()}] loaded {len(SCHEDULES)} schedules")


def accept(sock):
    conn, addr = sock.accept()
    conn.setblocking(False)
    st = ConnState(sock=conn, addr=addr)
    with LOCK:
        CONNS[addr] = st
    sel.register(conn, selectors.EVENT_READ, data=st)
    print(f"[{now()}] + conn {addr}")


def close_conn(st, reason=""):
    with LOCK:
        try:
            sel.unregister(st.sock)
        except Exception:
            pass
        try:
            st.sock.close()
        except Exception:
            pass

        CONNS.pop(st.addr, None)
        if st.device_id and DEVICES.get(st.device_id) is st:
            DEVICES.pop(st.device_id, None)

    print(f"[{now()}] - close {st.addr} id={st.device_id} {reason}")


def send_all_blocking(st, data: bytes, timeout: float = 10.0):
    with st.send_lock:
        sock = st.sock
        old_timeout = sock.gettimeout()
        try:
            sock.setblocking(True)
            sock.settimeout(timeout)
            total = 0
            while total < len(data):
                sent = sock.send(data[total:])
                if sent <= 0:
                    raise ConnectionError("socket send returned 0")
                total += sent
        finally:
            sock.settimeout(old_timeout)
            sock.setblocking(False)


def send_line(st, line: str):
    if not line.endswith("\n"):
        line += "\n"
    send_all_blocking(st, line.encode(), timeout=5.0)


def send_bytes(st, data: bytes):
    send_all_blocking(st, data, timeout=20.0)


def handle_line(st, line: str):
    st.last_seen = time.time()
    if not line:
        return

    print(f"[{now()}] RX {st.addr}: {line}")

    if line.startswith("HELLO"):
        kv = parse_kv_line(line)
        dev_id = kv.get("id")
        fw = kv.get("fw")

        if dev_id:
            with LOCK:
                old = DEVICES.get(dev_id)
                if old and old is not st:
                    close_conn(old, "replaced")

                st.device_id = dev_id
                st.fw = fw
                DEVICES[dev_id] = st

            print(f"[{now()}]   registered {dev_id} fw={fw}")
            send_line(st, "OK")
        return

    if line.startswith("STATUS"):
        kv = parse_kv_line(line)
        bat = kv.get("battery")
        if bat is not None:
            try:
                st.battery_level = int(bat)
            except Exception:
                pass
        return

    if line.startswith("ACKIMG"):
        kv = parse_kv_line(line)
        try:
            ack_seq = int(kv.get("seq", "0"))
        except Exception:
            ack_seq = 0
        if ack_seq and st.pending_img_seq == ack_seq:
            print(f"[{now()}]   ACKIMG ok id={st.device_id} seq={st.pending_img_seq}")
            st.pending_img_seq = None
        return

    if line.startswith("ACKLCD"):
        kv = parse_kv_line(line)
        try:
            ack_seq = int(kv.get("seq", "0"))
        except Exception:
            ack_seq = 0
        if ack_seq and st.pending_lcd_seq == ack_seq:
            print(f"[{now()}]   ACKLCD ok id={st.device_id} seq={st.pending_lcd_seq}")
            st.pending_lcd_seq = None
        return

    if line.startswith("ACK"):
        kv = parse_kv_line(line)
        try:
            ack_seq = int(kv.get("seq", "0"))
        except Exception:
            ack_seq = 0
        if ack_seq and st.pending_seq == ack_seq:
            print(f"[{now()}]   ACK ok id={st.device_id} seq={st.pending_seq}")
            st.pending_seq = None
        return

    if line == "PONG":
        return


def service_read(st):
    try:
        data = st.sock.recv(4096)
    except ConnectionResetError:
        close_conn(st, "reset")
        return
    except Exception as e:
        close_conn(st, f"recv error {e}")
        return

    if not data:
        close_conn(st, "eof")
        return

    st.buf += data
    while b"\n" in st.buf:
        raw, st.buf = st.buf.split(b"\n", 1)
        line = raw.decode(errors="ignore").strip()
        if line:
            handle_line(st, line)


def tcp_loop():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, TCP_PORT))
    server.listen(50)
    server.setblocking(False)
    sel.register(server, selectors.EVENT_READ, data="ACCEPT")
    print(f"[{now()}] TCP listening on {HOST}:{TCP_PORT}")

    while True:
        events = sel.select(timeout=0.2)
        for key, _ in events:
            if key.data == "ACCEPT":
                accept(key.fileobj)
            else:
                service_read(key.data)


def dispatch_lcd_command(device_id: str, command: str):
    command = (command or "").strip().lower()
    if command not in {"on", "off"}:
        return False, 400, {"ok": False, "err": "invalid command"}

    with LOCK:
        st = DEVICES.get(device_id)
        if not st:
            return False, 404, {"ok": False, "err": "device not connected"}
        if st.pending_lcd_seq is not None:
            return False, 409, {"ok": False, "err": "lcd channel busy", "pending_lcd_seq": st.pending_lcd_seq}
        seq = next_seq()
        st.pending_lcd_seq = seq
        st.pending_lcd_sent_at = time.time()

    line = f"LCD seq={seq} cmd={command}"
    try:
        send_line(st, line)
        print(f"[{now()}] TXLCD -> {device_id}: {line}")
        return True, 200, {"ok": True, "seq": seq, "command": command}
    except Exception as e:
        with LOCK:
            if st.pending_lcd_seq == seq:
                st.pending_lcd_seq = None
        print(f"[{now()}] send lcd exception: {repr(e)}")
        close_conn(st, f"send lcd failed {e}")
        return False, 500, {"ok": False, "err": f"send failed: {e}"}


def clear_stale_pending():
    now_ts = time.time()
    with LOCK:
        states = list(DEVICES.values())

    for st in states:
        if st.pending_seq is not None and (now_ts - st.pending_sent_at) > 25:
            print(f"[{now()}] stale text pending cleared id={st.device_id} seq={st.pending_seq}")
            st.pending_seq = None
        if st.pending_img_seq is not None and (now_ts - st.pending_img_sent_at) > 45:
            print(f"[{now()}] stale image pending cleared id={st.device_id} seq={st.pending_img_seq}")
            st.pending_img_seq = None
        if st.pending_lcd_seq is not None and (now_ts - st.pending_lcd_sent_at) > 25:
            print(f"[{now()}] stale lcd pending cleared id={st.device_id} seq={st.pending_lcd_seq}")
            st.pending_lcd_seq = None


def schedule_loop():
    while True:
        clear_stale_pending()

        today = time.strftime("%Y-%m-%d")
        hhmm = time.strftime("%H:%M")
        minute_key = f"{today} {hhmm}"
        changed = False

        with LOCK:
            rows = list(SCHEDULES.items())

        for key, sch in rows:
            if not sch.device_id or not sch.enabled:
                continue

            if sch.sleep_if_no_image and not sch.has_image:
                if sch.last_forced_off_minute != minute_key:
                    ok, _status, _payload = dispatch_lcd_command(sch.device_id, "off")
                    sch.last_forced_off_minute = minute_key
                    if ok:
                        sch.last_off_date = today
                    changed = True
                continue

            on_attempt_key = f"{minute_key}:on"
            off_attempt_key = f"{minute_key}:off"

            if sch.lcd_on_time == hhmm and sch.last_on_attempt != on_attempt_key:
                ok, _status, _payload = dispatch_lcd_command(sch.device_id, "on")
                sch.last_on_attempt = on_attempt_key
                if ok:
                    sch.last_on_date = today
                changed = True

            if sch.lcd_off_time == hhmm and sch.last_off_attempt != off_attempt_key:
                ok, _status, _payload = dispatch_lcd_command(sch.device_id, "off")
                sch.last_off_attempt = off_attempt_key
                if ok:
                    sch.last_off_date = today
                changed = True

            if changed:
                with LOCK:
                    SCHEDULES[key] = sch

        if changed:
            try:
                save_schedules()
            except Exception as e:
                print(f"[{now()}] save schedules failed: {e}")

        time.sleep(10)


@app.route("/devices", methods=["GET"])
def devices():
    with LOCK:
        rows = list(DEVICES.items())

    out = []
    for dev_id, st in rows:
        out.append({
            "id": dev_id,
            "ip": st.addr[0],
            "port": st.addr[1],
            "fw": st.fw,
            "pending_seq": st.pending_seq,
            "pending_img_seq": st.pending_img_seq,
            "pending_lcd_seq": st.pending_lcd_seq,
            "last_seen_s": int(time.time() - st.last_seen),
            "battery_level": st.battery_level,
        })
    return jsonify(out)


@app.route("/send", methods=["POST"])
def send():
    body = request.get_json(force=True, silent=True) or {}
    dev_id = body.get("id")
    if not dev_id:
        return jsonify({"ok": False, "err": "missing id"}), 400

    with LOCK:
        st = DEVICES.get(dev_id)
        if not st:
            return jsonify({"ok": False, "err": "device not connected"}), 404
        if st.pending_seq is not None:
            return jsonify({"ok": False, "err": "device busy", "pending_seq": st.pending_seq}), 409
        seq = next_seq()
        st.pending_seq = seq
        st.pending_sent_at = time.time()

    fields = [f"UPDATE seq={seq}"]
    if "name" in body:
        fields.append(f"name={enc_spaces(str(body['name']))}")
    if "room" in body:
        fields.append(f"room={enc_spaces(str(body['room']))}")

    diet_list = normalize_list(body.get("diet"))
    if diet_list:
        fields.append(f"diet={join_pipe(diet_list)}")

    allergy_list = normalize_list(body.get("allergies"))
    if allergy_list:
        fields.append(f"allergies={join_pipe(allergy_list)}")

    if "note" in body:
        fields.append(f"note={enc_spaces(str(body['note']))}")
    if "drinks" in body:
        fields.append(f"drinks={enc_spaces(str(body['drinks']))}")
    if "schedule" in body:
        fields.append(f"schedule={enc_spaces(str(body['schedule']))}")

    highlights = body.get("highlights", [])
    if isinstance(highlights, list) and highlights:
        hl_wire = encode_highlights(highlights)
        if hl_wire:
            fields.append(f"hl={hl_wire}")

    line = " ".join(fields)

    try:
        send_line(st, line)
        print(f"[{now()}] TX -> {dev_id}: {line}")
        return jsonify({"ok": True, "seq": seq})
    except Exception as e:
        with LOCK:
            if st.pending_seq == seq:
                st.pending_seq = None
        print(f"[{now()}] send text exception: {repr(e)}")
        close_conn(st, f"send failed {e}")
        return jsonify({"ok": False, "err": f"send failed: {e}"}), 500


@app.route("/send_image", methods=["POST"])
def send_image():
    dev_id = request.form.get("id", "").strip()
    if not dev_id:
        return jsonify({"ok": False, "err": "missing id"}), 400

    with LOCK:
        st = DEVICES.get(dev_id)
        if not st:
            return jsonify({"ok": False, "err": "device not connected"}), 404
        if st.pending_img_seq is not None:
            return jsonify({"ok": False, "err": "image channel busy", "pending_img_seq": st.pending_img_seq}), 409

    f = request.files.get("image")
    if not f:
        return jsonify({"ok": False, "err": "missing image file"}), 400

    try:
        raw_file = f.read()
        rgb565 = image_to_rgb565_bytes(raw_file, LCD_W, LCD_H)
        if len(rgb565) != LCD_BYTES:
            return jsonify({"ok": False, "err": f"bad converted size: {len(rgb565)}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "err": f"image convert failed: {e}"}), 400

    seq = next_seq()
    header = f"IMAGE seq={seq} size={len(rgb565)}"

    with LOCK:
        st.pending_img_seq = seq
        st.pending_img_sent_at = time.time()

    try:
        send_line(st, header)
        send_bytes(st, rgb565)
        print(f"[{now()}] TXIMG -> {dev_id}: {header}")
        return jsonify({
            "ok": True,
            "seq": seq,
            "size": len(rgb565),
            "width": LCD_W,
            "height": LCD_H,
            "format": "RGB565_LE"
        })
    except Exception as e:
        with LOCK:
            if st.pending_img_seq == seq:
                st.pending_img_seq = None
        print(f"[{now()}] send_image exception: {repr(e)}")
        close_conn(st, f"send image failed {e}")
        return jsonify({"ok": False, "err": f"send image failed: {e}"}), 500


@app.route("/lcd", methods=["POST"])
def lcd():
    body = request.get_json(force=True, silent=True) or {}
    dev_id = body.get("id") or body.get("device_id")
    if not dev_id:
        return jsonify({"ok": False, "err": "missing id"}), 400

    command = body.get("command")
    ok, status, payload = dispatch_lcd_command(str(dev_id), str(command or ""))
    return jsonify(payload), status


@app.route("/schedule", methods=["POST"])
def schedule():
    body = request.get_json(force=True, silent=True) or {}

    key = _schedule_key(body)
    enabled = bool(body.get("enabled", False))
    on_time = str(body.get("lcd_on_time") or "07:00")
    off_time = str(body.get("lcd_off_time") or "20:00")

    if enabled and (not _valid_hhmm(on_time) or not _valid_hhmm(off_time)):
        return jsonify({"ok": False, "err": "invalid time format, expected HH:MM"}), 400

    with LOCK:
        current = SCHEDULES.get(key) or ScheduleState(resident_uid=str(body.get("resident_uid") or key))
        current.resident_uid = str(body.get("resident_uid") or current.resident_uid or key)
        current.resident_id = body.get("resident_id")
        current.device_id = body.get("device_id")
        current.enabled = enabled
        current.lcd_on_time = on_time
        current.lcd_off_time = off_time
        current.sleep_if_no_image = bool(body.get("sleep_if_no_image", False))
        current.has_image = bool(body.get("has_image", False))
        current.updated_at = time.time()
        SCHEDULES[key] = current

    save_schedules()

    immediate = {"ok": True, "skipped": True}
    if current.device_id and enabled and current.sleep_if_no_image and not current.has_image:
        ok, status, payload = dispatch_lcd_command(current.device_id, "off")
        immediate = {"ok": ok, "status": status, "payload": payload}

    return jsonify({
        "ok": True,
        "schedule_key": key,
        "schedule": asdict(current),
        "immediate_action": immediate,
    })


@app.route("/schedules", methods=["GET"])
def schedules():
    with LOCK:
        rows = {k: asdict(v) for k, v in SCHEDULES.items()}
    return jsonify(rows)


def main():
    load_schedules()

    t_tcp = threading.Thread(target=tcp_loop, daemon=True)
    t_tcp.start()

    t_sched = threading.Thread(target=schedule_loop, daemon=True)
    t_sched.start()

    print(f"[{now()}] HTTP listening on {HOST}:{HTTP_PORT}")
    app.run(host=HOST, port=HTTP_PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
