import os
import uuid
import logging
import warnings
import streamlit as st
import json
import re
import io
import pandas as pd
import altair as alt
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
st.set_page_config(page_title='Social media data upload & anonymization tool', layout='wide')

# Helper functions
def sanitize_key(k):
    patterns = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']
    for pat in patterns:
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
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    resp = drive_service.files().list(q=query, fields='files(id)').execute()
    files = resp.get('files', [])
    if files: return files[0]['id']
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    return drive_service.files().create(body=meta, fields='id').execute()['id']

# Session state
st.session_state.setdefault('user_id', uuid.uuid4().hex[:8])
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('donate', False)
st.session_state.setdefault('survey_submitted', False)

user_id = st.session_state['user_id']

# Sidebar
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to manage your data; only this ID links uploads.')

# PII definitions
COMMON_PII = {'id','uuid','name','full_name','username','userName','email','emailAddress','phone','phone_number','telephoneNumber','birthDate','date_of_birth','ip_address','device_id','deviceModel','os_version','last_login_ip','location','hometown','current_city','external_url','created_at','registration_time'}
PLATFORMS = {
    'TikTok': {'uid','unique_id','nickname','profilePhoto','profileVideo','bioDescription','likesReceived','From','Content','email','phone_number','date_of_birth','ip_address'},
    'Instagram': {'username','full_name','biography','profile_picture','email','phone_number','gender','birthday','external_url','account_creation_date'},
    'Facebook': {'name','birthday','gender','relationship_status','hometown','current_city','emails','phones','friend_count','friends','posts','story','comments','likes'},
    'Twitter': {'accountId','username','accountDisplayName','description','website','location','avatarMediaUrl','headerMediaUrl','email','in_reply_to_user_id','source','retweet_count','favorite_count'},
    'Reddit': {'username','email','karma','subreddit','author','body','selftext','post_id','title','created_utc','ip_address'}
}

# Ensure Drive configured
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Drive not configured — check secrets')
    st.stop()

# UI
title = 'Social Media JSON Uploader'
st.title(title)
platform = st.sidebar.selectbox('Platform', list(PLATFORMS.keys()))

uploaded = st.file_uploader(f'Upload {platform} JSON', type='json')

if uploaded and not st.session_state['finalized']:
    # Load JSON
    raw = uploaded.read()
    try: text = raw.decode('utf-8-sig')
    except: text = raw.decode('utf-8', errors='replace')
    try: data = json.loads(text)
    except: data = [json.loads(l) for l in text.splitlines() if l.strip()]

    # --- REDACTION ---
    st.write('**Consents & Redaction**')
    st.session_state['donate'] = st.checkbox('Donate anonymized data?')
    ok = st.checkbox('I acknowledge I can request deletion and have saved my ID')
    extras = st.multiselect('Additional keys to redact', sorted(extract_keys(data)))
    if ok:
        red = anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(extras))
        # Preview & download redacted
        with st.expander('Preview Redacted JSON'):
            st.json(red)
            fname = f"{user_id}_{platform}_{uploaded.name}".replace('.json.json','.json')
            st.download_button('Download Redacted JSON', data=json.dumps(red, indent=2), file_name=fname)

        # --- VISUALIZATIONS AFTER REDACTION ---
        df = pd.json_normalize(red if isinstance(red, list) else [red])
        def render(date_col, ttl):
            if date_col in df.columns:
                ts = pd.to_datetime(df[date_col], unit='s', errors='coerce') if df[date_col].dtype=='int64' else pd.to_datetime(df[date_col], errors='coerce')
                daily = ts.dropna().dt.date.value_counts().sort_index().reset_index()
                daily.columns = ['day','count']
                chart = alt.Chart(daily).mark_line(point=True).encode(x='day:T', y='count:Q', tooltip=['day','count']).properties(title=ttl)
                st.altair_chart(chart, use_container_width=True)
        st.subheader('Your Activity (Redacted Data)')
        if platform=='TikTok': render('watched_at','TikTok Videos Watched per Day')
        if platform=='Instagram': render('timestamp','Instagram Media per Day')
        if platform=='Facebook': render('created_time','Facebook Posts per Day')
        if platform=='Twitter': render('created_at','Tweets per Day')
        if platform=='Reddit': render('created_utc','Reddit Activity per Day')

        # Finalize & upload
        if st.button('Finalize & Upload'):
            grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
            ids = [get_or_create_folder(x, ROOT_FOLDER_ID) for x in (grp, user_id, platform, 'redacted')]
            buf = io.BytesIO(json.dumps(red, indent=2).encode())
            drive_service.files().create(body={'name': fname, 'parents': [ids[-1]]}, media_body=MediaIoBaseUpload(buf, 'application/json')).execute()
            st.success('Uploaded successfully')
            st.session_state['finalized'] = True
    else:
        st.info('Please agree to proceed')
else:
    if not uploaded:
        st.info('Upload a JSON file to begin')

# Survey (unchanged)
if st.session_state['finalized'] and not st.session_state['survey_submitted']:
    choice = st.radio('Answer optional research questions?',['Yes','No','Already did'])
    if choice == 'Yes':
        q1 = st.radio('Active in a movement?',['Yes','No'])
        smf = smt = smtk = ''
        if q1 == 'Yes': smf = str(st.date_input('From')); smt = str(st.date_input('Until')); smtk = st.text_input('Movement?')
        q2 = st.radio('Participated protest?',['Yes','No'])
        pf = pl = pr = ''
        if q2 == 'Yes': pf = str(st.date_input('First?')); pl = str(st.date_input('Last?')); pr = st.text_area('Why?')
        q3 = st.text_area('Any posts you remember?')
        if st.button('Submit'): 
            surv = {'anonymous_id': user_id, 'platform': platform, 'active_movement': q1, 'movement_from': smf, 'movement_until': smt, 'movement_kind': smtk, 'participated_protest': q2, 'first_protest': pf, 'last_protest': pl, 'protest_reason': pr, 'remembered_post': q3}
            ids = [get_or_create_folder(x, ROOT_FOLDER_ID) for x in ('research_donations' if st.session_state['donate'] else 'non_donations', user_id, 'survey')]
            buf = io.BytesIO(json.dumps(surv, indent=2).encode())
            drive_service.files().create(body={'name': f'{user_id}_survey.json', 'parents': [ids[-1]]}, media_body=MediaIoBaseUpload(buf, 'application/json')).execute()
            st.success('Survey saved')
            st.session_state['survey_submitted'] = True
    else:
        st.session_state['survey_submitted'] = True

if st.session_state['survey_submitted']:
    st.subheader('Thank you! You can upload another platform via the sidebar.')









