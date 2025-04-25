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
st.sidebar.write('Save this ID to manage or delete your data.')

# PII definitions
PLATFORMS={
    'TikTok':{'profilePhoto','profileVideo','bioDescription','likesReceived','From','Content'},
    'Instagram':{'biography','followers_count','following_count','media_count','profile_picture'},
    'Facebook':{'friend_count','friends','posts','story','comments','likes'},
    'Twitter':{'created_at','text','source','in_reply_to_status_id','in_reply_to_user_id','retweet_count','favorite_count'},
    'Reddit':{'subreddit','author','body','selftext','post_id','created_utc','title'}
}
COMMON_PII={'username','userName','email','emailAddress','id','name','full_name','telephoneNumber','birthDate'}

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
        st.session_state['donate']=st.checkbox('Donate anonymized data for research purposes (optional)')
        delete_ok=st.checkbox('I can request deletion of my data at any time')
        voluntary=st.checkbox('This is voluntary/independent of ICS3; no grade impact')
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
        else:
            st.info('Agree to deletion & voluntary to proceed')
    else:
        st.info('Upload a JSON to begin')

# Survey
if st.session_state['finalized'] and not st.session_state['survey_submitted']:
    choice=st.radio('Answer optional research questions?',['Yes','No','I have already answered'])
    if choice=='Yes':
        st.markdown('*Voluntary — no grade impact*')
        q1=st.radio('Ever active in social movement?',['Yes','No'])
        smf=smt=smtk=''
        if q1=='Yes': smf=str(st.date_input('From when?')); smt=str(st.date_input('Until when?')); smtk=st.text_input('What movement?')
        q2=st.radio('Participated in protest?',['Yes','No'])
        pf=pl=pr=''
        if q2=='Yes': pf=str(st.date_input('First protest?')); pl=str(st.date_input('Last protest?')); pr=st.text_area('Why join/stop?')
        q3=st.text_area('Any post you remember?')
        if st.button('Submit survey'):
            survey={'anonymous_id':user_id,'platform':platform,'active_movement':q1,'movement_from':smf,'movement_until':smt,'movement_kind':smtk,'participated_protest':q2,'first_protest':pf,'last_protest':pl,'protest_reason':pr,'remembered_post':q3}
            # upload survey
            grp = 'research_donations' if st.session_state['donate'] else 'non_donations'
            grp_id=get_or_create_folder(grp,ROOT_FOLDER_ID)
            usr_id=get_or_create_folder(user_id,grp_id)
            surv_id=get_or_create_folder('survey',usr_id)
            buf=io.BytesIO(json.dumps(survey,indent=2).encode())
            drive_service.files().create(body={'name':f'{user_id}_survey.json','parents':[surv_id]},media_body=MediaIoBaseUpload(buf,'application/json')).execute()
            st.session_state['survey_submitted']=True
            st.success('Survey saved')
    else:
        st.session_state['survey_submitted']=True

# Thank-you
if st.session_state['survey_submitted']:
    st.subheader('Thank you! You can upload another platform via sidebar.')







