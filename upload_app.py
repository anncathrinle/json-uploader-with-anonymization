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

# --- Optional word cloud dependency ---
try:
    from wordcloud import WordCloud
except ImportError:
    WordCloud = None

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
COMMON_STOPWORDS = {
    'the', 'and', 'for', 'that', 'with', 'this', 'from', 'they', 'have', 'your',
    'will', 'just', 'like', 'about', 'when', 'what', 'there', 'their', 'were',
    'which', 'been', 'more', 'than', 'some', 'could', 'them', 'only', 'also'
}
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
            # Comments Analysis ... [existing code] ...
            # Posts Analysis
            posts = red.get('Post', {}).get('Posts', {}).get('VideoList', []) or []
            df_p = pd.DataFrame(posts)
            if not df_p.empty and 'Date' in df_p.columns:
                df_p['timestamp'] = pd.to_datetime(df_p['Date'], errors='coerce')
                df_p['Likes'] = pd.to_numeric(df_p['Likes'], errors='coerce')
                # ... previous metrics ...

                # Average Likes per Keyword Topic
                text_col = next((c for c in df_p.columns if c.lower() in ['desc','description','caption','content']), None)
                if text_col and not df_p[text_col].dropna().empty:
                    # derive top keywords
                    words_p = [w.lower() for text in df_p[text_col].dropna() for w in re.findall(r"\b\w+\b", text)]
                    words_p = [w for w in words_p if w not in COMMON_STOPWORDS and len(w) > 3]
                    top5 = pd.Series(words_p).value_counts().head(5).index.tolist()
                    topic_likes = []
                    for kw in top5:
                        mask = df_p[text_col].str.contains(fr"\b{kw}\b", case=False, na=False)
                        avg_like = df_p.loc[mask, 'Likes'].mean() if mask.any() else 0
                        topic_likes.append({'topic': kw, 'avg_likes': round(avg_like,1)})
                    st.subheader('Average Likes per Topic')
                    st.table(pd.DataFrame(topic_likes))

            # ... rest of code ...

        # Generic panel for other platforms ...

        st.info('Analysis complete. Add more modules as desired.')
else:
    st.info('Please agree to proceed.')




