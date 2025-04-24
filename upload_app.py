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
gdrive_info = st.secrets.get("gdrive") if hasattr(st, 'secrets') else None

drive_service = None
ROOT_FOLDER_ID = None
if gdrive_info:
    SCOPES = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        gdrive_info["service_account"], scopes=SCOPES
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

def extract_keys(o):
    s = set()
    if isinstance(o, dict):
        for kk, vv in o.items():
            sk = sanitize_key(kk)
            if not sk.isdigit(): s.add(sk)
            s |= extract_keys(vv)
    elif isinstance(o, list):
        for i in o: s |= extract_keys(i)
    return s

def anonymize(o, pii_set):
    if isinstance(o, dict):
        return {sanitize_key(k): ('REDACTED' if sanitize_key(k) in pii_set else anonymize(v, pii_set)) for k, v in o.items()}
    if isinstance(o, list):
        return [anonymize(i, pii_set) for i in o]
    return o

def get_or_create_folder(name, parent_id):
    query = (f"mimeType='application/vnd.google-apps.folder' and name='{name}' "
             f"and '{parent_id}' in parents")
    resp = drive_service.files().list(q=query, fields='files(id)').execute()
    files = resp.get('files', [])
    if files:
        return files[0]['id']
    metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = drive_service.files().create(body=metadata, fields='id').execute()
    return folder['id']

# Session state flags
if 'finalized' not in st.session_state:
    st.session_state.finalized = False
if 'survey_submitted' not in st.session_state:
    st.session_state.survey_submitted = False

# Anonymous user ID
def get_user_id():
    if 'user_id' not in st.session_state:
        st.session_state.user_id = uuid.uuid4().hex[:8]
    return st.session_state.user_id
user_id = get_user_id()

# Sidebar
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to delete or manage your data later.')

# PII definitions
COMMON = {'username','userName','email','emailAddress','id','name','full_name','telephoneNumber','birthDate'}
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
uploaded = st.file_uploader(f'Upload JSON for {platform}', type=['json'], accept_multiple_files=True)

if drive_service and ROOT_FOLDER_ID:
    # Prepare Drive folders
    user_folder = get_or_create_folder(user_id, ROOT_FOLDER_ID)
    plat_folder = get_or_create_folder(platform, user_folder)
    redact_folder = get_or_create_folder('redacted', plat_folder)
    survey_folder = get_or_create_folder('survey', user_folder)

    if not st.session_state.finalized:
        # Show upload and finalize
        if uploaded:
            f = uploaded[0]
            st.subheader(f.name)
            raw = f.read()
            try:
                text = raw.decode('utf-8-sig')
            except:
                text = raw.decode('utf-8', errors='replace')
            try:
                data = json.loads(text)
            except:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]

            # Consent
            c1 = st.checkbox('Donate anonymized data for research purposes')
            c2 = st.checkbox('Agree to research use of anonymized data')
            c3 = st.checkbox('Can request deletion of my data at any time')
            c4 = st.checkbox('I understand this is independent from ICS3 and voluntary; no grade impact')
            extras = st.multiselect('Select additional keys to redact', sorted(extract_keys(data)))

            if c1 and c2 and c3 and c4:
                redacted = anonymize(data, COMMON.union(PLATFORM[platform]).union(extras))
                with st.expander('Preview Anonymized Data'):
                    st.json(redacted)
                base_name, _ = os.path.splitext(f.name)
                filename = f"{user_id}_{platform}_{base_name}"
                if st.button(f'Finalize and send {filename}.json'):
                    # upload
                    fr = io.BytesIO(json.dumps(redacted, indent=2).encode('utf-8'))
                    drive_service.files().create(
                        body={'name': filename + '.json', 'parents': [redact_folder]},
                        media_body=MediaIoBaseUpload(fr, mimetype='application/json')
                    ).execute()
                    st.session_state.finalized = True
                    st.success(f'Uploaded {filename}.json to Google Drive (ID: {user_id})')
            else:
                st.info('Please check all consent boxes to proceed.')
    else:
        # After finalize, show survey or thank you
        if not st.session_state.survey_submitted:
            choice = st.radio(
                'Would you like to answer optional research questions? (Voluntary, no grade impact)',
                ['Yes', 'No', 'I have already answered']
            )
            if choice == 'Yes':
                st.markdown('*This survey is voluntary and independent of ICS3; it will not affect your grade or standing.*')
                st.subheader('Optional Research Questions')
                q1 = st.radio('Have you ever been active in a social movement?', ['Yes', 'No'])
                sm_from = sm_to = sm_kind = None
                if q1 == 'Yes':
                    sm_from = st.date_input('If yes, from when?')
                    sm_to = st.date_input('If yes, until when?')
                    sm_kind = st.text_input('What kind of movement?')
                q2 = st.radio('Have you ever participated in a protest?', ['Yes', 'No'])
                p_first = p_last = p_reason = None
                if q2 == 'Yes':
                    p_first = st.date_input('When was your first protest?')
                    p_last = st.date_input('When was your last protest?')
                    p_reason = st.text_area('Why did you decide to join or stop protesting?')
                q3 = st.text_area('Is there any post you particularly remember? (optional)')
                if st.button('Submit Survey'):
                    survey = {
                        'anonymous_id': user_id,
                        'platform': platform,
                        'active_movement': q1,
                        'movement_from': str(sm_from) if sm_from else '',
                        'movement_until': str(sm_to) if sm_to else '',
                        'movement_kind': sm_kind or '',
                        'participated_protest': q2,
                        'first_protest': str(p_first) if p_first else '',
                        'last_protest': str(p_last) if p_last else '',
                        'protest_reason': p_reason or '',
                        'remembered_post': q3 or ''
                    }
                    sr = io.BytesIO(json.dumps(survey, indent=2).encode('utf-8'))
                    drive_service.files().create(
                        body={'name': f'{user_id}_survey.json', 'parents': [survey_folder]},
                        media_body=MediaIoBaseUpload(sr, mimetype='application/json')
                    ).execute()
                    st.session_state.survey_submitted = True
                    st.success('Your survey responses have been saved. Thank you!')
            else:
                st.session_state.survey_submitted = True
        if st.session_state.survey_submitted:
            st.subheader('Thank you! Your response has been recorded.')
            st.write('If you would like, you can add data from other platforms using the navigation menu to the left.')
else:
    if not uploaded:
        st.info('Please upload JSON files to begin.')
    else:
        st.error('Google Drive not configured. Please check your secrets.')
