import cv2
import numpy as np
import json
import uuid
import time
from datetime import datetime
from pathlib import Path
from vehicle_lookup import VehicleLookup

FINES_DB = Path(__file__).parent / "fines_db.json"
PLATE_WIDTH_M = 0.52


class NumberPlateSystem:
    def __init__(self, speed_limit=60):
        self.speed_limit = speed_limit
        self.fines = []
        self.reader = None
        self.ocr_ok = False
        self.alpr = None
        self.vehicle_db = VehicleLookup()
        self._load()
        self._init_ocr()
        self._init_alpr()

    def _init_ocr(self):
        try:
            import easyocr
            self.reader = easyocr.Reader(["en"], gpu=False)
            self.ocr_ok = True
        except Exception:
            self.ocr_ok = False

    def _init_alpr(self):
        try:
            from fast_alpr import ALPR
            self.alpr = ALPR(
                detector_model="yolo-v9-t-640-license-plate-end2end",
                ocr_model="global-plates-mobile-vit-v2-model",
                detector_conf_thresh=0.15,
            )
        except Exception:
            self.alpr = None

    def detect_plate(self, frame):
        """Try fast-alpr YOLO detection first, fall back to OpenCV strategies.
        Returns: (plate_img, bbox, approx, pre_read_text)
        """
        if self.alpr is not None:
            try:
                results = self.alpr.predict(frame)
                if results:
                    r = results[0]
                    bb = r.detection.bounding_box
                    bw = bb.x2 - bb.x1
                    bh = bb.y2 - bb.y1
                    bbox = (bb.x1, bb.y1, bw, bh)
                    plate_img = frame[bb.y1:bb.y2, bb.x1:bb.x2]
                    plate_text = ""
                    if r.ocr is not None and r.ocr.text:
                        plate_text = r.ocr.text.strip().upper()
                    return plate_img, bbox, None, plate_text
            except Exception:
                pass

        candidates = []

        try:
            candidates.extend(self._detect_mser(frame))
        except Exception:
            pass

        try:
            candidates.extend(self._detect_edges(frame))
        except Exception:
            pass

        try:
            candidates.extend(self._detect_color(frame))
        except Exception:
            pass

        if not candidates:
            return None, None, None, None

        best = max(candidates, key=lambda c: c[0])
        score, plate_img, bbox, approx = best

        if score < 0.5:
            return None, None, None, None

        return plate_img, bbox, approx, None

    def _detect_mser(self, frame):
        """Detect plate by finding text regions with MSER and grouping them."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = frame.shape[:2]

        mser = cv2.MSER_create()
        mser.setMinArea(30)
        mser.setMaxArea(5000)

        regions, _ = mser.detectRegions(gray)
        if len(regions) < 3:
            return []

        hulls = [cv2.convexHull(r.reshape(-1, 1, 2)) for r in regions]

        # Group nearby hulls into clusters
        all_pts = np.vstack([r.reshape(-1, 2) for r in regions])
        if len(all_pts) < 4:
            return []

        rect = cv2.minAreaRect(all_pts)
        box = cv2.boxPoints(rect)
        box = np.int32(box)

        x, y, bw, bh = cv2.boundingRect(box)
        if bw < 40 or bh < 15:
            return []
        if bw > w * 0.8 or bh > h * 0.5:
            return []

        ar = bw / float(bh)
        if 1.2 < ar < 6.0:
            margin = 10
            x1 = max(0, x - margin)
            y1 = max(0, y - margin)
            x2 = min(w, x + bw + margin)
            y2 = min(h, y + bh + margin)
            plate_roi = frame[y1:y2, x1:x2]
            score = min(len(regions) / 15, 1.5) + (0.5 if 1.5 < ar < 5.0 else 0)
            return [(score, plate_roi, (x1, y1, x2 - x1, y2 - y1), box)]

        return []

    def _detect_edges(self, frame):
        """Detect plate using edge detection + contour finding."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = frame.shape[:2]
        results = []

        # Adaptive Canny thresholds based on image median
        median = np.median(gray)
        low = int(max(10, 0.4 * median))
        high = int(min(200, 1.3 * median))

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blur = cv2.bilateralFilter(enhanced, 7, 15, 15)
        edges = cv2.Canny(blur, low, high)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            peri = cv2.arcLength(c, True)
            if peri < 40:
                continue
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                x, y, cw, ch = cv2.boundingRect(approx)
                if cw < 40 or ch < 15:
                    continue
                if cw > w * 0.9 or ch > h * 0.5:
                    continue
                ar = cw / float(ch)
                if ar < 1.2 or ar > 6.5:
                    continue
                area = cw * ch
                if area < 500 or area > w * h * 0.2:
                    continue

                margin = 5
                x1 = max(0, x - margin)
                y1 = max(0, y - margin)
                x2 = min(w, x + cw + margin)
                y2 = min(h, y + ch + margin)

                plate_roi = frame[y1:y2, x1:x2]
                score = 1.0
                if 2.0 < ar < 4.5:
                    score += 1.0
                elif 1.5 < ar < 5.5:
                    score += 0.5
                score += min(area / 10000, 2.0)

                results.append((score, plate_roi, (x1, y1, x2 - x1, y2 - y1), approx))

        return results

    def _detect_color(self, frame):
        """Detect plate by finding white/light-colored rectangular regions."""
        h, w = frame.shape[:2]
        results = []

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # White regions (common plate background)
        lower_white = np.array([0, 0, 150])
        upper_white = np.array([180, 40, 255])
        mask_white = cv2.inRange(hsv, lower_white, upper_white)

        # Yellow regions (common for bike plates in some regions)
        lower_yellow = np.array([15, 50, 100])
        upper_yellow = np.array([45, 255, 255])
        mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)

        mask = cv2.bitwise_or(mask_white, mask_yellow)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if cw < 40 or ch < 15:
                continue
            if cw > w * 0.9 or ch > h * 0.5:
                continue
            ar = cw / float(ch)
            if ar < 1.2 or ar > 6.5:
                continue
            area = cw * ch
            if area < 500 or area > w * h * 0.2:
                continue

            # Check if region has text-like content (high edge density)
            gray = cv2.cvtColor(frame[y:y+ch, x:x+cw], cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            edge_density = np.sum(edges > 0) / max(area, 1)

            if edge_density < 0.01:
                continue

            x1 = max(0, x - 5)
            y1 = max(0, y - 5)
            x2 = min(w, x + cw + 5)
            y2 = min(h, y + ch + 5)
            plate_roi = frame[y1:y2, x1:x2]

            score = 0.5 + edge_density * 10
            if 2.0 < ar < 4.5:
                score += 0.5

            results.append((score, plate_roi, (x1, y1, x2 - x1, y2 - y1), c))

        return results

    def read_plate(self, plate_img, attempts=3):
        if not self.ocr_ok or self.reader is None:
            return "OCR UNAVAILABLE"
        if plate_img is None or plate_img.size == 0:
            return "???"

        h, w = plate_img.shape[:2]
        if w < 80 or h < 25:
            scale = max(2, min(4, 200 // max(w, 1)))
            plate_img = cv2.resize(plate_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        texts = []

        variants = []

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        sharpen = cv2.GaussianBlur(enhanced, (0, 0), 1.5)
        sharpened = cv2.addWeighted(enhanced, 1.5, sharpen, -0.5, 0)
        variants.append(sharpened)

        # Bilateral filter + OTSU
        blur = cv2.bilateralFilter(gray, 9, 17, 17)
        _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(binary)

        # Adaptive threshold
        at = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 11, 2)
        variants.append(at)

        # Try OCR on each variant
        for variant in variants:
            try:
                results = self.reader.readtext(variant)
                for r in results:
                    text = r[1].strip()
                    conf = r[2]
                    if len(text) >= 2 and conf > 0.1:
                        texts.append((text.upper(), conf))
            except Exception:
                continue

        if texts:
            texts.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
            return texts[0][0]

        return "???"

    def read_plate_from_file(self, image_path):
        img = cv2.imread(str(image_path))
        if img is None:
            return None, "Could not read image"
        plate_img, bbox, _, pre_read = self.detect_plate(img)
        if bbox is None:
            return None, "No plate detected in image"
        text = pre_read if pre_read else self.read_plate(plate_img)
        return text, None

    def estimate_speed(self, prev, curr, plate_w, dt):
        if prev is None or curr is None or plate_w <= 0 or dt <= 0:
            return 0
        dx = curr[0] - prev[0]
        dy = curr[1] - prev[1]
        dist_px = np.sqrt(dx * dx + dy * dy)
        if dist_px < 2:
            return 0
        dist_m = (dist_px / plate_w) * PLATE_WIDTH_M
        return (dist_m / dt) * 3.6

    def generate_fine(self, plate, speed, limit):
        excess = speed - limit
        if excess <= 10:
            amount = 50
        elif excess <= 20:
            amount = 100
        elif excess <= 30:
            amount = 200
        else:
            amount = 500
        vehicle = self.vehicle_db.lookup(plate)
        fine = {
            "id": str(uuid.uuid4())[:8].upper(),
            "plate": plate,
            "speed": round(speed, 1),
            "limit": limit,
            "excess": round(excess, 1),
            "amount": amount,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "UNPAID",
            "vehicle": vehicle,
        }
        self.fines.append(fine)
        self._save()
        return fine

    def _load(self):
        try:
            if FINES_DB.exists():
                with open(FINES_DB) as f:
                    self.fines = json.load(f)
        except Exception:
            self.fines = []

    def _save(self):
        try:
            FINES_DB.parent.mkdir(parents=True, exist_ok=True)
            with open(FINES_DB, "w") as f:
                json.dump(self.fines, f, indent=2)
        except Exception:
            pass

    def pay_fine(self, fid):
        for f in self.fines:
            if f["id"] == fid:
                f["status"] = "PAID"
                self._save()
                return True
        return False

    def update_vehicle(self, plate, updates):
        plate = plate.strip().upper()
        self.vehicle_db.cache[plate] = {**self.vehicle_db.cache.get(plate, {}), **updates, "source": "manual"}
        self.vehicle_db._save_cache()
        for f in self.fines:
            if f["plate"] == plate:
                f["vehicle"] = self.vehicle_db.cache[plate]
        self._save()
        return True

    def stats(self):
        total = len(self.fines)
        unpaid = sum(1 for f in self.fines if f["status"] == "UNPAID")
        return {
            "total": total,
            "unpaid": unpaid,
            "paid": total - unpaid,
            "total_amount": sum(f["amount"] for f in self.fines),
            "collected": sum(f["amount"] for f in self.fines if f["status"] == "PAID"),
        }


class SpeedCamProcessor:
    def __init__(self):
        self.sys = NumberPlateSystem()
        self.prev_center = None
        self.prev_pw = None
        self.plate_text = "---"
        self.speed = 0.0
        self.speed_limit = 60
        self.last_t = time.time()
        self.n_frame = 0
        self.violation = None
        self.vf = 0

    def process(self, frame):
        self.n_frame += 1
        now = time.time()
        dt = now - self.last_t
        self.last_t = now
        h, w = frame.shape[:2]

        plate_img, bbox, _, pre_read = self.sys.detect_plate(frame)

        if bbox:
            x, y, pw, ph = bbox
            center = (x + pw // 2, y + ph // 2)

            cv2.rectangle(frame, (x, y), (x + pw, y + ph), (0, 255, 0), 2)
            cv2.circle(frame, center, 4, (255, 255, 0), -1)

            if pre_read:
                self.plate_text = pre_read
            elif self.n_frame % 6 == 0:
                self.plate_text = self.sys.read_plate(plate_img)

            if self.prev_center:
                speed = self.sys.estimate_speed(self.prev_center, center, self.prev_pw, dt)
                self.speed = speed
                cv2.line(frame, self.prev_center, center, (255, 0, 0), 2)

                if speed > self.speed_limit and self.plate_text not in ("---", "???", "OCR UNAVAILABLE"):
                    fine = self.sys.generate_fine(self.plate_text, speed, self.speed_limit)
                    self.violation = fine
                    self.vf = 0

            self.prev_center = center
            self.prev_pw = pw
        else:
            self.speed = max(0.0, self.speed - 0.5)
            if self.n_frame > 15:
                self.prev_center = None

        cv2.putText(frame, f"Plate: {self.plate_text}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        sc = (0, 255, 0) if self.speed <= self.speed_limit else (0, 0, 255)
        cv2.putText(frame, f"Speed: {self.speed:.1f} km/h", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, sc, 2)
        cv2.putText(frame, f"Limit: {self.speed_limit} km/h", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 2)
        cv2.putText(frame, f"Fines: {len(self.sys.fines)}", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 2)

        if self.violation and self.vf < 60:
            self.vf += 1
            v = self.violation
            overlay = frame.copy()
            cv2.rectangle(overlay, (0, 0), (w, 70), (0, 0, 200), -1)
            cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)
            cv2.putText(frame, f"VIOLATION! {v['plate']}  ${v['amount']}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, f"{v['speed']} km/h over {v['limit']} limit", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        elif self.vf >= 60:
            self.violation = None
            self.vf = 0

        return frame

    def status(self):
        return {
            "plate": self.plate_text,
            "speed": round(self.speed, 1),
            "limit": self.speed_limit,
            "fines": len(self.sys.fines),
            "stats": self.sys.stats(),
        }
