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

# Page configuration
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Helper functions for anonymization
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for pat in KEY_PATTERNS:
        if re.match(pat, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':')


def extract_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            sk = sanitize_key(k)
            if not sk.isdigit(): keys.add(sk)
            keys |= extract_keys(v)
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
    """Return the folder ID for 'name' under 'parent_id', creating it if necessary."""
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    res = drive_service.files().list(q=query, fields='files(id)').execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = drive_service.files().create(body=meta, fields='id').execute()
    return folder['id']

# Session state defaults
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('survey_submitted', False)

# Generate or retrieve anonymous user ID
if 'user_id' not in st.session_state:
    st.session_state['user_id'] = uuid.uuid4().hex[:8]
user_id = st.session_state['user_id']

# Sidebar: show Anonymous ID
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID if you wish to manage or delete your data.')

# Social media platforms and PII keys
PLATFORMS = {
    'TikTok': {'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
    'Instagram': {'biography','followers_count','following_count','media_count','profile_picture'},
    'Facebook': {'friend_count','friends','posts','story','comments','likes'},
    'Twitter': {'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
    'Reddit': {'subreddit','author','body','selftext','post_id','created_utc','title'}
}
COMMON_PII = {'username','userName','email','emailAddress','id','name','full_name','telephoneNumber','birthDate'}

# Ensure Google Drive is configured
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Google Drive not configured. Please check your Streamlit secrets.')
    st.stop()

# Sidebar: choose platform
def main():
    platform = st.sidebar.selectbox('Select Platform', list(PLATFORMS.keys()))
    st.title('Upload and Anonymize Social Media JSON')
    uploaded = st.file_uploader(f'Upload JSON for {platform}', type=['json'], accept_multiple_files=False)

    # Step 1: Finalize upload
    if not st.session_state['finalized']:
        if uploaded:
            data = _load_json(uploaded)
            # Top-level donation checkbox
            donate = st.checkbox('(Optional) I want to donate my data for research purposes.')
            delete_ok = st.checkbox('I understand I can request deletion of my data at any time.')
            voluntary = st.checkbox('This is voluntary, independent of ICS3; no grade impact.')
            extras = st.multiselect('Select additional keys to redact', sorted(extract_keys(data)))

            if delete_ok and voluntary:
                redacted = anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(extras))
                with st.expander('Preview Anonymized Data'):
                    st.json(redacted)
                # Allow downloading of redacted JSON
                st.download_button(
                    'Download Redacted JSON',
                    data=json.dumps(redacted, indent=2),
                    file_name=filename,
                    mime='application/json'
                )
                base, _ = os.path.splitext(uploaded.name)
                filename = f"{user_id}_{platform}_{base}.json"
                if st.button(f'Finalize and send {filename}'):
                    # Determine root folder based on donation choice
                    group_name = 'research_donations' if donate else 'non_donations'
                    group_folder = get_or_create_folder(group_name, ROOT_FOLDER_ID)
                    user_folder = get_or_create_folder(user_id, group_folder)
                    plat_folder = get_or_create_folder(platform, user_folder)
                    red_folder = get_or_create_folder('redacted', plat_folder)

                    buf = io.BytesIO(json.dumps(redacted, indent=2).encode('utf-8'))
                    drive_service.files().create(
                        body={'name': filename, 'parents': [red_folder]},
                        media_body=MediaIoBaseUpload(buf, mimetype='application/json')
                    ).execute()
                    st.session_state['finalized'] = True
                    st.success(f'Uploaded {filename} (ID: {user_id})')
            else:
                st.info('Please agree to the required consents to proceed.')
        else:
            st.info('Upload a JSON file to begin.')
    # Step 2: Survey or skip
    if st.session_state['finalized'] and not st.session_state['survey_submitted']:
        choice = st.radio(
            'Would you like to answer optional research questions? (Voluntary, no grade impact)',
            ['Yes', 'No', 'I have already answered']
        )
        if choice == 'Yes':
            _run_survey()
        else:
            st.session_state['survey_submitted'] = True
    # Step 3: Thank-you page
    if st.session_state['survey_submitted']:
        st.subheader('Thank you! Your response has been recorded.')
        st.write('You can upload data for other platforms using the sidebar.')

# Utility to load JSON robustly
def _load_json(uploaded_file):
    raw = uploaded_file.read()
    try:
        text = raw.decode('utf-8-sig')
    except:
        text = raw.decode('utf-8', errors='replace')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line: continue
            try:
                items.append(json.loads(line))
            except:
                continue
        return items

# Survey component

def _run_survey():
    st.markdown('*This survey is voluntary and independent of ICS3; it will not affect your grade or standing.*')
    st.subheader('Optional Research Questions')
    q1 = st.radio('Have you ever been active in a social movement?', ['Yes', 'No'], key='q1')
    sm_from = sm_to = sm_kind = ''
    if q1 == 'Yes':
        sm_from = str(st.date_input('If yes, from when?', key='sm_from'))
        sm_to = str(st.date_input('If yes, until when?', key='sm_to'))
        sm_kind = st.text_input('What kind of movement?', key='sm_kind')
    q2 = st.radio('Have you ever participated in a protest?', ['Yes', 'No'], key='q2')
    p_first = p_last = p_reason = ''
    if q2 == 'Yes':
        p_first = str(st.date_input('When was your first protest?', key='p_first'))
        p_last = str(st.date_input('When was your last protest?', key='p_last'))
        p_reason = st.text_area('Why did you decide to join or stop protesting?', key='p_reason')
    q3 = st.text_area('Is there any post you particularly remember? (optional)', key='q3')
    if st.button('Submit Survey Responses'):
        survey = {
            'anonymous_id': user_id,
            'platform': st.session_state.get('platform'),
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
        # Create survey folder under same group/user
de"}
]}





