import os
import uuid
import logging
import warnings
import streamlit as st
import json
import re
import io
import requests  # new import for IP geolocation
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
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for pat in KEY_PATTERNS:
        if re.match(pat, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':' )

def extract_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            sk = sanitize_key(k)
            if not sk.isdigit(): keys.add(sk)
            keys |= extract_keys(v)
    elif isinstance(obj, list):
        for i in obj: keys |= extract_keys(i)
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
    return drive_service.files().create(body=meta, fields='id').execute()['id']

# Session state
st.session_state.setdefault('user_id', uuid.uuid4().hex[:8])
st.session_state.setdefault('finalized', False)
st.session_state.setdefault('donate', False)
st.session_state.setdefault('survey_choice', None)
st.session_state.setdefault('survey_submitted', False)

user_id = st.session_state['user_id']

# Sidebar info
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Anonymous ID:** `{user_id}`")
st.sidebar.write('DISCLAIMER: Please save this ID in case you want to manage or delete your data later. Since the data is only transferred anonymised, this code would be the only way to match your data to your request.')

# PII definitions
COMMON_PII = {
    'id', 'uuid', 'name', 'full_name', 'username', 'userName',
    'email', 'emailAddress', 'phone', 'phone_number', 'telephoneNumber',
    'birthDate', 'date_of_birth',
    'device_id', 'deviceModel', 'os_version',
    'location', 'hometown', 'current_city', 'external_url',
    'created_at', 'registration_time'
}

PLATFORMS = {
    'TikTok': {
        'uid', 'unique_id', 'nickname',
        'profilePhoto', 'profileVideo', 'bioDescription',
        'likesReceived', 'From', 'Content',
        'email', 'phone_number', 'date_of_birth'
    },
    'Instagram': {
        'username', 'full_name', 'biography', 'profile_picture',
        'email', 'phone_number', 'gender', 'birthday', 'external_url', 'account_creation_date'
    },
    'Facebook': {
        'name', 'birthday', 'gender', 'relationship_status',
        'hometown', 'current_city',
        'emails', 'phones', 'friend_count', 'friends',
        'posts', 'story', 'comments', 'likes'
    },
    'Twitter': {
        'accountId', 'username', 'accountDisplayName',
        'description', 'website', 'location', 'avatarMediaUrl', 'headerMediaUrl',
        'email', 'in_reply_to_user_id', 'source', 'retweet_count', 'favorite_count'
    },
    'Reddit': {
        'username', 'email', 'karma',
        'subreddit', 'author', 'body', 'selftext', 'post_id', 'title',
        'created_utc', 'ip_address'
    }
}

# Ensure Drive
if not (drive_service and ROOT_FOLDER_ID):
    st.error('Drive not configured — check secrets.')
    st.stop()

# UI: platform selection and title
platform = st.sidebar.selectbox('Platform', list(PLATFORMS.keys()))
st.title('Social Media JSON Uploader')

# File upload
uploaded = st.file_uploader(f'Upload {platform} JSON', type='json', accept_multiple_files=False)

# Finalization block
if not st.session_state['finalized']:
    if uploaded:
        # load JSON
        raw = uploaded.read()
        try:
            text = raw.decode('utf-8-sig')
        except:
            text = raw.decode('utf-8', errors='replace')
        try:
            data = json.loads(text)
        except:
            data = [json.loads(l) for l in text.splitlines() if l.strip()]

        st.write('**Consents**')
        st.session_state['donate'] = st.checkbox('(Optional) I donate my anonymized data for research purposes. I agree to its use for research purposes.')
        delete_ok = st.checkbox('I understand that I can request deletion of my data at any time. I have saved my anonymous ID for this purpose.')
        voluntary = st.checkbox('I understand that this is voluntary and does not have an impact for my grade of standing of the course.')
        extras = st.multiselect('Additional keys to redact', sorted(extract_keys(data)))

        if delete_ok and voluntary:
            red = anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(extras))
            with st.expander('Preview Redacted Data'):
                st.json(red)
                fname = f"{user_id}_{platform}_{uploaded.name}".replace('.json.json', '.json')
                st.download_button('Download Redacted JSON', data=json.dumps(red, indent=2), file_name=fname)

            if st.button('Finalize and upload'):
                grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
                grp_id = get_or_create_folder(grp, ROOT_FOLDER_ID)
                usr_id = get_or_create_folder(user_id, grp_id)
                plt_id = get_or_create_folder(platform, usr_id)
                red_id = get_or_create_folder('redacted', plt_id)
                buf = io.BytesIO(json.dumps(red, indent=2).encode())
                drive_service.files().create(body={'name': fname, 'parents': [red_id]}, media_body=MediaIoBaseUpload(buf, 'application/json')).execute()
                st.session_state['finalized'] = True
                st.success('Uploaded redacted JSON')

                if platform == 'TikTok':
                    import pandas as pd
                    st.subheader('TikTok Comments & Posts Analysis')

                    # Comments analysis
                    comments = red.get('Comment', {}).get('Comments', {}).get('CommentsList', [])
                    df_comments = pd.DataFrame(comments)
                    if not df_comments.empty:
                        df_comments['timestamp'] = pd.to_datetime(df_comments['date'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
                        df_comments['date'] = df_comments['timestamp'].dt.date
                        st.metric('Total Comments', len(df_comments))
                        comments_per_day = df_comments.groupby('date').size().rename('count')
                        st.line_chart(comments_per_day)
                    else:
                        st.info('No comments found for TikTok.')

                    # Posts analysis
                    posts = red.get('Post', {}).get('Posts', {}).get('VideoList', [])
                    df_posts = pd.DataFrame(posts)
                    if not df_posts.empty:
                        df_posts['timestamp'] = pd.to_datetime(df_posts['Date'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
                        st.metric('Total Posts', len(df_posts))
                        df_posts['Likes'] = pd.to_numeric(df_posts['Likes'], errors='coerce')
                        top_liked = df_posts.nlargest(5, 'Likes')[['Date', 'Likes', 'Link']]
                        st.table(top_liked)
                    else:
                        st.info('No posts found for TikTok.')

                    # Live watch history analysis
                    live_watch = red.get('Tiktok Live', {}).get('Watch Live History', {}).get('WatchLiveMap', {})
                    if live_watch:
                        watch_times = [v.get('WatchTime') for v in live_watch.values() if v.get('WatchTime')]
                        df_watch = pd.to_datetime(watch_times, format='%Y-%m-%d %H:%M:%S', errors='coerce')
                        df_watch = df_watch.dropna()
                        df_watch = pd.DataFrame({'timestamp': df_watch})
                        df_watch['date'] = df_watch['timestamp'].dt.date
                        st.metric('Total Live Sessions Watched', len(df_watch))
                        watch_per_day = df_watch.groupby('date').size().rename('count')
                        st.bar_chart(watch_per_day)
                    else:
                        st.info('No live watch history found for TikTok.')

                    # Overall videos watched to end (from activity summary)
                    summary = red.get('Your Activity', {}).get('Activity Summary', {}).get('ActivitySummaryMap', {})
                    total_watched = summary.get('videosWatchedToTheEndSinceAccountRegistration')
                    if total_watched is not None:
                        st.metric('Total Videos Watched to End', total_watched)

                    # Unique sounds (tags) in posts
                    if not df_posts.empty and 'Sound' in df_posts.columns:
                        unique_sounds = df_posts['Sound'].dropna().unique().tolist()
                        st.subheader('Unique Sounds Used in Posts')
                        st.write(unique_sounds)

                                        # Login history analysis
                    login_history = red.get('Login History', {}).get('LoginHistoryList', [])
                    df_login = pd.DataFrame(login_history)
                    if not df_login.empty:
                        df_login['timestamp'] = pd.to_datetime(df_login['Date'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
                        df_login['date'] = df_login['timestamp'].dt.date
                        st.metric('Total Login Events', len(df_login))
                        login_per_day = df_login.groupby('date').size().rename('count')
                        st.bar_chart(login_per_day)
                        st.subheader('Devices Used')
                        st.write(df_login['DeviceModel'].value_counts())
                        st.subheader('Network Types')
                        st.write(df_login['NetworkType'].value_counts())

                        # ——— New: IP geolocation & map ———
                        st.subheader("Where They Logged In From")
                        unique_ips = df_login['IP'].dropna().unique().tolist()
                        locs = []
                        token = st.secrets["ipinfo"]["token"]
                        for ip in unique_ips:
                            r = requests.get(f"https://ipinfo.io/{ip}/json?token={token}")
                            if r.status_code == 200:
                                js = r.json()
                                if js.get("loc"):
                                    lat, lon = map(float, js["loc"].split(","))
                                    locs.append({
                                        "lat": lat,
                                        "lon": lon,
                                        "ip": ip,
                                        "city": js.get("city"),
                                        "region": js.get("region")
                                    })
                        if locs:
                            df_locs = pd.DataFrame(locs)
                            st.map(df_locs[["lat", "lon"]])
                            st.table(df_locs[["ip", "city", "region"]].drop_duplicates().reset_index(drop=True))
                        else:
                            st.info("No valid IP locations found.")
                    else:
                        st.info('No login history found for TikTok.')
        else:
            st.info('Agree to deletion & voluntary to proceed')
    else:
        st.info('Upload a JSON to begin')