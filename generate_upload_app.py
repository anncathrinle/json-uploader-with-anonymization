# generate_upload_app.py
"""
Generator script for the Streamlit JSON uploader app with dynamic platform selection, multi-level consent, persistent anonymous user ID, secured Admin mode via Streamlit Secrets, and ready for GitHub deployment.

Usage:
  1. Save this file as `generate_upload_app.py` in your project root (you'll push this and `upload_app.py` to GitHub).
  2. (Optional) Create `.streamlit/secrets.toml` with:
       [admin]
       password = "YOUR_SECURE_PASSWORD"
  3. Run locally to generate `upload_app.py`:
       python generate_upload_app.py
  4. Commit and push **all** files to GitHub:
       git add generate_upload_app.py upload_app.py .streamlit/secrets.toml
       git commit -m "Add Streamlit JSON uploader"
       git push origin main
  5. On Streamlit Community Cloud, connect the repo. No absolute paths used—everything is relative.
"""

import os
import sys

# Write to current working directory (repo root)
# Write to the same directory as this script
dir_path = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(dir_path, 'upload_app.py')

# Embedded Streamlit app code
APP_CODE = r"""
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

# Page config (must be first)
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Mode selector
mode = st.sidebar.radio('Mode', ['Upload', 'Admin'])

# Common PII keys
def get_pii_sets():
    COMMON = {'username','userName','email','emailAddress','id','name','full_name','telephoneNumber','birthDate'}
    PLATFORM = {
        'TikTok': {'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
        'Instagram': {'biography','followers_count','following_count','media_count','profile_picture'},
        'Facebook': {'friend_count','friends','posts','story','comments','likes'},
        'Twitter': {'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
        'Reddit': {'subreddit','author','body','selftext','post_id','created_utc','title'}
    }
    return COMMON, PLATFORM

KEY_PATTERNS = [r'Chat History with .+',r'comments?:.*',r'replies?:.*',r'posts?:.*',r'story:.*']

def sanitize_key(k):
    for p in KEY_PATTERNS:
        if re.match(p, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':')

def extract_keys(o):
    s=set()
    if isinstance(o, dict):
        for kk, vv in o.items():
            sk = sanitize_key(kk)
            if not sk.isdigit(): s.add(sk)
            s |= extract_keys(vv)
    elif isinstance(o, list):
        for ii in o: s |= extract_keys(ii)
    return s

def anonymize(o, pset):
    if isinstance(o, dict):
        return {sanitize_key(kk): ('REDACTED' if sanitize_key(kk) in pset else anonymize(vv,pset)) for kk,vv in o.items()}
    if isinstance(o, list):
        return [anonymize(item,pset) for item in o]
    return o

# Uploader mode
if mode == 'Upload':
    # Generate persistent user ID
    if 'user_id' not in st.session_state:
        st.session_state.user_id = uuid.uuid4().hex[:8]
    user_id = st.session_state.user_id

    st.sidebar.markdown('---')
    st.sidebar.markdown(f"**Your Anonymous ID:** `{user_id}`")
    st.sidebar.write('Save this ID to manage or delete your data.')

    COMMON, PLATFORM = get_pii_sets()
    st.title('Upload Mode')
    platform = st.sidebar.selectbox('Select Platform', options=list(PLATFORM.keys()))
    st.sidebar.markdown(f'**Platform:** {platform}')

    files = st.file_uploader(f'Upload JSON for {platform}', type=['json'], accept_multiple_files=True)
    if files:
        for f in files:
            st.header(f.name)
            raw = f.read()
            # Save raw
            raw_dir = os.path.join('uploads', user_id, platform)
            os.makedirs(raw_dir, exist_ok=True)
            with open(os.path.join(raw_dir, f.name), 'wb') as out: out.write(raw)
            # Parse
            try: txt = raw.decode('utf-8-sig')
            except: txt = raw.decode('utf-8', errors='replace')
            try: data = json.loads(txt)
            except: data = [json.loads(line) for line in txt.splitlines() if line.strip()]
            # Redact
            base_pii = COMMON.union(PLATFORM[platform])
            extra = st.multiselect('Extra keys to redact', options=sorted(extract_keys(data)))
            red = anonymize(data, base_pii.union(extra))
            # Save redacted for audit
            red_dir = os.path.join('uploads', user_id, platform, 'redacted')
            os.makedirs(red_dir, exist_ok=True)
            fn = f"{user_id}_{platform}_{f.name}"
            with open(os.path.join(red_dir, fn + '.json'), 'w') as out: out.write(json.dumps(red, indent=2))
            # Preview
            with st.expander('Preview Anonymized Data'):
                st.json(red)
            # Consents
            c1 = st.checkbox('Participation is voluntary and does not affect my grade or standing in ICS3.')
            c2 = st.checkbox('I agree to the use of the anonymized data for research purposes.')
            c3 = st.checkbox('I understand I can request deletion of my data at any time.')
            if c1 and c2 and c3:
                st.download_button('Download Anonymized JSON', data=json.dumps(red, indent=2), file_name=fn + '.json')
                if st.button(f'Finalize and send {fn}.json'):
                    st.success(f'{fn}.json finalized with ID {user_id}')
            else:
                st.info('Please check all consent boxes to proceed.')
    else:
        st.info('Upload one or more JSON files to begin')

# Admin mode
if mode == 'Admin':
    st.title('Admin Mode')
    pwd = st.sidebar.text_input('Enter Admin Password', type='password')
    if ADMIN_PASSWORD and pwd == ADMIN_PASSWORD:
        uid = st.text_input('Anonymous ID')
        if uid:
            base = os.path.join('uploads', uid)
            if os.path.isdir(base):
                plats = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d, 'redacted'))]
                plat = st.selectbox('Select Platform', options=plats)
                rd = os.path.join(base, plat, 'redacted')
                for fn in os.listdir(rd):
                    path = os.path.join(rd, fn)
                    st.write(fn)
                    st.download_button('Download File', data=open(path, 'rb').read(), file_name=fn)
            else:
                st.warning('No uploads found for that ID')
    else:
        st.sidebar.error('Invalid admin password')
"""

# Write the app file
def main():
    with open(OUTPUT_PATH, 'w') as f:
        f.write(APP_CODE)
    print(f"Created {OUTPUT_PATH}\nRun: streamlit run upload_app.py")

if __name__ == '__main__':
    main()
