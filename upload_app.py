import os
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

# Load Google Drive settings
try:
    gdrive_info = st.secrets["gdrive"]
except Exception:
    gdrive_info = None

drive_service = None
ROOT_FOLDER_ID = None
if gdrive_info:
    creds = Credentials.from_service_account_info(
        gdrive_info["service_account"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build("drive", "v3", credentials=creds)
    ROOT_FOLDER_ID = gdrive_info.get("folder_id")

# Suppress Streamlit warnings
logging.getLogger('streamlit.ScriptRunner').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='missing ScriptRunContext')

# Page config
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Helper functions
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for p in KEY_PATTERNS:
        if re.match(p, k, flags=re.IGNORECASE):
            return k.split(':', 1)[0].title()
    return k.rstrip(':')

def extract_keys(o):
    s = set()
    if isinstance(o, dict):
        for kk, vv in o.items():
            sk = sanitize_key(kk)
            if not sk.isdigit():
                s.add(sk)
            s |= extract_keys(vv)
    elif isinstance(o, list):
        for i in o:
            s |= extract_keys(i)
    return s

def anonymize(o, pii_set):
    if isinstance(o, dict):
        return {
            sanitize_key(k): (
                'REDACTED' if sanitize_key(k) in pii_set else anonymize(v, pii_set)
            )
            for k, v in o.items()
        }
    if isinstance(o, list):
        return [anonymize(i, pii_set) for i in o]
    return o

def get_or_create_folder(name, parent_id):
    query = (
        f"mimeType='application/vnd.google-apps.folder' and name='{name}' "
        f"and '{parent_id}' in parents"
    )
    resp = drive_service.files().list(q=query, fields='files(id)').execute()
    files = resp.get('files', [])
    if files:
        return files[0]['id']
    meta = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(body=meta, fields='id').execute()
    return folder['id']

# Initialize session state
for key, val in {
    'finalized': False,
    'survey_choice': None,
    'survey_submitted': False
}.items():
    st.session_state.setdefault(key, val)

# Anonymous user ID
def get_user_id():
    if 'user_id' not in st.session_state:
        st.session_state.user_id = uuid.uuid4().hex[:8]
    return st.session_state.user_id
user_id = get_user_id()

# Sidebar info
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to manage or delete your data later.')

# PII definitions
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

# Main UI
st.title('Upload and Anonymize Social Media JSON')
platform = st.sidebar.selectbox('Select Platform', list(PLATFORM.keys()))
st.sidebar.markdown(f'**Platform:** {platform}')
uploads = st.file_uploader(
    f'Upload JSON for {platform}', type=['json'], accept_multiple_files=True
)

# Ensure Drive configured
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Google Drive not configured. Please check your secrets.')
    st.stop()

# Create Drive folder hierarchy
user_folder = get_or_create_folder(user_id, ROOT_FOLDER_ID)
plat_folder = get_or_create_folder(platform, user_folder)
redact_folder = get_or_create_folder('redacted', plat_folder)
survey_folder = get_or_create_folder('survey', user_folder)

# Step 1: Upload & finalize redacted JSON
if not st.session_state.finalized:
    if uploads:
        f = uploads[0]
        st.subheader(f.name)
        raw = f.read()
        try:
            txt = raw.decode('utf-8-sig')
        except:
            txt = raw.decode('utf-8', errors='replace')
        try:
            data = json.loads(txt)
        except:
            data = [json.loads(line) for line in txt.splitlines() if line.strip()]

        # Consent checkboxes
        c1 = st.checkbox('I donate my anonymized data for research purposes.')
        c2 = st.checkbox('I agree to the use of anonymized data for research purposes.')
        c3 = st.checkbox('I understand I can request deletion at any time.')
        c4 = st.checkbox('I understand this is independent of ICS3 and voluntary; no grade impact.')
        extras = st.multiselect(
            'Select additional keys to redact', sorted(extract_keys(data))
        )

        if c1 and c2 and c3 and c4:
            redacted = anonymize(
                data, COMMON.union(PLATFORM[platform]).union(extras)
            )
            with st.expander('Preview Anonymized Data'):
                st.json(redacted)
            base, _ = os.path.splitext(f.name)
            fname = f"{user_id}_{platform}_{base}.json"
            if st.button(f'Finalize and send {fname}'):
                # Upload to Drive
                buf = io.BytesIO(json.dumps(redacted, indent=2).encode('utf-8'))
                drive_service.files().create(
                    body={'name': fname, 'parents': [redact_folder]},
                    media_body=MediaIoBaseUpload(buf, mimetype='application/json')
                ).execute()
                st.session_state.finalized = True
                st.success(f'Uploaded {fname} (ID: {user_id})')
        else:
    # No or already answered
    st.subheader('Thank you! Your response has been recorded.')

# Step 3: Final message after survey or skip
if st.session_state.survey_submitted or (
    st.session_state.survey_choice in ['No', 'I have already answered']
):
    st.subheader('Thank you! Your response has been recorded.')
    st.write(
        'If you would like, you can add data from other platforms using the navigation menu to the left.'
    )
if st.session_state.survey_submitted or (
    st.session_state.survey_choice in ['No', 'I have already answered']
):
    st.subheader('Thank you! Your response has been recorded.')
    st.write(
        'If you would like, you can add data from other platforms using the navigation menu to the left.'
    )
