import cv2
import numpy as np
import logging
import traceback
import threading
import time
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify
from werkzeug.utils import secure_filename

from number_plate_reader import SpeedCamProcessor
from vehicle_lookup import reload_captcha_config, _save_twocaptcha_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = Path("uploads")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "tiff"}

PROCESS_W = 640
PROCESS_H = 480
camera = None
camera_lock = threading.Lock()


def get_camera():
    global camera
    with camera_lock:
        if camera is not None:
            return camera
        for backend in [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]:
            try:
                cap = cv2.VideoCapture(0, backend)
                if not cap.isOpened():
                    cap.release()
                    continue
                ret, frame = cap.read()
                if ret and frame is not None:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, PROCESS_W)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, PROCESS_H)
                    camera = cap
                    log.info(f"Camera opened (backend {backend})")
                    return camera
                cap.release()
            except Exception as e:
                log.warning(f"Backend {backend} failed: {e}")
        return None


def close_camera():
    global camera
    with camera_lock:
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass
            camera = None


def get_frame():
    cap = get_camera()
    if cap is None:
        return None
    with camera_lock:
        try:
            ret, frame = cap.read()
            if not ret or frame is None:
                return None
            return frame.copy()
        except Exception as e:
            log.error(f"Frame read error: {e}")
            return None


processors = {}
proc_lock = threading.Lock()


def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_processor():
    with proc_lock:
        if "speedcam" not in processors:
            processors["speedcam"] = SpeedCamProcessor()
        return processors["speedcam"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/live")
def live():
    return render_template("live.html")


@app.route("/status")
def status():
    try:
        p = get_processor()
        return jsonify(p.status())
    except Exception as e:
        return jsonify(success=False, error=repr(e)), 500


@app.route("/fines")
def fines():
    p = get_processor()
    return render_template("fines.html", fines=p.sys.fines, stats=p.sys.stats())


@app.route("/fine/<fine_id>")
def fine_detail(fine_id):
    p = get_processor()
    fine = next((f for f in p.sys.fines if f["id"] == fine_id), None)
    if not fine:
        return "Fine not found", 404
    return render_template("fine_detail.html", fine=fine)


@app.route("/pay/<fine_id>")
def pay_fine(fine_id):
    p = get_processor()
    if p.sys.pay_fine(fine_id):
        return redirect(url_for("fine_detail", fine_id=fine_id))
    return "Fine not found", 404


@app.route("/update-vehicle/<fine_id>", methods=["POST"])
def update_vehicle(fine_id):
    p = get_processor()
    fine = next((f for f in p.sys.fines if f["id"] == fine_id), None)
    if not fine:
        return "Fine not found", 404

    updates = {
        "owner": request.form.get("owner", "").strip(),
        "make": request.form.get("make", "").strip(),
        "model": request.form.get("model", "").strip(),
        "color": request.form.get("color", "").strip(),
        "engine_no": request.form.get("engine_no", "").strip(),
        "chassis_no": request.form.get("chassis_no", "").strip(),
        "registration_date": request.form.get("registration_date", "").strip(),
        "status": request.form.get("status", "").strip(),
        "address": request.form.get("address", "").strip(),
        "city": request.form.get("city", "").strip(),
        "province": request.form.get("province", "").strip(),
    }
    updates = {k: v for k, v in updates.items() if v}
    if not updates:
        return redirect(url_for("fine_detail", fine_id=fine_id))

    p.sys.update_vehicle(fine["plate"], updates)
    return redirect(url_for("fine_detail", fine_id=fine_id))


@app.route("/frame")
def frame():
    f = get_frame()
    if f is None:
        return jsonify(success=False, error="Camera not available"), 503

    try:
        p = get_processor()
        f = p.process(f)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return jsonify(success=False, error="Failed to encode frame"), 500
        return Response(buf.tobytes(), mimetype="image/jpeg")
    except Exception as e:
        log.error(f"Process error: {traceback.format_exc()}")
        return jsonify(success=False, error=repr(e)), 500


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        if "file" not in request.files:
            return redirect(request.url)
        file = request.files["file"]
        if file.filename == "" or not allowed_file(file.filename):
            return redirect(request.url)

        filename = secure_filename(file.filename)
        filepath = app.config["UPLOAD_FOLDER"] / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        file.save(str(filepath))

        img = cv2.imread(str(filepath))
        if img is None:
            return render_template("upload.html", error="Failed to read image")

        p = get_processor()
        plate_img, bbox, _, pre_read = p.sys.detect_plate(img)
        result_img = img.copy()

        if bbox:
            x, y, w, h = bbox
            cv2.rectangle(result_img, (x, y), (x + w, y + h), (0, 255, 0), 3)
            text = pre_read if pre_read else p.sys.read_plate(plate_img)
            cv2.putText(result_img, text, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        else:
            text = None

        _, buf = cv2.imencode(".jpg", result_img)
        img_b64 = buf.tobytes().hex()

        return render_template("upload.html", img=img_b64, text=text, bbox=bbox)

    return render_template("upload.html")


@app.route("/manual", methods=["GET", "POST"])
def manual():
    if request.method == "POST":
        plate = request.form.get("plate", "").strip().upper()
        speed_str = request.form.get("speed", "80")
        limit_str = request.form.get("limit", "60")

        if not plate or len(plate) < 2:
            return render_template("manual.html", error="Invalid plate number")

        try:
            speed = float(speed_str)
            limit = float(limit_str)
        except ValueError:
            return render_template("manual.html", error="Invalid speed or limit")

        p = get_processor()
        fine = p.sys.generate_fine(plate, speed, limit)
        return redirect(url_for("fine_detail", fine_id=fine["id"]))

    return render_template("manual.html")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    from vehicle_lookup import _load_twocaptcha_key
    if request.method == "POST":
        key = request.form.get("twocaptcha_api_key", "").strip()
        _save_twocaptcha_key(key)
        reload_captcha_config()
        return redirect(url_for("settings"))
    current_key = _load_twocaptcha_key()
    masked = current_key[:6] + "****" + current_key[-4:] if len(current_key) > 10 else ""
    return render_template("settings.html", has_key=bool(current_key), masked_key=masked)


@app.route("/stop")
def stop():
    global processors
    with proc_lock:
        processors = {}
    close_camera()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
