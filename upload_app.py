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

# --- Configuration ---
logging.getLogger('streamlit.ScriptRunner').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='missing ScriptRunContext')
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# --- Google Drive Setup ---
try:
    gdrive_info = st.secrets["gdrive"]
    creds = Credentials.from_service_account_info(
        gdrive_info['service_account'],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build('drive', 'v3', credentials=creds)
    ROOT_FOLDER_ID = gdrive_info.get('folder_id')
except Exception:
    drive_service, ROOT_FOLDER_ID = None, None

if not (drive_service and ROOT_FOLDER_ID):
    st.error('Drive not configured — check secrets.')
    st.stop()

# --- Helpers ---
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']
COMMON_PII = {
    'id', 'uuid', 'name', 'full_name', 'username', 'userName',
    'email', 'emailAddress', 'phone', 'phone_number', 'telephoneNumber',
    'birthDate', 'date_of_birth',
    'device_id', 'deviceModel', 'os_version',
    'location', 'hometown', 'current_city', 'external_url',
    'created_at', 'registration_time'
}
PLATFORMS = {
    'TikTok': {'uid', 'unique_id', 'nickname', 'profilePhoto', 'profileVideo', 'bioDescription',
               'likesReceived', 'From', 'Content', 'email', 'phone_number', 'date_of_birth'},
    'Instagram': {'username', 'full_name', 'biography', 'profile_picture', 'email',
                  'phone_number', 'gender', 'birthday', 'external_url', 'account_creation_date'},
    'Facebook': {'name', 'birthday', 'gender', 'relationship_status', 'hometown',
                 'current_city', 'emails', 'phones', 'friend_count', 'friends', 'posts',
                 'story', 'comments', 'likes'},
    'Twitter': {'accountId', 'username', 'accountDisplayName', 'description', 'website',
                'location', 'avatarMediaUrl', 'headerMediaUrl', 'email',
                'in_reply_to_user_id', 'source', 'retweet_count', 'favorite_count'},
    'Reddit': {'username', 'email', 'karma', 'subreddit', 'author', 'body',
               'selftext', 'post_id', 'title', 'created_utc', 'ip_address'}
}

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
        for i in obj:
            keys |= extract_keys(i)
    return keys

def anonymize(obj, pii_set):
    if isinstance(obj, dict):
        return {sanitize_key(k): ('REDACTED' if sanitize_key(k) in pii_set else anonymize(v, pii_set))
                for k, v in obj.items()}
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
    return drive_service.files().create(body=meta, fields='id').execute()['id']

# --- Session State ---
st.session_state.setdefault('user_id', uuid.uuid4().hex[:8])
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('donate', False)
st.session_state.setdefault('survey_submitted', False)
user_id = st.session_state['user_id']

# --- Sidebar ---
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to manage or delete data later.')
platform = st.sidebar.selectbox('Platform', list(PLATFORMS.keys()))
st.title('Social Media JSON Uploader')

# --- Upload ---
uploaded = st.file_uploader(f'Upload {platform} JSON', type='json')
if not uploaded:
    st.info('Upload a JSON to begin')
    st.stop()

# --- Load & Redact ---
raw = uploaded.read()
try:
    text = raw.decode('utf-8-sig')
except:
    text = raw.decode('utf-8', errors='replace')
try:
    data = json.loads(text)
except:
    data = [json.loads(l) for l in text.splitlines()]

st.session_state['donate'] = st.checkbox('Donate anonymized data for research')
delete_ok = st.checkbox('I understand I can request deletion and have saved my ID')
if delete_ok:
    extras = st.multiselect('Additional keys to redact', sorted(extract_keys(data)))
    red = anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(set(extras)))
    st.expander('Preview Redacted Data').json(red)
    fname = f"{user_id}_{platform}_{uploaded.name}".replace('.json.json', '.json')
    st.download_button('Download Redacted JSON', data=json.dumps(red, indent=2), file_name=fname)
    if st.button('Finalize and upload'):
        grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
        grp_id = get_or_create_folder(grp, ROOT_FOLDER_ID)
        usr_id = get_or_create_folder(user_id, grp_id)
        plt_id = get_or_create_folder(platform, usr_id)
        red_id = get_or_create_folder('redacted', plt_id)
        buf = io.BytesIO(json.dumps(red, indent=2).encode())
        drive_service.files().create(
            body={'name': fname, 'parents': [red_id]},
            media_body=MediaIoBaseUpload(buf, 'application/json')
        ).execute()
        st.success('Uploaded redacted JSON')
        st.subheader(f'{platform} Analytics')

        # --- TikTok Analytics ---
        if platform == 'TikTok':
            # Comments Analysis
            comments = red.get('Comment', {}).get('Comments', {}).get('CommentsList', []) or []
            df_c = pd.DataFrame(comments)
            if not df_c.empty and 'date' in df_c.columns:
                df_c['timestamp'] = pd.to_datetime(df_c['date'], errors='coerce')
                df_c['date'] = df_c['timestamp'].dt.date
                st.metric('Total Comments', len(df_c))
                st.line_chart(df_c.groupby('date').size().rename('count'))
                df_c['length'] = df_c['comment'].str.len()
                st.metric('Avg. Comment Length', round(df_c['length'].mean(), 1))

            # Posts Analysis
            posts = red.get('Post', {}).get('Posts', {}).get('VideoList', []) or []
            df_p = pd.DataFrame(posts)
            if not df_p.empty and 'Date' in df_p.columns:
                df_p['timestamp'] = pd.to_datetime(df_p['Date'], errors='coerce')
                st.metric('Total Posts', len(df_p))
                df_p['Likes'] = pd.to_numeric(df_p['Likes'], errors='coerce')
                st.metric('Avg. Likes per Post', round(df_p['Likes'].mean(), 1))
                st.bar_chart(df_p.set_index('timestamp')['Likes'].resample('W').mean())
                st.table(df_p.nlargest(3, 'Likes')[['Date', 'Likes', 'Link']])

            # Hashtag Analysis
            hashtags = red.get('Hashtag', {}).get('HashtagList', []) or []
            if hashtags:
                df_h = pd.DataFrame(hashtags)
                top = df_h['HashtagName'].value_counts().head(5)
                st.subheader('Top Hashtags')
                st.bar_chart(top)

            # Login History Analysis
            login_history = red.get('Login History', {}).get('LoginHistoryList', []) or []
            total_logins = len(login_history)
            wifi_logins = sum(1 for e in login_history if e.get('NetworkType') == 'Wi-Fi')
            non_wifi_logins = total_logins - wifi_logins
            st.metric('Total Login Events', total_logins)
            st.metric('Wi-Fi Logins', wifi_logins)
            st.metric('Non-Wi-Fi Logins', non_wifi_logins)
            if total_logins:
                df_l = pd.DataFrame(login_history)
                date_field = 'Date' if 'Date' in df_l.columns else next((c for c in df_l.columns if c.lower() == 'date'), None)
                if date_field:
                    df_l['timestamp'] = pd.to_datetime(df_l[date_field], errors='coerce')
                    df_l['date'] = df_l['timestamp'].dt.date
                    st.subheader('Logins per Day')
                    st.bar_chart(df_l.groupby('date').size().rename('count'))

        else:
            # Generic time-series for other platforms
            for section, content in red.items():
                if isinstance(content, dict):
                    for key, block in content.items():
                        if isinstance(block, list) and block:
                            df = pd.DataFrame(block)
                            date_col = next((c for c in df.columns if c.lower() in ['date', 'timestamp']), None)
                            if date_col:
                                df['ts'] = pd.to_datetime(df[date_col], errors='coerce')
                                df = df.dropna(subset=['ts'])
                                ts = df.groupby(df['ts'].dt.date).size().rename('count')
                                st.subheader(f'{section} - {key} per Day')
                                st.line_chart(ts)

        st.info('Analysis complete. Add more modules as desired.')
else:
    st.info('Please agree to proceed.')




