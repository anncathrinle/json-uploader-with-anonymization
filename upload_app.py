import os
import uuid
import logging
import warnings
import streamlit as st
import json
import re
import io
import pandas as pd
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
st.set_page_config(page_title='Social Media Data Upload & Anonymization Tool', layout='wide')

# PII and platform-specific PII definitions
COMMON_PII = {
    'id', 'uuid', 'name', 'full_name', 'username', 'email', 'phone', 'device_id',
    'ip_address', 'location', 'birthDate', 'created_at'
}

PLATFORMS = {
    'TikTok': {'actionType', 'duration', 'timestamp', 'videoId'},
    'Instagram': {'username', 'full_name', 'biography', 'profile_picture'},
    'Facebook': {'name', 'birthday', 'gender'},
    'Twitter': {'accountId', 'username', 'text', 'created_at'},
    'Reddit': {'username', 'subreddit', 'body', 'created_utc'}
}

# Helper functions
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
            if not sk.isdigit():
                keys.add(sk)
            keys |= extract_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= extract_keys(item)
    return keys

def anonymize(obj, pii_set):
    if isinstance(obj, dict):
        return {sanitize_key(k): ('REDACTED' if sanitize_key(k) in pii_set else anonymize(v, pii_set)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [anonymize(item, pii_set) for item in obj]
    return obj

def get_or_create_folder(name, parent_id):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    resp = drive_service.files().list(q=query, fields='files(id)').execute()
    files = resp.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    return drive_service.files().create(body=meta, fields='id').execute()['id']

# Session state
st.session_state.setdefault('user_id', uuid.uuid4().hex[:8])
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('donate', False)
st.session_state.setdefault('survey_submitted', False)

user_id = st.session_state['user_id']

# Sidebar info
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Anonymous ID:** `{user_id}`")
st.sidebar.write('DISCLAIMER: Please save this ID in case you want to manage or delete your data later.')

# Ensure Drive
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Drive not configured — check secrets.')
    st.stop()

# UI: platform selection and title
platform = st.sidebar.selectbox('Platform', list(PLATFORMS.keys()))
st.title('Social Media JSON Uploader')

# File upload
uploaded = st.file_uploader(f'Upload {platform} JSON', type='json')

if not st.session_state['finalized']:
    if uploaded:
        raw = uploaded.read()
        try:
            text = raw.decode('utf-8-sig')
        except:
            text = raw.decode('utf-8', errors='replace')
        try:
            data = json.loads(text)
        except:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]

        # Consent & Redaction
        st.write('**Consents & Redaction**')
        st.session_state['donate'] = st.checkbox('Donate anonymized data')
        delete_ok = st.checkbox('Agree to deletion policy')
        voluntary = st.checkbox('I understand participation is voluntary')
        extras = st.multiselect('Additional keys to redact', sorted(extract_keys(data)))

        if delete_ok and voluntary:
            redacted = anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(extras))
            with st.expander('Preview Redacted Data'):
                st.json(redacted)
                fname = f"{user_id}_{platform}_{uploaded.name}".replace('.json.json', '.json')
                st.download_button('Download Redacted JSON', data=json.dumps(redacted, indent=2), file_name=fname)

            if st.button('Finalize and upload'):
                grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
                grp_id = get_or_create_folder(grp, ROOT_FOLDER_ID)
                usr_id = get_or_create_folder(user_id, grp_id)
                plt_id = get_or_create_folder(platform, usr_id)
                red_id = get_or_create_folder('redacted', plt_id)

                buf = io.BytesIO(json.dumps(redacted, indent=2).encode())
                drive_service.files().create(
                    body={'name': fname, 'parents': [red_id]},
                    media_body=MediaIoBaseUpload(buf, 'application/json')
                ).execute()
                st.session_state['finalized'] = True
                st.success('Uploaded redacted JSON')

                # TikTok-specific data extraction
                if platform == 'TikTok':
                    st.subheader('TikTok Usage Summary')
                    # Locate the history array in TikTok export
                    if isinstance(redacted, dict):
                        for key in ['videoPlayHistory', 'playHistory', 'watchHistory']:
                            if key in redacted and isinstance(redacted[key], list):
                                history = redacted[key]
                                break
                        else:
                            history = []
                    else:
                        history = redacted if isinstance(redacted, list) else []
                    df_tt = pd.DataFrame(history)
                    # Normalize timestamp field
                    if 'createTime' in df_tt.columns:
                        df_tt['timestamp'] = pd.to_datetime(df_tt['createTime'], errors='coerce')
                    elif 'viewTime' in df_tt.columns:
                        df_tt['timestamp'] = pd.to_datetime(df_tt['viewTime'], errors='coerce')
                    elif 'timestamp' in df_tt.columns:
                        df_tt['timestamp'] = pd.to_datetime(df_tt['timestamp'], errors='coerce')
                    # Normalize duration field
                    if 'duration' in df_tt.columns:
                        df_tt['duration'] = pd.to_numeric(df_tt['duration'], errors='coerce')
                    else:
                        df_tt['duration'] = pd.to_numeric(df_tt.get('watchDuration', pd.Series()), errors='coerce')
                    df_tt = df_tt.dropna(subset=['timestamp'])
                    if not df_tt.empty:
                        df_tt['date'] = df_tt['timestamp'].dt.date
                        total_videos = len(df_tt)
                        total_time_min = df_tt['duration'].sum() / 60
                        st.metric('Total Videos Watched', total_videos)
                        st.metric('Total Watch Time (min)', round(total_time_min, 2))
                        usage = df_tt.groupby('date')['duration'].sum().reset_index()
                        usage['minutes'] = usage['duration'] / 60
                        usage = usage.rename(columns={'date': 'Date', 'minutes': 'Minutes Watched'})
                        st.line_chart(usage.set_index('Date'))
                    else:
                        st.info('No watch events found for TikTok.')

                # Generic data analysis for all platforms
                st.subheader(f"{platform} Data Summary")
                df = pd.json_normalize(redacted)
                st.write(f"Loaded {len(df)} records with {len(df.columns)} fields.")
                st.dataframe(df.head())

                # Top fields by non-null count
                non_null_counts = df.count().sort_values(ascending=False)
                st.subheader("Top 10 Fields by Non-Null Count")
                st.bar_chart(non_null_counts.head(10))

                # Activity over time if timestamp exists
                if 'timestamp' in df.columns:
                    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
                    df['date'] = df['timestamp'].dt.date
                    activity = df.groupby('date').size().rename('count').reset_index()
                    st.subheader("Activity Over Time")
                    st.line_chart(activity.set_index('date'))









