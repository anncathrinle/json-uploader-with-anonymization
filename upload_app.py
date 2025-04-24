import uuid
import logging
import warnings
import streamlit as st
import json
import re
import os

# Load admin password from Streamlit Secrets
try:
    ADMIN_PASSWORD = st.secrets["admin"]["password"]
except Exception:
    ADMIN_PASSWORD = None

# Suppress extraneous warnings
logging.getLogger('streamlit.ScriptRunner').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='missing ScriptRunContext')

# Page configuration (must be first)
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Mode selector
tab = st.sidebar.radio('Mode', ['Upload', 'Admin'])

# Common and platform-specific PII sets
def get_pii_sets():
    COMMON = {'username', 'userName', 'email', 'emailAddress', 'id', 'name', 'full_name', 'telephoneNumber', 'birthDate'}
    PLATFORM = {
        'TikTok': {'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
        'Instagram': {'biography','followers_count','following_count','media_count','profile_picture'},
        'Facebook': {'friend_count','friends','posts','story','comments','likes'},
        'Twitter': {'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
        'Reddit': {'subreddit','author','body','selftext','post_id','created_utc','title'}
    }
    return COMMON, PLATFORM

KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']

def sanitize_key(k):
    for p in KEY_PATTERNS:
        if re.match(p, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':')

def extract_keys(o):
    keys = set()
    if isinstance(o, dict):
        for kk, vv in o.items():
            sk = sanitize_key(kk)
            if not sk.isdigit():
                keys.add(sk)
            keys |= extract_keys(vv)
    elif isinstance(o, list):
        for item in o:
            keys |= extract_keys(item)
    return keys

def anonymize(o, pset):
    if isinstance(o, dict):
        return {sanitize_key(kk): ('REDACTED' if sanitize_key(kk) in pset else anonymize(vv, pset)) for kk, vv in o.items()}
    if isinstance(o, list):
        return [anonymize(i, pset) for i in o]
    return o

# --- Upload Mode ---
if tab == 'Upload':
    if 'user_id' not in st.session_state:
        st.session_state.user_id = uuid.uuid4().hex[:8]
    user_id = st.session_state.user_id
    
    st.sidebar.markdown('---')
    st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
    st.sidebar.write('Save this ID; you will need it to manage or delete your data.')

    COMMON, PLATFORM = get_pii_sets()
    st.title('Upload Social Media JSON')
    platform = st.sidebar.selectbox('Platform', options=list(PLATFORM.keys()))
    st.sidebar.markdown(f'**Selected:** {platform}')

    uploaded = st.file_uploader(f'Upload JSON file(s) for {platform}', type=['json'], accept_multiple_files=True)
    if uploaded:
        for f in uploaded:
            st.subheader(f.name)
            raw = f.read()
            
            # Persist raw upload
            raw_dir = os.path.join('uploads', user_id, platform)
            os.makedirs(raw_dir, exist_ok=True)
            with open(os.path.join(raw_dir, f.name), 'wb') as out:
                out.write(raw)
            
            # Parse JSON
            try:
                text = raw.decode('utf-8-sig')
            except:
                text = raw.decode('utf-8', errors='replace')
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]

            # Redaction selection
            base_pii = COMMON.union(PLATFORM[platform])
            additional = st.multiselect('Select additional keys to redact', options=sorted(extract_keys(data)))
            pii_set = base_pii.union(additional)
            redacted = anonymize(data, pii_set)

            # Save redacted
            red_dir = os.path.join('uploads', user_id, platform, 'redacted')
            os.makedirs(red_dir, exist_ok=True)
            filename = f"{user_id}_{platform}_{f.name}"
            with open(os.path.join(red_dir, filename + '.json'), 'w') as out:
                out.write(json.dumps(redacted, indent=2))

            # Preview
            with st.expander('Preview Anonymized Data'):
                st.json(redacted)

            # Consent
            c1 = st.checkbox('I understand that participation is voluntary and does not affect my grade or standing in ICS3.')
            c2 = st.checkbox('I agree to use the anonymized data for research purposes.')
            c3 = st.checkbox('I understand I can request deletion of my data at any time.')

            if c1 and c2 and c3:
                st.download_button('Download Anonymized JSON', data=json.dumps(redacted, indent=2), file_name=filename + '.json')
                if st.button(f'Finalize and send {filename}.json'):
                    st.success(f'{filename}.json finalized (ID: {user_id})')
            else:
                st.info('Please check all consent boxes to proceed.')
    else:
        st.info('Upload one or more JSON files to start')

# --- Admin Mode ---
else:
    st.title('Admin Dashboard')
    pwd = st.sidebar.text_input('Admin Password', type='password')
    if ADMIN_PASSWORD and pwd == ADMIN_PASSWORD:
        uid = st.text_input('Enter Anonymous ID to view data')
        if uid:
            base_path = os.path.join('uploads', uid)
            if os.path.isdir(base_path):
                platforms = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d, 'redacted'))]
                plat = st.selectbox('Platform', options=platforms)
                red_path = os.path.join(base_path, plat, 'redacted')
                for fn in sorted(os.listdir(red_path)):
                    st.write(fn)
                    with open(os.path.join(red_path, fn), 'rb') as file:
                        st.download_button('Download File', data=file.read(), file_name=fn)
            else:
                st.warning('No data found for that ID.')
    else:
        st.sidebar.error('Invalid admin password')
""
