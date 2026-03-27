import os
import json
import time
import logging
import base64
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from bs4 import BeautifulSoup
import re
from flask import Flask, jsonify

# OPEN https://px-olo-refunds-pos.onrender.com TO KEEP ALIVE DURING TEST PHASE

# ========================= CONFIG =========================
# Use the same broad scope as your other project
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
        "timestamp": datetime.now(UTC).isoformat()
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
    
    # Try to load token from Environment Variable (preferred)
    token_json = os.environ.get("GMAIL_TOKEN")
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
            logger.info("Loaded token from environment variable")
        except Exception as e:
            logger.error(f"Failed to parse GMAIL_TOKEN: {e}")

    service = build('gmail', 'v1', credentials=creds)
    logger.info("Gmail service initialized successfully.")
    return service

# ====================== MAIN EXTRACTOR ========================================
def extract_refund_data(html_content: str, subject: str) -> dict:
    logger.info(f"Processing email: {subject}")
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # ====================== LOCATION EXTRACTION ===============================
    location = None
    full_text = (subject + " " + html_content).lower()
    
    # Bill Gray's
    if "bill gray" in full_text:
        match = re.search(r"Bill Gray's?\s+([^\s<,]+(?:\s+[^\s<,]+)?)", subject + " " + html_content, re.I)
        if match:
            location = f"Bill Gray's {match.group(1).strip()}"
    
    # Tom Wahl's
    elif "tom wahl" in full_text:
        match = re.search(r"Tom Wahl's?\s+([^\s<,]+(?:\s+[^\s<,]+)?)", subject + " " + html_content, re.I)
        if match:
            location = f"Tom Wahl's {match.group(1).strip()}"
    
    # Flaherty's
    elif "flaherty" in full_text:
        location = "Flaherty's"
    
    # Fallback using your LOCS dictionary (location codes)
    else:
        for code, name in LOCS.items():
            if code in full_text:
                location = name
                break
    
    # Final fallback from subject line
    if not location:
        match = re.search(r"By\s+(.+?)(?:\s+|$)", subject, re.I)
        if match:
            location = match.group(1).strip()

    # ====================== BODY DATA EXTRACTION ======================
    data = {
        "order_number": None,
        "location": location,
        "refund_amount": None,
        "requested_datetime": None,
        "submitted_datetime": None,
        "reason": None,
        "processed_at": datetime.now(UTC).isoformat(),
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

    # Clean & Detailed Logging
    logger.info("=" * 85)
    logger.info("REFUND DATA EXTRACTED")
    logger.info(f"Order Number       : {data['order_number'] or 'Not found'}")
    logger.info(f"Location           : {data['location'] or 'Not found'}")
    logger.info(f"Refund Amount      : {data['refund_amount'] or 'Not found'}")
    logger.info(f"Requested DateTime : {data['requested_datetime'] or 'Not found'}")
    logger.info(f"Submitted          : {data['submitted_datetime'] or 'Not found'}")
    logger.info(f"Reason             : {data['reason'] or 'None'}")
    logger.info("=" * 85)

    return data

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
            logger.error(f"Error in polling loop: {e}", exc_info=True)
        
        time.sleep(POLL_INTERVAL)

# ====================== MAIN ======================
def main():
    logger.info("=== Multi-Brand Refund Monitor Started ===")
    
    service = get_gmail_service()
    
    # Start polling in background thread
    polling_thread = Thread(target=poll_emails, args=(service,), daemon=True)
    polling_thread.start()
    
    logger.info(f"Polling thread started. Listening on port {os.environ.get('PORT', 10000)}")
    
    # Start Flask server (required by Render)
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()