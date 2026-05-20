import pytesseract
from PIL import Image
import aiohttp
import io
import re
import os
import platform
import urllib.request
import hashlib
import logging

logger = logging.getLogger("eumenes.ocr")

TESSDATA_DIR = os.path.join(os.path.dirname(__file__), "tessdata")

KNOWN_CHECKSUMS = {}


def _find_tesseract():
    cmd = os.environ.get("TESSERACT_CMD", "")
    if cmd:
        return cmd
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files\UB-Mannheim\Tesseract-OCR\tesseract.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
    return "tesseract"


def _verify_checksum(path, expected_sha):
    if not expected_sha:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest() == expected_sha


def _ensure_languages():
    os.makedirs(TESSDATA_DIR, exist_ok=True)
    needed = {"ara.traineddata", "fra.traineddata", "eng.traineddata"}
    base = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/"
    for lang in needed:
        dest = os.path.join(TESSDATA_DIR, lang)
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        if size < 500000:
            if os.path.exists(dest):
                logger.warning("%s is too small (%d bytes), re-downloading...", lang, size)
            else:
                logger.info("Downloading %s...", lang)
            url = base + lang
            urllib.request.urlretrieve(url, dest)
            expected = KNOWN_CHECKSUMS.get(lang)
            if expected and not _verify_checksum(dest, expected):
                os.remove(dest)
                raise RuntimeError(f"Checksum mismatch for {lang}, possible tampering")


tesseract_cmd = _find_tesseract()
pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR
_ensure_languages()


async def download_image(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()


C2PA_MARKERS = [b"c2pa", b"openai", b"dall-e", b"stability.ai"]


def analyze_image_security(img, raw_bytes):
    flags = []
    try:
        exif = img.getexif()
        make = exif.get(271) if exif else None
        model = exif.get(272) if exif else None
        software = exif.get(305) if exif else None
        description = exif.get(270) if exif else None
        if any(x and ("AI" in x.upper() or "OPENAI" in x.upper() or "DALL" in x.upper()) for x in [make, model, software, description]):
            flags.append("ai_metadata_detected")
    except Exception:
        pass

    w, h = img.size
    if w < 200 or h < 200:
        flags.append("too_small")
    aspect = w / h
    if aspect < 0.5 or aspect > 2.5:
        flags.append("unusual_aspect")

    for marker in C2PA_MARKERS:
        if marker in raw_bytes:
            flags.append("c2pa_detected")
            break

    return flags


def parse_receipt(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")

    security_flags = analyze_image_security(img, image_bytes)
    text = pytesseract.image_to_string(img, lang="ara+fra+eng")

    amount = extract_amount(text)
    amount_flagged = amount > 10000

    result = {
        "amount": amount,
        "currency": "TND",
        "tx_id": extract_tx_id(text),
        "sender": extract_sender(text),
        "timestamp": extract_timestamp(text),
        "commission": extract_commission(text),
        "auth_code": extract_auth_code(text),
        "confidence": 0.3 if amount_flagged else (0.8 if amount > 0 else 0.3),
        "raw_text": text[:500],
        "suspicious": False,
        "flags": security_flags,
    }

    if amount_flagged:
        result["flags"].append("amount_exceeds_threshold")

    result["suspicious"] = (
        result["amount"] > 0
        and (
            "c2pa_detected" in security_flags
            or "ai_metadata_detected" in security_flags
        )
    )

    return result


def _parse_tunisian_number(s):
    s = s.strip()
    if "," in s and s.count(",") == 1:
        parts = s.split(",")
        if len(parts[1]) == 3:
            s = parts[0] + "." + parts[1]
        elif len(parts[1]) == 2:
            s = parts[0] + "." + parts[1]
    s = s.replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0


def extract_amount(text):
    patterns = [
        r"(\d+[.,]\d{3})\s*(?:TND|د\.ت|DT|dinars?)",
        r"(\d+[.,]\d{2})\s*(?:TND|د\.ت|DT|dinars?)",
        r"(\d+)\s*(?:TND|د\.ت|DT|dinars?)",
        r"(?:montant\s*(?:de|du)?\s*transfert|transfert.*montant\s*de)\s*(\d+[.,]?\d*)",
        r"(?:montant|total|amount|prix|price|المبلغ|الإجمالي|المجموع|مبلغ)[:\s]*(\d+[.,]?\d*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = _parse_tunisian_number(match.group(1))
            if val > 0:
                return val
    return 0


def extract_tx_id(text):
    patterns = [
        r"(?:opération|transaction|référence|ref|n°|no|numéro|رقم|رقم العملية|مرجع)[:\s]*([A-Z0-9\-_\/]{6,})",
        r"numéro\s*(\d{6,12})",
        r"([A-Z]{2,}\d{6,})",
        r"\b(\d{8,20})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def extract_sender(text):
    patterns = [
        r"(?:expéditeur|sender|de|from|envoyé par|المرسل|من)[:\s]*([A-Za-zÀ-ÿ\u0600-\u06FF\s]{3,30})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def extract_timestamp(text):
    patterns = [
        r"(\d{2}/\d{2}/\d{4}\s*\d{2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2})",
        r"(\d{2}/\d{2}/\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def extract_commission(text):
    patterns = [
        r"(?:commission|frais|fee)[:\s]*(\d+[.,]?\d*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = match.group(1).replace(",", "")
            try:
                return float(val)
            except ValueError:
                continue
    return 0


def extract_auth_code(text):
    patterns = [
        r"(?:numéro\s*(?:d'|de\s+)?autorisation|autorisation)[:\s]*(\d{6,15})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""
