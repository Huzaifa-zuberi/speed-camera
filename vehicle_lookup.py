import requests
import json
import random
import re
import os
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent / "vehicle_cache.json"

# 2captcha config – set TWOCAPTCHA_API_KEY env var or write to config.json
CONFIG_PATH = Path(__file__).parent / "config.json"
SINDH_SITEKEY = "6LczdnQsAAAAAK2YNjS9L6upyt4ng1cQiYzqXU24"
SINDH_EXCISE_URL = "https://excise.gos.pk/vehicle/vehicle_search"
SINDH_RESULT_URL = "https://excise.gos.pk/vehicle/vehicle_result"

def _load_twocaptcha_key():
    key = os.environ.get("TWOCAPTCHA_API_KEY", "")
    if not key:
        try:
            conf = json.loads(CONFIG_PATH.read_text())
            key = conf.get("twocaptcha_api_key", "")
        except Exception:
            pass
    return key

def _save_twocaptcha_key(key):
    try:
        conf = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        conf["twocaptcha_api_key"] = key
        CONFIG_PATH.write_text(json.dumps(conf, indent=2))
    except Exception as e:
        log.warning(f"Failed to save config: {e}")

# Lazy import for 2captcha (only when used)
_twocaptcha_solver = None
_last_api_key = None
def _get_captcha_solver():
    global _twocaptcha_solver, _last_api_key
    key = _load_twocaptcha_key()
    if not key:
        _twocaptcha_solver = None
        return None
    if _twocaptcha_solver is None or key != _last_api_key:
        try:
            from twocaptcha import TwoCaptcha
            _twocaptcha_solver = TwoCaptcha(key)
            _last_api_key = key
        except ImportError:
            log.warning("2captcha-python not installed; run: pip install 2captcha-python")
            _twocaptcha_solver = None
    return _twocaptcha_solver

def reload_captcha_config():
    """Force re-read of API key on next solver access."""
    global _twocaptcha_solver
    _twocaptcha_solver = None

MAKES = {
    "Suzuki": ["Mehran", "Cultus", "Wagon R", "Alto", "Swift", "Bolan", "Ravi", "Every"],
    "Toyota": ["Corolla", "Yaris", "Camry", "Fortuner", "Hilux", "Land Cruiser", "Prius", "Passo"],
    "Honda": ["Civic", "City", "Accord", "BR-V", "HR-V", "CR-V", "N-One"],
    "Hyundai": ["Sonata", "Elantra", "Tucson", "Santa Fe", "Grand i10", "i20"],
    "Kia": ["Sportage", "Sorento", "Picanto", "Rio", "Stonic"],
    "Daihatsu": ["Cuore", "Mira", "Move", "Terios", "Boon"],
    "Nissan": ["Sunny", "Dayz", "March", "Note"],
    "United": ["Prince", "Alpha", "Super Star"],
    "Qingqi": ["Qingqi Metro", "Qingqi Rickshaw"],
    "Honda Motorcycle": ["CD 70", "CG 125", "CB 150F", "CBR 250"],
    "Yamaha Motorcycle": ["YB 125", "YZF R15", "MT-15"],
}

# Bike makes (more likely for smaller plates)
BIKE_MAKES = ["Honda Motorcycle", "Yamaha Motorcycle", "Suzuki", "United"]
CAR_MAKES = ["Suzuki", "Toyota", "Honda", "Hyundai", "Kia", "Daihatsu", "Nissan"]

COLORS = [
    "White", "Black", "Silver", "Gray", "Blue", "Red",
    "Green", "Brown", "Beige", "Gold", "Maroon", "Dark Blue",
]

# Region-based city mapping
REGION_CITIES = {
    "sindh": ["Karachi", "Hyderabad", "Sukkur", "Larkana", "Nawabshah", "Mirpur Khas", "Jacobabad"],
    "punjab": ["Lahore", "Rawalpindi", "Faisalabad", "Multan", "Gujranwala", "Sialkot", "Sargodha", "Bahawalpur"],
    "kpk": ["Peshawar", "Abbottabad", "Mardan", "Swat", "Kohat", "Dera Ismail Khan"],
    "balochistan": ["Quetta", "Gwadar", "Turbat", "Khuzdar", "Zhob"],
    "islamabad": ["Islamabad"],
}
REGION_NAMES = {
    "sindh": ["Muhammad Ali", "Ahmed Khan", "Fatima Hussain", "Omar Sheikh",
              "Zainab Ahmed", "Bilal Mahmood", "Sana Mirza", "Usman Ghani",
              "Rabia Iqbal", "Imran Farooqi", "Nadia Hasan", "Tariq Jameel",
              "Sadia Khan", "Kamran Akmal", "Ali Raza", "Zara Shah"],
    "punjab": ["Ahmad Butt", "Sajid Mahmood", "Noreen Akhtar", "Khalid Parvez",
               "Tahir Iqbal", "Shahid Afridi", "Fariha Rashid", "Javed Mirza",
               "Shazia Khan", "Rashid Latif", "Nasreen Javed", "Iqbal Hussain"],
    "kpk": ["Hassan Khan", "Zahid Ullah", "Pervez Khattak", "Sher Ali",
            "Fazal Rehman", "Waqar Younis", "Sadia Bibi", "Tariq Ahmad"],
    "balochistan": ["Ali Ahmed", "Sanaullah Khan", "Ahmed Nawaz", "Riaz Hussain",
                    "Naseem Bibi", "Rashid Ahmed", "Yasmeen Baloch", "Aman Ullah"],
    "islamabad": ["Ahmed Raza", "Sara Riaz", "Usman Dar", "Hina Syed",
                  "Farhan Ali", "Zara Tariq", "Omar Hayat", "Mahnoor Sheikh"],
}

# Plate prefix -> (region, default_city) mapping
PLATE_REGIONS = {
    "K": ("sindh", "Karachi"),
    "A": ("sindh", "Karachi"),
    "L": ("punjab", "Lahore"),
    "P": ("punjab", "Peshawar"),
    "R": ("punjab", "Rawalpindi"),
    "M": ("punjab", "Multan"),
    "S": ("sindh", "Sukkur"),
    "I": ("islamabad", "Islamabad"),
    "G": ("punjab", "Gujranwala"),
    "F": ("punjab", "Faisalabad"),
    "Q": ("kpk", "Peshawar"),
    "T": ("kpk", "Kohat"),
    "B": ("balochistan", "Quetta"),
    "U": ("punjab", "Sargodha"),
    "D": ("sindh", "Hyderabad"),
    "Z": ("sindh", "Mirpur Khas"),
    "N": ("sindh", "Nawabshah"),
    "H": ("sindh", "Hyderabad"),
}

EXCISE_URLS = {
    "punjab": "https://mtmis.excise.punjab.gov.pk/",
    "sindh": "https://excise.gos.pk/vehicle/vehicle_search",
    "kpk": "https://excise.kp.gov.pk/vehicle-verification",
}

SINDH_STREETS = [
    "Shahrah-e-Faisal", "Tariq Road", "University Road", "Gulshan-e-Maymar",
    "Clifton", "Defence", "Nazimabad", "Korangi Road", "Landhi",
    "Tipu Sultan Road", "Burns Road", "I.I. Chundrigar Road",
    "Rashid Minhas Road", "Shaheed-e-Millat", "Super Highway",
    "Shah Faisal Colony", "Malir", "DHA Phase 2", "Gulistan-e-Jauhar",
]


class VehicleLookup:
    def __init__(self):
        self.cache = {}
        self._load_cache()

    def lookup(self, plate_text):
        plate = plate_text.strip().upper()
        if not plate or plate in ("???", "---", "OCR UNAVAILABLE", "UNREADABLE"):
            return None

        if plate in self.cache:
            return self.cache[plate]

        data = self._try_excise_with_captcha(plate)
        if not data:
            data = self._try_excise_lookup(plate)
        if not data:
            data = self._generate_mock(plate)
            data["source"] = "mock"

        self.cache[plate] = data
        self._save_cache()
        return data

    def _get_region_hint(self, plate):
        plate = plate.strip().upper().replace("-", "").replace(" ", "")
        if plate.startswith("ISB"):
            return ("islamabad", "Islamabad")
        if len(plate) >= 1:
            first = plate[0]
            if first.isalpha():
                info = PLATE_REGIONS.get(first, None)
                if info:
                    return info
        return (None, None)

    def _try_excise_with_captcha(self, plate):
        """Use 2captcha to solve Sindh excise portal reCAPTCHA and look up vehicle."""
        region, _ = self._get_region_hint(plate)
        if region != "sindh":
            return None

        solver = _get_captcha_solver()
        if not solver:
            log.debug("2captcha not configured, skipping CAPTCHA-based lookup")
            return None

        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })

            # Step 1: solve reCAPTCHA
            log.info(f"Solving reCAPTCHA for plate {plate}...")
            result = solver.recaptcha(
                sitekey=SINDH_SITEKEY,
                url=SINDH_EXCISE_URL,
            )
            token = result.get("code")
            if not token:
                log.warning("2captcha returned no token")
                return None

            # Step 2: POST to vehicle_result (form params: reg_no, wheelers_type, g-recaptcha-response)
            post_headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://excise.gos.pk",
                "Referer": SINDH_EXCISE_URL,
            }
            # Try 2-wheeler first, fall back to 4-wheeler
            for vtype in ("2", "4"):
                payload = {
                    "reg_no": plate,
                    "wheelers_type": vtype,
                    "g-recaptcha-response": token,
                }
                resp = session.post(SINDH_RESULT_URL, data=payload, headers=post_headers, timeout=15)
                if resp.status_code != 200:
                    continue
                text = resp.text.strip()
                if not text or text == "Invalid":
                    continue
                data = self._parse_excise_html(text, plate)
                if data:
                    data["source"] = "excise.sindh.gov.pk"
                    return data

            log.info(f"Excise CAPTCHA lookup returned no data for {plate}")
            return None

        except Exception as e:
            log.warning(f"Excise CAPTCHA lookup failed for {plate}: {e}")
            return None

    def _parse_excise_html(self, text, plate):
        """Parse the HTML table response from excise.gos.pk vehicle_result endpoint."""
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception:
            return None

        # Try table parsing first
        table = soup.find("table")
        if table:
            details = {}
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True).lower()
                    val = cells[1].get_text(strip=True)
                    details[key] = val
            if details:
                return {
                    "plate": plate,
                    "owner": details.get("owner name", details.get("owner", "N/A")),
                    "make": details.get("make", details.get("vehicle make", "N/A")),
                    "model": details.get("model", details.get("vehicle model", "N/A")),
                    "color": details.get("color", details.get("colour", "N/A")),
                    "registration_date": details.get("registration date", details.get("reg date", "N/A")),
                    "engine_no": details.get("engine no", details.get("engine number", "N/A")),
                    "chassis_no": details.get("chassis no", details.get("chassis number", "N/A")),
                    "status": details.get("status", details.get("registration status", "Active")),
                    "address": details.get("address", details.get("owner address", "N/A")),
                    "city": details.get("city", "N/A"),
                    "province": details.get("province", "Sindh"),
                }

        # Fall back to JSON parsing (some endpoints return JSON)
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data:
                return {
                    "plate": plate,
                    "owner": data.get("OwnerName", data.get("Owner", data.get("owner", "N/A"))),
                    "make": data.get("Make", data.get("make", "N/A")),
                    "model": data.get("Model", data.get("model", "N/A")),
                    "color": data.get("Color", data.get("color", "N/A")),
                    "registration_date": data.get("RegistrationDate", data.get("reg_date", "N/A")),
                    "engine_no": data.get("EngineNo", data.get("engine_no", "N/A")),
                    "chassis_no": data.get("ChassisNo", data.get("chassis_no", "N/A")),
                    "status": data.get("Status", data.get("status", "Active")),
                    "address": data.get("Address", data.get("OwnerAddress", data.get("address", "N/A"))),
                    "city": data.get("City", data.get("city", "N/A")),
                    "province": data.get("Province", data.get("province", "Sindh")),
                }
        except json.JSONDecodeError:
            pass

        return None

    def _try_excise_lookup(self, plate):
        region, _ = self._get_region_hint(plate)
        ordered_urls = list(EXCISE_URLS.items())

        matched = [(k, v) for k, v in ordered_urls if k == region]
        others = [(k, v) for k, v in ordered_urls if k != region]
        ordered = matched + others

        for prov_key, url in ordered:
            try:
                session = requests.Session()
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                session.headers.update(headers)

                resp = session.get(url, timeout=8)
                if resp.status_code != 200:
                    continue

                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                token = None
                token_input = soup.find("input", {"name": re.compile(r"csrf|token", re.I)})
                if token_input:
                    token = token_input.get("value")

                payload = {"regno": plate}
                if token:
                    payload[token_input.get("name")] = token

                post_resp = session.post(url, data=payload, timeout=8)
                if post_resp.status_code == 200:
                    data = self._parse_response(post_resp.text, plate)
                    if data:
                        data["source"] = f"excise.{prov_key}.gov.pk"
                        return data
            except Exception as e:
                log.warning(f"Excise lookup on {prov_key} failed for {plate}: {e}")
                continue
        return None

    def _parse_response(self, html, plate):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            if not table:
                return None
            rows = table.find_all("tr")
            details = {}
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True).lower()
                    val = cells[1].get_text(strip=True)
                    details[key] = val
            if not details:
                return None
            return {
                "plate": plate,
                "owner": details.get("owner name", details.get("name", "N/A")),
                "make": details.get("make", details.get("vehicle make", "N/A")),
                "model": details.get("model", details.get("vehicle model", "N/A")),
                "color": details.get("color", details.get("vehicle color", "N/A")),
                "registration_date": details.get("registration date", details.get("reg date", "N/A")),
                "engine_no": details.get("engine no", details.get("engine number", "N/A")),
                "chassis_no": details.get("chassis no", details.get("chassis number", "N/A")),
                "status": details.get("status", details.get("registration status", "Active")),
                "address": details.get("address", details.get("owner address", "N/A")),
            }
        except Exception as e:
            log.warning(f"Parse error: {e}")
            return None

    def _generate_mock(self, plate):
        region, default_city = self._get_region_hint(plate)
        if region is None:
            region = "sindh"
            default_city = "Karachi"

        if region == "sindh":
            cities = REGION_CITIES["sindh"]
            names = REGION_NAMES["sindh"]
            province = "Sindh"
            streets = SINDH_STREETS
            make = random.choice(BIKE_MAKES + CAR_MAKES)
        elif region == "punjab":
            cities = REGION_CITIES["punjab"]
            names = REGION_NAMES["punjab"]
            province = "Punjab"
            streets = ["The Mall", "Main Boulevard", "Jail Road", "Canal Road", "GT Road"]
            make = random.choice(list(MAKES.keys()))
        elif region == "kpk":
            cities = REGION_CITIES["kpk"]
            names = REGION_NAMES["kpk"]
            province = "KPK"
            streets = ["Grand Trunk Road", "Mall Road", "University Road"]
            make = random.choice(CAR_MAKES + BIKE_MAKES)
        elif region == "balochistan":
            cities = REGION_CITIES["balochistan"]
            names = REGION_NAMES["balochistan"]
            province = "Balochistan"
            streets = ["Jinnah Road", "Civil Hospital Road", "Airport Road"]
            make = random.choice(CAR_MAKES + BIKE_MAKES)
        else:
            cities = REGION_CITIES["islamabad"]
            names = REGION_NAMES["islamabad"]
            province = "Islamabad"
            streets = ["Constitution Avenue", "Jinnah Avenue", "Khayaban-e-Suhrawardy"]
            make = random.choice(list(MAKES.keys()))

        model = random.choice(MAKES[make])
        year = random.randint(2005, 2025)
        color = random.choice(COLORS)
        city = default_city if default_city in cities else random.choice(cities)
        reg_date = datetime(random.randint(2005, 2025), random.randint(1, 12), random.randint(1, 28))
        owner = random.choice(names)
        street = f"{random.randint(1, 999)}, {random.choice(streets)}"
        return {
            "plate": plate,
            "owner": owner,
            "make": make,
            "model": f"{model} {year}",
            "color": color,
            "registration_date": reg_date.strftime("%Y-%m-%d"),
            "engine_no": f"EN{random.randint(100000, 999999)}",
            "chassis_no": f"CH{random.randint(1000000, 9999999)}",
            "status": random.choice(["Active", "Active", "Active", "Active", "Expired"]),
            "address": f"{street}, {city}, {province}",
            "city": city,
            "province": province,
            "source": "mock",
        }

    def _load_cache(self):
        try:
            if CACHE_PATH.exists():
                with open(CACHE_PATH) as f:
                    self.cache = json.load(f)
        except Exception:
            self.cache = {}

    def _save_cache(self):
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_PATH, "w") as f:
                json.dump(self.cache, f, indent=2)
        except Exception:
            pass
