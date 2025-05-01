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
st.set_page_config(page_title='Social media data upload & anonymization tool', layout='wide')

# Helper functions
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for pat in KEY_PATTERNS:
        if re.match(pat, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':' )

def extract_keys(obj):
    keys=set()
    if isinstance(obj, dict):
        for k,v in obj.items():
            sk=sanitize_key(k)
            if not sk.isdigit(): keys.add(sk)
            keys |= extract_keys(v)
    elif isinstance(obj,list):
        for i in obj: keys |= extract_keys(i)
    return keys

def anonymize(obj, pii_set):
    if isinstance(obj, dict):
        return {sanitize_key(k):( 'REDACTED' if sanitize_key(k) in pii_set else anonymize(v,pii_set)) for k,v in obj.items()}
    if isinstance(obj, list):
        return [anonymize(i,pii_set) for i in obj]
    return obj

def get_or_create_folder(name,parent_id):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    resp = drive_service.files().list(q=query,fields='files(id)').execute()
    files=resp.get('files',[])
    if files: return files[0]['id']
    meta={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent_id]}
    return drive_service.files().create(body=meta,fields='id').execute()['id']

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
    # universal identifiers
    'id', 'uuid', 'name', 'full_name', 'username', 'userName',
    'email', 'emailAddress', 'phone', 'phone_number', 'telephoneNumber',
    'birthDate', 'date_of_birth',
    # device/network
    'ip_address', 'device_id', 'deviceModel', 'os_version', 'last_login_ip',
    # locations & urls
    'location', 'hometown', 'current_city', 'external_url',
    # timestamps that can identify you
    'created_at', 'registration_time'
}

PLATFORMS = {
    'TikTok': {
        # core profile & device info
        'uid', 'unique_id', 'nickname',
        'profilePhoto', 'profileVideo', 'bioDescription',
        'likesReceived', 'From', 'Content',
        # personal PII
        'email', 'phone_number', 'date_of_birth', 'ip_address'
    },
    'Instagram': {
        # core profile
        'username', 'full_name', 'biography', 'profile_picture',
        # contact
        'email', 'phone_number',
        # demographics
        'gender', 'birthday', 'external_url', 'account_creation_date'
    },
    'Facebook': {
        # core
        'name', 'birthday', 'gender', 'relationship_status',
        'hometown', 'current_city',
        # contact & social
        'emails', 'phones', 'friend_count', 'friends',
        # activity
        'posts', 'story', 'comments', 'likes'
    },
    'Twitter': {
        # identifiers
        'accountId', 'username', 'accountDisplayName',
        # profile & contact
        'description', 'website', 'location', 'avatarMediaUrl', 'headerMediaUrl',
        'email',
        # metadata
        'in_reply_to_user_id', 'source', 'retweet_count', 'favorite_count'
    },
    'Reddit': {
        # account
        'username', 'email', 'karma',
        # content
        'subreddit', 'author', 'body', 'selftext', 'post_id', 'title',
        # metadata
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
        raw=uploaded.read()
        try: text=raw.decode('utf-8-sig')
        except: text=raw.decode('utf-8',errors='replace')
        try: data=json.loads(text)
        except: data=[json.loads(l) for l in text.splitlines() if l.strip()]
        # consents
        st.write('**Consents**')
        st.session_state['donate']=st.checkbox(' (Optional) I donate my anonymized data for research purposes. I agree to its use for research purposes.')
        delete_ok=st.checkbox('I understand that I can request deletion of my data at any time. I have saved my anonymous ID for this purpose.')
        voluntary=st.checkbox('I understand that this is voluntary and does not have an impact for my grade of standing of the course.')
        extras=st.multiselect('Additional keys to redact', sorted(extract_keys(data)))
        if delete_ok and voluntary:
            red=anonymize(data, COMMON_PII.union(PLATFORMS[platform]).union(extras))
            with st.expander('Preview Redacted Data'):
                st.json(red)
                # download
                fname=f"{user_id}_{platform}_{uploaded.name}".replace('.json.json','.json')
                st.download_button('Download Redacted JSON', data=json.dumps(red,indent=2),file_name=fname)
            if st.button('Finalize and upload'):
                # group
                grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
                grp_id = get_or_create_folder(grp, ROOT_FOLDER_ID)
                usr_id = get_or_create_folder(user_id, grp_id)
                plt_id = get_or_create_folder(platform, usr_id)
                red_id = get_or_create_folder('redacted', plt_id)
                # upload
                buf=io.BytesIO(json.dumps(red,indent=2).encode())
                drive_service.files().create(body={'name':fname,'parents':[red_id]},media_body=MediaIoBaseUpload(buf,'application/json')).execute()
                st.session_state['finalized']=True
                st.success('Uploaded redacted JSON')

                # TikTok-specific data extraction and analysis
                if platform == 'TikTok':
                    import pandas as pd
                    st.subheader('TikTok Usage Summary')
                    # locate history
                    if isinstance(red, dict):
                        for key in ['videoPlayHistory','playHistory','watchHistory']:
                            if key in red and isinstance(red[key], list):
                                history = red[key]
                                break
                        else:
                            history = []
                    else:
                        history = red if isinstance(red, list) else []
                    df_tt = pd.DataFrame(history)
                    # normalize timestamp
                    if 'createTime' in df_tt.columns:
                        df_tt['timestamp'] = pd.to_datetime(df_tt['createTime'], unit='s', errors='coerce')
                    elif 'viewTime' in df_tt.columns:
                        df_tt['timestamp'] = pd.to_datetime(df_tt['viewTime'], unit='s', errors='coerce')
                    else:
                        df_tt['timestamp'] = pd.to_datetime(df_tt.get('timestamp'), unit='s', errors='coerce')
                    # normalize duration
                    if 'duration' in df_tt.columns:
                        df_tt['duration'] = pd.to_numeric(df_tt['duration'], errors='coerce')
                    elif 'playDuration' in df_tt.columns:
                        df_tt['duration'] = pd.to_numeric(df_tt['playDuration'], errors='coerce')
                    else:
                        df_tt['duration'] = pd.to_numeric(df_tt.get('duration',0), errors='coerce')
                    df_tt = df_tt.dropna(subset=['timestamp'])
                    if not df_tt.empty:
                        df_tt['date'] = df_tt['timestamp'].dt.date
                        total_videos = len(df_tt)
                        total_time_min = df_tt['duration'].sum() / 60
                        st.metric('Total Videos Watched', total_videos)
                        st.metric('Total Watch Time (min)', round(total_time_min, 2))
                        usage = df_tt.groupby('date')['duration'].sum().reset_index()
                        usage['minutes'] = usage['duration'] / 60
                        st.line_chart(usage.rename(columns={'date':'Date','minutes':'Minutes Watched'}).set_index('Date'))
                    else:
                        st.info('No watch events found for TikTok.')

                # Generic data summary for all platforms
                st.subheader(f"{platform} Data Summary")
                import pandas as pd
                df_all = pd.json_normalize(red)
                st.write(f"Loaded {len(df_all)} records with {len(df_all.columns)} fields.")
                st.dataframe(df_all.head())
                non_null_counts = df_all.count().sort_values(ascending=False)
                st.subheader("Top 10 Fields by Non-Null Count")
                st.bar_chart(non_null_counts.head(10))
                if 'timestamp' in df_all.columns:
                    df_all['timestamp'] = pd.to_datetime(df_all['timestamp'], errors='coerce')
                    df_all['date'] = df_all['timestamp'].dt.date
                    activity = df_all.groupby('date').size().rename('count').reset_index()
                    st.subheader("Activity Over Time")
                    st.line_chart(activity.set_index('date'))
        else:
            st.info('Agree to deletion & voluntary to proceed')
    else:
        st.info('Upload a JSON to begin')










