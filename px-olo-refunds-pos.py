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

# ========================= CONFIG =========================
# Use the same broad scope as your other project
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']   # ← Full access (matches your other project)

POLL_INTERVAL = 60 # in seconds
SUBJECT_KEYWORD = "Your Refund Has Been Submitted By"

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
    logger.debug("Initializing Gmail service...")
    creds = None
    
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing access token...")
            creds.refresh(Request())
        else:
            logger.error("No valid token found. Please run the script locally first to generate token.json")
            raise Exception("Missing or invalid token.json - Run locally to authorize")
        
        # Save updated token
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    service = build('gmail', 'v1', credentials=creds)
    logger.info("Gmail service initialized successfully (using gmail.modify scope)")
    return service

# ====================== DATA EXTRACTOR ======================
def extract_refund_data(html_content: str) -> dict:
    logger.debug("Parsing HTML content...")
    soup = BeautifulSoup(html_content, 'html.parser')
    
    data = {
        "order_number": None,
        "location": None,
        "refund_amount": None,
        "requested_datetime": None,
        "submitted_datetime": None,
        "reason": None,
        "customer_name": None,
        "processed_at": datetime.utcnow().isoformat()
    }
    
    # Location
    loc_match = re.search(r"Your Refund Has Been Submitted By (.+?)(?=\s|$|<)", html_content, re.I)
    if loc_match:
        data["location"] = loc_match.group(1).strip()
    
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
    
    # Optional fields
    sub_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)', html_content)
    if sub_match:
        data["submitted_datetime"] = sub_match.group(1)
    
    reason_match = re.search(r'(Missing|quality issue)', html_content, re.I)
    if reason_match:
        data["reason"] = reason_match.group(1).strip()
    
    logger.info(f"Successfully extracted data - Order #{data['order_number']} | Location: {data['location']} | Amount: {data['refund_amount']}")
    return data

# ====================== MAIN LOOP ======================
def main():
    logger.info("=== Bill Gray's Refund Email Monitor Started ===")
    logger.info(f"Monitoring for subject containing: '{SUBJECT_KEYWORD}'")
    
    service = get_gmail_service()
    
    while True:
        try:
            query = f'is:unread subject:"{SUBJECT_KEYWORD}"'
            results = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
            messages = results.get('messages', [])
            
            logger.info(f"Found {len(messages)} new matching refund email(s)")
            
            for msg in messages:
                msg_id = msg['id']
                
                msg_data = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
                
                # Get subject for logging
                headers = msg_data['payload'].get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                logger.info(f"Processing email: {subject}")
                
                # Extract HTML body
                html_body = None
                if 'parts' in msg_data['payload']:
                    for part in msg_data['payload']['parts']:
                        if part.get('mimeType') == 'text/html':
                            html_body = part.get('body', {}).get('data')
                            break
                elif msg_data['payload'].get('mimeType') == 'text/html':
                    html_body = msg_data['payload'].get('body', {}).get('data')
                
                if not html_body:
                    logger.warning("No HTML body found in email")
                    continue
                
                html_decoded = base64.urlsafe_b64decode(html_body).decode('utf-8', errors='ignore')
                extracted = extract_refund_data(html_decoded)
                
                # === OUTPUT THE RESULT ===
                print("\n" + "="*80)
                print("REFUND EMAIL SUCCESSFULLY PROCESSED")
                print(json.dumps(extracted, indent=2))
                print("="*80 + "\n")
                
                # Mark as read
                service.users().messages().modify(
                    userId='me',
                    id=msg_id,
                    body={'removeLabelIds': ['UNREAD']}
                ).execute()
                logger.info(f"Marked message {msg_id} as read")
                
        except HttpError as error:
            logger.error(f"Gmail API error: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
        
        logger.debug(f"Sleeping for {POLL_INTERVAL} seconds...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()