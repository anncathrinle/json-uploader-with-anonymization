import uuid
import logging
import warnings
import streamlit as st
import json
import re
import io
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Load Google Drive settings from Streamlit Secrets
try:
    GDRIVE_INFO = st.secrets["gdrive"]
except KeyError:
    GDRIVE_INFO = None

# Initialize Google Drive client if credentials provided
drive_service = None
DRIVE_FOLDER_ID = None
if GDRIVE_INFO:
    SCOPES = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        GDRIVE_INFO["service_account"], scopes=SCOPES
    )
    drive_service = build("drive", "v3", credentials=creds)
    DRIVE_FOLDER_ID = GDRIVE_INFO.get("folder_id")

# Suppress extraneous Streamlit warnings
logging.getLogger('streamlit.ScriptRunner').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='missing ScriptRunContext')

# Page configuration (must be first)
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Helper functions for key sanitization and anonymization
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for p in KEY_PATTERNS:
        if re.match(p, k, flags=re.IGNORECASE):
            return k.split(':', 1)[0].title()
    return k.rstrip(':')

def extract_keys(o):
    keys = set()
    if isinstance(o, dict):
        for kk, vv in o.items():
            sk = sanitize_key(kk)
            if not sk.isdigit():
                keys.add(sk)
            keys |= extract_keys(vv)
    elif isinstance(o, list):
        for item in o:
            keys |= extract_keys(item)
    return keys

def anonymize(o, pii_set):
    if isinstance(o, dict):
        return {
            sanitize_key(kk): (
                'REDACTED' if sanitize_key(kk) in pii_set else anonymize(vv, pii_set)
            )
            for kk, vv in o.items()
        }
    if isinstance(o, list):
        return [anonymize(item, pii_set) for item in o]
    return o

# --- Upload Mode Only ---
if 'user_id' not in st.session_state:
    st.session_state.user_id = uuid.uuid4().hex[:8]
user_id = st.session_state.user_id

st.sidebar.markdown('---')
st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID; you will need it for any future requests.')

# Define PII sets
COMMON = {
    'username','userName','email','emailAddress','id','name',
    'full_name','telephoneNumber','birthDate'
}
PLATFORM = {
    'TikTok': {'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
    'Instagram': {'biography','followers_count','following_count','media_count','profile_picture'},
    'Facebook': {'friend_count','friends','posts','story','comments','likes'},
    'Twitter': {'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
    'Reddit': {'subreddit','author','body','selftext','post_id','created_utc','title'}
}

st.title('Upload and Anonymize Social Media JSON')
platform = st.sidebar.selectbox('Select Platform', options=list(PLATFORM.keys()))
st.sidebar.markdown(f'**Platform:** {platform}')

uploaded_files = st.file_uploader(
    f'Upload JSON file(s) for {platform}',
    type=['json'],
    accept_multiple_files=True
)

if uploaded_files:
    if not (drive_service and DRIVE_FOLDER_ID):
        st.error('Google Drive not configured. Please check your secrets.')
    else:
        for f in uploaded_files:
            st.subheader(f.name)
            raw = f.read()

            # Parse JSON content
            try:
                text = raw.decode('utf-8-sig')
            except:
                text = raw.decode('utf-8', errors='replace')
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]

            # Let user choose extra keys to redact
            pii_base = COMMON.union(PLATFORM[platform])
            extras = st.multiselect(
                'Select additional keys to redact',
                options=sorted(extract_keys(data))
            )
            pii_set = pii_base.union(extras)

            # Anonymize data
            redacted = anonymize(data, pii_set)

            # Upload ONLY the redacted JSON to Drive
            filename = f"{user_id}_{platform}_{f.name}"
            redact_path = f"{user_id}/{platform}/redacted/{filename}.json"
            fr = io.BytesIO(json.dumps(redacted, indent=2).encode('utf-8'))
            media = MediaIoBaseUpload(fr, mimetype='application/json')
            drive_service.files().create(
                body={
                    'name': redact_path,
                    'parents': [DRIVE_FOLDER_ID]
                },
                media_body=media
            ).execute()

            # Preview & consents
            with st.expander('Preview Anonymized Data'):
                st.json(redacted)
            c1 = st.checkbox(
                'My participation is voluntary and does not affect my grade or standing in ICS3.'
            )
            c2 = st.checkbox(
                'I agree to the use of the anonymized data for research purposes.'
            )
            c3 = st.checkbox(
                'I can request deletion of my data at any time.'
            )
            if c1 and c2 and c3:
                st.download_button(
                    'Download Anonymized JSON',
                    data=json.dumps(redacted, indent=2),
                    file_name=filename + '.json'
                )
                if st.button(f'Finalize and send {filename}.json'):
                    st.success(
                        f'Your file {filename}.json has been finalized (ID: {user_id})'
                    )
else:
    st.info('Please upload JSON files to begin.')
