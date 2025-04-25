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

# Initialize Google Drive client
drive_service = None
ROOT_FOLDER_ID = None
if gdrive_info:
    creds = Credentials.from_service_account_info(
        gdrive_info["service_account"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build("drive", "v3", credentials=creds)
    ROOT_FOLDER_ID = gdrive_info.get("folder_id")

# Suppress warnings
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

def extract_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for kk, vv in obj.items():
            sk = sanitize_key(kk)
            if not sk.isdigit(): keys.add(sk)
            keys |= extract_keys(vv)
    elif isinstance(obj, list):
        for item in obj: keys |= extract_keys(item)
    return keys

def anonymize(obj, pii_set):
    if isinstance(obj, dict):
        return {sanitize_key(k): ('REDACTED' if sanitize_key(k) in pii_set else anonymize(v, pii_set)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [anonymize(i, pii_set) for i in obj]
    return obj

def get_or_create_folder(name, parent_id):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    resp = drive_service.files().list(q=query, fields='files(id)').execute()
    files = resp.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = drive_service.files().create(body=meta, fields='id').execute()
    return folder['id']

# Session state defaults
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('survey_submitted', False)

# Anonymous user ID
if 'user_id' not in st.session_state:
    st.session_state['user_id'] = uuid.uuid4().hex[:8]
user_id = st.session_state['user_id']

# Sidebar info
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to manage or delete your data later.')

# PII definitions
COMMON = {'username','userName','email','emailAddress','id','name','full_name','telephoneNumber','birthDate'}
PLATFORM = {
    'TikTok': {'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
    'Instagram': {'biography','followers_count','following_count','media_count','profile_picture'},
    'Facebook': {'friend_count','friends','posts','story','comments','likes'},
    'Twitter': {'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
    'Reddit': {'subreddit','author','body','selftext','post_id','created_utc','title'}
}

# Ensure Drive configured
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Google Drive not configured. Please check your secrets.')
    st.stop()

# Sidebar platform selection
platform = st.sidebar.selectbox('Select Platform', list(PLATFORM.keys()))

# Create Drive folder hierarchy
user_folder = get_or_create_folder(user_id, ROOT_FOLDER_ID)
plat_folder = get_or_create_folder(platform, user_folder)
redact_folder = get_or_create_folder('redacted', plat_folder)
survey_folder = get_or_create_folder('survey', user_folder)

# Main UI Title
st.title('Upload and Anonymize Social Media JSON')

# File uploader
uploads = st.file_uploader(f'Upload JSON for {platform}', type=['json'], accept_multiple_files=True)

# Step 1: Upload & Finalize
if not st.session_state['finalized']:
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
        extras = st.multiselect('Select additional keys to redact', sorted(extract_keys(data)))

        if c1 and c2 and c3 and c4:
            redacted = anonymize(data, COMMON.union(PLATFORM[platform]).union(extras))
            with st.expander('Preview Anonymized Data'):
                st.json(redacted)
            base, _ = os.path.splitext(f.name)
            fname = f"{user_id}_{platform}_{base}.json"
            if st.button(f'Finalize and send {fname}'):
                buf = io.BytesIO(json.dumps(redacted, indent=2).encode('utf-8'))
                drive_service.files().create(
                    body={'name': fname, 'parents': [redact_folder]},
                    media_body=MediaIoBaseUpload(buf, mimetype='application/json')
                ).execute()
                st.session_state['finalized'] = True
                st.success(f'Uploaded {fname} to Google Drive (ID: {user_id})')
    else:
        st.info('Please upload a JSON file to begin.')

# Step 2: Survey or skip
if st.session_state['finalized'] and not st.session_state['survey_submitted']:
    choice = st.radio(
        'Would you like to answer optional research questions? (Voluntary, no grade impact)',
        ['Yes', 'No', 'I have already answered']
    )
    if choice == 'Yes':
        st.markdown('*This survey is voluntary and independent of ICS3; it will not affect your grade or standing.*')
        st.subheader('Optional Research Questions')
        q1 = st.radio('Have you ever been active in a social movement?', ['Yes', 'No'])
        sm_from = sm_to = sm_kind = ''
        if q1 == 'Yes':
            sm_from = str(st.date_input('If yes, from when?'))
            sm_to = str(st.date_input('If yes, until when?'))
            sm_kind = st.text_input('What kind of movement?')
        q2 = st.radio('Have you ever participated in a protest?', ['Yes', 'No'])
        p_first = p_last = p_reason = ''
        if q2 == 'Yes':
            p_first = str(st.date_input('When was your first protest?'))
            p_last = str(st.date_input('When was your last protest?'))
            p_reason = st.text_area('Why did you decide to join or stop protesting?')
        q3 = st.text_area('Is there any post you particularly remember? (optional)')
        if st.button('Submit Survey Responses'):
            survey = {
                'anonymous_id': user_id,
                'platform': platform,
                'active_movement': q1,
                'movement_from': sm_from,
                'movement_until': sm_to,
                'movement_kind': sm_kind,
                'participated_protest': q2,
                'first_protest': p_first,
                'last_protest': p_last,
                'protest_reason': p_reason,
                'remembered_post': q3
            }
            buf = io.BytesIO(json.dumps(survey, indent=2).encode('utf-8'))
            drive_service.files().create(
                body={'name': f'{user_id}_survey.json', 'parents': [survey_folder]},
                media_body=MediaIoBaseUpload(buf, mimetype='application/json')
            ).execute()
            st.session_state['survey_submitted'] = True
            st.success('Your survey responses have been saved.')
    else:
        st.session_state['survey_submitted'] = True

# Step 3: Thank-you and navigation
if st.session_state['survey_submitted']:
    st.subheader('Thank you! Your response has been recorded.')
    st.write('If you would like, you can add data from other platforms using the sidebar.')

