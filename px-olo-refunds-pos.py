import os
import json
import time
import uuid
import hashlib
import secrets
import logging
import base64
import requests
from datetime import datetime, timezone
from threading import Thread
from urllib.parse import parse_qs, urlparse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from bs4 import BeautifulSoup
import re
from flask import Flask, jsonify

# OPEN https://px-olo-refunds-pos.onrender.com/health TO KEEP ALIVE DURING TEST PHASE

# ========================= CONFIG =========================
API_HOST = "https://mte2-sts.oraclecloud.com"
AUTH_HOST = "https://ors-idm.mte2.oraclerestaurants.com"
API_BASE = f"{API_HOST}/api/v1"
AUTH_BASE = f"{AUTH_HOST}/oidc-provider/v1/oauth2"

CLIENT_ID = os.environ.get("CLIENT_ID") #"QkdJLjgxMzBlYjZjLWE0NWYtNDhiMi1hNTEyLTI5MWI2ZjNlYjhiZg"
USERNAME = os.environ.get("USERNAME") #"MITCH STSGen2"
PASSWORD = os.environ.get("PASSWORD") #"B1llGr4y5!!!!"
ORG_SHORT_NAME = "bgi"
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/opt/render/project/src/simphony_tokens.json")
EMPLOYEE_REF = "1929"
ORDER_TYPE_REF = 24
MENU_ITEM_REF = 10103
MENU_ITEM_NAME = "Open Food Incl Tax"
RVC_REF = "1"

USE_IDEMPOTENCY = True
TOKEN_FILE = "C:\\Users\\mmcke4\\Desktop\\Projects\\TomWahls\\simphony_tokens.json"
VERBOSE_BODY = True
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
POLL_INTERVAL = 60 # in seconds
SUBJECT_KEYWORD = "Your Refund Has Been Submitted By"

LOCS = {
    "bg lab": "Menu",
    "000010": "Webster",
    "000020": "Henrietta",
    "000040": "Penfield",
    "000050": "Seabreeze (Culver Rd.)",
    "000060": "Strong (Admission Required)",
    "000070": "Chili (Chili Ave.)",
    "000080": "Latta Rd.",
    "000090": "Brockport",
    "000100": "Avon",
    "000120": "Irondequoit",
    "000135": "Gates (Buffalo Rd.)",
    "000170": "Ontario",
    "000210": "North Greece Rd.",
    "000220": "Port",
    "000300": "Flaherty's",
    "002000": "Bushnell's Basin",
    "003000": "Canandaigua",
    "006000": "Newark",
    "006500": "Fairport",
    "009500": "Brighton",
    "010500": "Greece"
}

# ====================== FLASK APP ======================
app = Flask(__name__)

@app.route('/health')
def health():
    return jsonify({
        "status": "running",
        "message": "Refund monitor is active",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/')
def home():
    return jsonify({
        "service": "BGI Refund Monitor",
        "status": "running",
        "endpoints": ["/health"]
    })

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("px-olo-refund-pos.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ====================== GMAIL SERVICE ======================
def get_gmail_service():
    logger.info("Initializing Gmail service...")
    
    creds = None
    
    token_json = os.environ.get("GMAIL_TOKEN") # env in Render
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
            logger.info("✅ Loaded token from environment variable")
        except Exception as e:
            logger.error(f"❌ Failed to parse GMAIL_TOKEN: {e}")

    service = build('gmail', 'v1', credentials=creds)
    logger.info("✅ Gmail service initialized successfully.")
    return service

# ====================== LOCATION NORMALIZATION ======================
def normalize_location(raw_location: str) -> str:
    """Return Simphony locref (e.g. '000010') from displayed location name"""
    if not raw_location:
        return None
    
    # Strip brand prefixes
    cleaned = re.sub(r'^(Bill Gray\'s|Tom Wahl\'s|Flaherty\'s)\s*', '', raw_location.strip(), flags=re.I)
    cleaned_lower = cleaned.lower().strip()

    # Build reverse map: name → locref
    for locref, name in LOCS.items():
        if cleaned_lower == name.lower().strip() or cleaned_lower in name.lower():
            return locref
        if locref in cleaned_lower:   # fallback for codes
            return locref

    logger.warning(f"⚠️ Could not map location: '{raw_location}' → cleaned: '{cleaned}'")
    return None

# ====================== MAIN EXTRACTOR ========================================
def extract_refund_data(html_content: str, subject: str) -> dict:
    logger.info(f"Processing email: {subject}")
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # ====================== LOCATION EXTRACTION ===============================
    raw_location = None
    full_text = (subject + " " + html_content).lower()
    
    # Bill Gray's
    if "bill gray" in full_text:
        match = re.search(r"Bill Gray's?\s+([^\s<,]+(?:\s+[^\s<,]+)?)", subject + " " + html_content, re.I)
        if match:
            raw_location = f"Bill Gray's {match.group(1).strip()}"
    
    # Tom Wahl's
    elif "tom wahl" in full_text:
        match = re.search(r"Tom Wahl's?\s+([^\s<,]+(?:\s+[^\s<,]+)?)", subject + " " + html_content, re.I)
        if match:
            raw_location = f"Tom Wahl's {match.group(1).strip()}"
    
    # Flaherty's
    elif "flaherty" in full_text:
        raw_location = "Flaherty's"
    
    # Fallback using your LOCS dictionary (location codes)
    else:
        for code, name in LOCS.items():
            if code in full_text:
                raw_location = name
                break
    
    # Final fallback from subject line
    if not raw_location:
        match = re.search(r"By\s+(.+?)(?:\s+|$)", subject, re.I)
        if match:
            raw_location = match.group(1).strip()

    simphony_locref = normalize_location(raw_location)

    # ====================== BODY DATA EXTRACTION ======================
    data = {
        "order_number": None,
        "raw_location": raw_location,
        "simphony_locref": simphony_locref,
        "refund_amount": None,
        "requested_datetime": None,
        "submitted_datetime": None,
        "reason": None,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "email_subject": subject
    }
    
    # Order Number
    order_label = soup.find(string=re.compile(r'Order Number', re.I))
    if order_label:
        next_td = order_label.find_parent('td').find_next_sibling('td')
        if next_td:
            data["order_number"] = next_td.get_text(strip=True)
    
    # Refund Amount
    amt_match = re.search(r'(-\d+\.\d{2})', html_content)
    if amt_match:
        data["refund_amount"] = amt_match.group(1)
    
    # Requested Date and Time
    req_label = soup.find(string=re.compile(r'Requested Date and Time', re.I))
    if req_label:
        next_td = req_label.find_parent('td').find_next_sibling('td')
        if next_td:
            data["requested_datetime"] = next_td.get_text(strip=True)
    
    # Submitted datetime
    sub_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)', html_content)
    if sub_match:
        data["submitted_datetime"] = sub_match.group(1)
    
    # Reason
    reason = None
    reason_cell = soup.find('td', attrs={'colspan': '2'})
    if reason_cell:
        reason_text = reason_cell.get_text(strip=True)
        # Avoid picking up numeric lines or amounts
        if reason_text and not reason_text.startswith('$') and not reason_text[0].isdigit():
            reason = reason_text
    
    # Fallback if colspan method fails
    if not reason:
        reason_match = soup.find(string=re.compile(r'(Missing|Wrong|issue)', re.I))
        if reason_match:
            reason = reason_match.strip()
    
    data["reason"] = reason

    # Build Simphony reference text
    if data["order_number"] and data["requested_datetime"] and data["reason"]:
        try:
            date_part = data["requested_datetime"].split()[0]  # e.g. "3/25/2026"
            month, day, _ = date_part.split('/')
            short_date = f"{month.zfill(2)}/{day.zfill(2)}"
            reference_text = f"OO#{data['order_number']} - {short_date} - {data['reason']}"
            data["simphony_reference"] = reference_text
            logger.info(f"Simphony Reference: {reference_text}")
        except:
            data["simphony_reference"] = None
    else:
        data["simphony_reference"] = None

    # Final logging
    logger.info("=" * 85)
    logger.info("REFUND DATA EXTRACTED")
    logger.info(f"Order Number       : {data['order_number'] or 'Not found'}")
    logger.info(f"Raw Location       : {data['raw_location'] or 'Not found'}")
    logger.info(f"Simphony LocRef    : {data['simphony_locref'] or 'Not found'}")
    logger.info(f"Refund Amount      : {data['refund_amount'] or 'Not found'}")
    logger.info(f"Requested DateTime : {data['requested_datetime'] or 'Not found'}")
    logger.info(f"Reason             : {data['reason'] or 'None'}")
    logger.info(f"Simphony Ref Text  : {data.get('simphony_reference') or 'Not built'}")
    logger.info("=" * 85)

    submit_to_simphony(data)

    return data

# ====================== SIMPHONY STS GEN2 INTEGRATION ======================
def submit_to_simphony(extracted: dict):
    if not extracted.get("simphony_locref") or not extracted.get("refund_amount"):
        logger.error("❌ Missing required data for Simphony submission.")
        return False
    
    if not extracted.get("refund_amount"):
        logger.error("❌ Missing refund_amount - cannot submit to Simphony")
        return False

    reference_text = extracted.get("simphony_reference")
    if not reference_text:
        logger.error("❌ Could not build reference text.")
        return False

    logger.info(f"Submitting to Simphony → LocRef: {extracted['simphony_locref']} | Amount: {extracted['refund_amount']} | Ref: {reference_text}")

    # ================== AUTH ==================
    try:
        id_token = get_valid_id_token()
    except Exception as e:
        logger.error(f"❌ Simphony authentication failed: {e}")
        return False

    # ================== CREATE CHECK ==================
    idempotency = str(uuid.uuid4()).replace("-", "") if USE_IDEMPOTENCY else None

    headers = {
        "Authorization": f"Bearer {id_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Simphony-OrgShortName": ORG_SHORT_NAME,
        "Simphony-LocRef": extracted["simphony_locref"],
        "Simphony-RvcRef": RVC_REF,
    }

    create_body = {
        "header": {
            "orgShortName": ORG_SHORT_NAME,
            "locRef": extracted["simphony_locref"],
            "rvcRef": int(RVC_REF),
            "orderTypeRef": ORDER_TYPE_REF,
            "checkEmployeeRef": EMPLOYEE_REF,
            "idempotencyId": idempotency,
            "status": "open",
        },
        "menuItems": [{
            "menuItemId": MENU_ITEM_REF,
            "name": MENU_ITEM_NAME,
            "quantity": 1.0,
            "unitPrice": float(extracted["refund_amount"]),
            "total": float(extracted["refund_amount"]),
            "referenceText": reference_text,
            "extensions": [],
        }],
        "tenders": [{
            "tenderId": 85,                    # Always 'Credit Card' tender
            "name": "Credit Card",
            "total": float(extracted["refund_amount"]),
            "chargedTipTotal": 0.0,
            "extensions": []
        }]
    }

    try:
        resp = requests.post(f"{API_BASE}/checks", headers=headers, json=create_body, timeout=30)
        logger.info(f"Simphony create response: {resp.status_code}")

        if resp.status_code in (200, 201):
            logger.info("✅ Simphony check created and closed successfully!")
            return True
        else:
            logger.error(f"❌ Simphony error: {resp.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Failed to call Simphony API: {e}")
        return False

# ====================== BACKGROUND POLLING ======================
def poll_emails(service):
    logger.info("Starting email polling thread...")
    while True:
        try:
            query = f'is:unread subject:"{SUBJECT_KEYWORD}"'
            results = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
            messages = results.get('messages', [])
            
            if messages:
                logger.info(f"Found {len(messages)} new refund email(s)")
            
            for msg in messages:
                msg_id = msg['id']
                msg_data = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
                
                headers = msg_data['payload'].get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                
                html_body = None
                if 'parts' in msg_data['payload']:
                    for part in msg_data['payload']['parts']:
                        if part.get('mimeType') == 'text/html':
                            html_body = part.get('body', {}).get('data')
                            break
                elif msg_data['payload'].get('mimeType') == 'text/html':
                    html_body = msg_data['payload'].get('body', {}).get('data')
                
                if not html_body:
                    continue
                
                html_decoded = base64.urlsafe_b64decode(html_body).decode('utf-8', errors='ignore')
                extract_refund_data(html_decoded, subject)
                
                service.users().messages().modify(
                    userId='me', id=msg_id, body={'removeLabelIds': ['UNREAD']}
                ).execute()
                
        except Exception as e:
            logger.error(f"❌ Error in polling loop: {e}", exc_info=True)
        
        time.sleep(POLL_INTERVAL)

# ====================== SIMPHONY AUTH FUNCTIONS - PKCE ======================
def generate_pkce_pair():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    return code_verifier, code_challenge

def perform_full_authentication():
    logger.info("Performing full PKCE authentication flow for Simphony...")
    
    session = requests.Session()
    code_verifier, code_challenge = generate_pkce_pair()

    authorize_url = f"{AUTH_BASE}/authorize"
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": "openid",
        "redirect_uri": "apiaccount://callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    try:
        resp = session.get(authorize_url, params=params, allow_redirects=False)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Failed to get authorize URL: {e}")
        raise

    signin_url = f"{AUTH_BASE}/signin"
    data = {
        "username": USERNAME,
        "password": PASSWORD,
        "orgname": ORG_SHORT_NAME
    }

    resp = session.post(signin_url, data=data)
    resp.raise_for_status()

    result = resp.json()
    if not result.get("success"):
        raise Exception(f"❌ Simphony sign-in failed: {json.dumps(result, indent=2)}")

    code = parse_qs(urlparse(result["redirectUrl"]).query)["code"][0]

    token_url = f"{AUTH_BASE}/token"
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": CLIENT_ID,
        "scope": "openid",
        "redirect_uri": "apiaccount://callback",
    }

    resp = session.post(token_url, data=token_data)
    resp.raise_for_status()

    tokens = resp.json()
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 1209600))

    # Save token
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
        logger.info("✅ Simphony authentication complete - Token saved to {TOKEN_FILE}")
    except Exception as e:
        logger.error(f"❌ Failed to save Simphony token file: {e}")

    return tokens["id_token"]

def refresh_saved_token():
    try:
        with open(TOKEN_FILE) as f:
            tokens = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("⚠️ No existing Simphony token file found.")
        return None

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    logger.info("Attempting to refresh Simphony token...")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": "openid",
        "redirect_uri": "apiaccount://callback",
    }

    resp = requests.post(f"{AUTH_BASE}/token", data=data)

    if resp.status_code != 200:
        logger.warning(f"⚠️ Simphony token refresh failed (status {resp.status_code}). Will do full auth next run.")
        return None

    new_tokens = resp.json()
    new_tokens["expires_at"] = time.time() + int(new_tokens.get("expires_in", 1209600))

    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(new_tokens, f, indent=2)
        logger.info("✅ Simphony token refreshed successfully")
    except Exception as e:
        logger.error(f"❌ Failed to save refreshed Simphony token: {e}")

    return new_tokens["id_token"]

def get_valid_id_token():
    token = refresh_saved_token()
    if token:
        return token

    logger.info("❌ No valid Simphony token found - performing full authentication")
    return perform_full_authentication()

# ====================== SIMPHONY STS GEN2 - SUBMIT FUNCTION ======================
def submit_to_simphony(extracted: dict):
    """Automatically submit refund as an Open Amount Check to Simphony STS Gen2"""
    
    if not extracted.get("simphony_locref"):
        logger.error("❌ Missing simphony_locref - cannot submit to Simphony")
        return False
    
    if not extracted.get("refund_amount"):
        logger.error("❌ Missing refund_amount - cannot submit to Simphony")
        return False
    
    reference_text = extracted.get("simphony_reference")
    if not reference_text:
        logger.error("❌ Missing simphony_reference text")
        return False

    logger.info(f"Submitting to Simphony | LocRef: {extracted['simphony_locref']} | Amount: {extracted['refund_amount']} | Ref: {reference_text}")

    try:
        id_token = get_valid_id_token() # Get valid ID token (will refresh or do full auth if needed)

        
        idempotency = str(uuid.uuid4()).replace("-", "") if USE_IDEMPOTENCY else None

        headers = {
            "Authorization": f"Bearer {id_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Simphony-OrgShortName": ORG_SHORT_NAME,
            "Simphony-LocRef": extracted["simphony_locref"],
            "Simphony-RvcRef": RVC_REF,
        }

        create_body = {
            "header": {
                "orgShortName": ORG_SHORT_NAME,
                "locRef": extracted["simphony_locref"],
                "rvcRef": int(RVC_REF),
                "orderTypeRef": ORDER_TYPE_REF,
                "checkEmployeeRef": EMPLOYEE_REF,
                "idempotencyId": idempotency,
                "status": "open",
            },
            "menuItems": [{
                "menuItemId": MENU_ITEM_REF,
                "name": MENU_ITEM_NAME,
                "quantity": 1.0,
                "unitPrice": float(extracted["refund_amount"]),
                "total": float(extracted["refund_amount"]),
                "referenceText": reference_text,
                "extensions": [],
            }],
            "tenders": [{
                "tenderId": 85,                    # Credit Card - change if needed
                "name": "Credit Card",
                "total": float(extracted["refund_amount"]),
                "chargedTipTotal": 0.0,
                "extensions": []
            }]
        }

        if VERBOSE_BODY:
            logger.debug("Simphony Request Body:")
            logger.debug(json.dumps(create_body, indent=2))

        resp = requests.post(
            f"{API_BASE}/checks", 
            headers=headers, 
            json=create_body, 
            timeout=40
        )

        logger.info(f"Simphony API Response Status: {resp.status_code}")

        if resp.status_code in (200, 201):
            data = resp.json()
            check_ref = data["header"].get("checkRef", "N/A")
            check_num = data["header"].get("checkNumber", "N/A")
            logger.info(f"✅ SUCCESS: Simphony Check #{check_num} created | CheckRef: {check_ref}")
            return True
        else:
            logger.error(f"❌ Simphony API Error: {resp.status_code} - {resp.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Failed to submit check to Simphony: {e}", exc_info=True)
        return False

# ====================== MAIN ======================
def main():
    logger.info("====== Refund Monitor Started ======")
    
    service = get_gmail_service()
    
    # Start polling in background thread
    polling_thread = Thread(target=poll_emails, args=(service,), daemon=True)
    polling_thread.start()
    
    logger.info(f"Polling thread started. Listening on port {os.environ.get('PORT', 10000)}")
    
    # Start Flask server (required by Render)
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()