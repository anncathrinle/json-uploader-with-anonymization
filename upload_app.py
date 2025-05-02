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

# Optional word cloud dependency
try:
    from wordcloud import WordCloud
except ImportError:
    WordCloud = None

# Configuration
logging.getLogger('streamlit.ScriptRunner').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='missing ScriptRunContext')
st.set_page_config(page_title='Social Media JSON Uploader', layout='wide')

# Google Drive Setup
try:
    gdrive_info = st.secrets['gdrive']
    creds = Credentials.from_service_account_info(
        gdrive_info['service_account'],
        scopes=['https://www.googleapis.com/auth/drive']
    )
    drive_service = build('drive', 'v3', credentials=creds)
    ROOT_FOLDER_ID = gdrive_info.get('folder_id')
except Exception:
    drive_service, ROOT_FOLDER_ID = None, None

if not (drive_service and ROOT_FOLDER_ID):
    st.error('Drive not configured — check secrets.')
    st.stop()

# Helper functions
KEY_PATTERNS = [r'Chat History with .+', r'comments?:.*', r'replies?:.*', r'posts?:.*', r'story:.*']
COMMON_STOPWORDS = {'the','and','for','that','with','this','from','they','have','your',
                    'will','just','like','about','when','what','there','their','were',
                    'which','been','more','than','some','could','them','only','also'}
COMMON_PII = {'id','uuid','name','full_name','username','userName','email','emailAddress',
              'phone','phone_number','telephoneNumber','birthDate','date_of_birth',
              'device_id','deviceModel','os_version','location','hometown','current_city',
              'external_url','created_at','registration_time'}
PLATFORMS = {
    'TikTok': {'uid','unique_id','nickname','profilePhoto','profileVideo','bioDescription',
               'likesReceived','From','Content','email','phone_number','date_of_birth'},
    'Instagram': {'username','full_name','biography','profile_picture','email',
                  'phone_number','gender','birthday','external_url','account_creation_date'},
    'Facebook': {'name','birthday','gender','relationship_status','hometown',
                 'current_city','emails','phones','friend_count','friends','posts',
                 'story','comments','likes'},
    'Twitter': {'accountId','username','accountDisplayName','description','website',
                'location','avatarMediaUrl','headerMediaUrl','email',
                'in_reply_to_user_id','source','retweet_count','favorite_count'},
    'Reddit': {'username','email','karma','subreddit','author','body',
               'selftext','post_id','title','created_utc','ip_address'}
}

def sanitize_key(k):
    for pat in KEY_PATTERNS:
        if re.match(pat, k, flags=re.IGNORECASE):
            return k.split(':',1)[0].title()
    return k.rstrip(':')

def extract_keys(obj):
    keys=set()
    if isinstance(obj, dict):
        for k,v in obj.items():
            sk=sanitize_key(k)
            if not sk.isdigit(): keys.add(sk)
            keys |= extract_keys(v)
    elif isinstance(obj, list):
        for i in obj: keys |= extract_keys(i)
    return keys

def anonymize(obj,ppi_set):
    if isinstance(obj, dict):
        return {sanitize_key(k):('REDACTED' if sanitize_key(k) in ppi_set else anonymize(v,ppi_set))
                for k,v in obj.items()}
    if isinstance(obj, list): return [anonymize(i,ppi_set) for i in obj]
    return obj

def get_folder(name,parent):
    q=f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent}' in parents"
    resp=drive_service.files().list(q=q,fields='files(id)').execute()
    files=resp.get('files',[])
    if files: return files[0]['id']
    meta={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]}
    return drive_service.files().create(body=meta,fields='id').execute()['id']

# Session state
st.session_state.setdefault('user_id',uuid.uuid4().hex[:8])
st.session_state.setdefault('finalized',False)
st.session_state.setdefault('donate',False)
user_id=st.session_state['user_id']

# Sidebar
st.sidebar.markdown('---')
st.sidebar.markdown(f"**Anonymous ID:** `{user_id}`")
st.sidebar.write('Save this ID to manage or delete data later.')
platform=st.sidebar.selectbox('Platform',list(PLATFORMS.keys()))
st.title('Social Media JSON Uploader')

# Upload
uploaded=st.file_uploader(f'Upload {platform} JSON',type='json')
if not uploaded:
    st.info('Upload a JSON to begin')
    st.stop()

# Load & Redact
raw=uploaded.read()
text=raw.decode('utf-8-sig',errors='replace')
try:data=json.loads(text)
except:data=[json.loads(l) for l in text.splitlines()]

st.session_state['donate']=st.checkbox('Donate anonymized data for research')
delete_ok=st.checkbox('I understand deletion & saved ID')
if not delete_ok:
    st.info('Please agree to proceed.')
    st.stop()
extras=st.multiselect('Additional redact keys',sorted(extract_keys(data)))
red=anonymize(data,COMMON_PII.union(PLATFORMS[platform]).union(extras))
with st.expander('Preview Redacted Data'): st.json(red)
fname=f"{user_id}_{platform}_{uploaded.name}".replace('.json.json','.json')
st.download_button('Download Redacted JSON',data=json.dumps(red,indent=2),file_name=fname)
if st.button('Finalize & upload'):
    grp='research_donations' if st.session_state['donate'] else 'non_donations'
    gid=get_folder(grp,ROOT_FOLDER_ID)
    uid=get_folder(user_id,gid)
    pid=get_folder(platform,uid)
    rid=get_folder('redacted',pid)
    buf=io.BytesIO(json.dumps(red,indent=2).encode())
    drive_service.files().create(body={'name':fname,'parents':[rid]},media_body=MediaIoBaseUpload(buf,'application/json')).execute()
    st.success('Uploaded')
    st.subheader(f'{platform} Analytics')
    
    # TikTok Analytics
    if platform=='TikTok':
        cmts=red.get('Comment',{}).get('Comments',{}).get('CommentsList',[]) or []
        dfc=pd.DataFrame(cmts)
        if not dfc.empty:
            dfc['ts']=pd.to_datetime(dfc['date'],errors='coerce')
            dfc['date']=dfc['ts'].dt.date
            st.metric('Total Comments',len(dfc))
            st.subheader('Comments Over Time')
            st.line_chart(dfc.groupby('date').size().rename('count'))
            st.subheader('Comment Length Distribution')
            st.metric('Avg Comment Length',round(dfc['comment'].str.len().mean(),1))
            dfc['weekday']=dfc['ts'].dt.day_name()
            st.subheader('Comments by Weekday')
            st.bar_chart(dfc['weekday'].value_counts())
            words=[w.lower() for txt in dfc['comment'].dropna() for w in re.findall(r"\b\w+\b",txt)]
            words=[w for w in words if w not in COMMON_STOPWORDS and len(w)>3]
            st.subheader('Top Comment Words')
            st.bar_chart(pd.Series(words).value_counts().head(10))
            if WordCloud:
                wc=WordCloud(width=400,height=200,background_color='white').generate(' '.join(dfc['comment'].dropna()))
                st.subheader('Comment Word Cloud')
                st.image(wc.to_array(),use_column_width=True)
        posts=red.get('Post',{}).get('Posts',{}).get('VideoList',[]) or []
        dfp=pd.DataFrame(posts)
        if not dfp.empty:
            dfp['ts']=pd.to_datetime(dfp['Date'],errors='coerce')
            dfp['Likes']=pd.to_numeric(dfp['Likes'],errors='coerce')
            st.metric('Total Posts',len(dfp))
            st.subheader('Weekly Likes Trend')
            st.bar_chart(dfp.set_index('ts')['Likes'].resample('W').mean())
            st.subheader('Posts by Hour of Day')
            dfp['hour']=dfp['ts'].dt.hour
            st.bar_chart(dfp['hour'].value_counts().sort_index())
            if not dfc.empty:
                st.metric('Comments per Post',round(len(dfc)/len(dfp),2))
            txtcol=next((c for c in dfp.columns if c.lower() in ['desc','description','caption','content']),None)
            if txtcol:
                words_p=[w.lower() for txt in dfp[txtcol].dropna() for w in re.findall(r"\b\w+\b",txt)]
                words_p=[w for w in words_p if w not in COMMON_STOPWORDS and len(w)>3]
                st.subheader('Top Post Words')
                st.bar_chart(pd.Series(words_p).value_counts().head(10))
                if WordCloud:
                    wc2=WordCloud(width=400,height=200,background_color='white').generate(' '.join(dfp[txtcol].dropna()))
                    st.subheader('Post Word Cloud')
                    st.image(wc2.to_array(),use_column_width=True)
                # Avg Likes per Topic
                topics=pd.Series(words_p).value_counts().head(5).index.tolist()
                tl=[{'topic':kw,'avg_likes':round(dfp[dfp[txtcol].str.contains(fr"\b{kw}\b",case=False,na=False)]['Likes'].mean(),1)} for kw in topics]
                st.subheader('Average Likes per Topic')
                st.table(pd.DataFrame(tl))
        tags=red.get('Hashtag',{}).get('HashtagList',[]) or []
        dfh=pd.DataFrame(tags)
        if not dfh.empty:
            st.subheader('Top Hashtags')
            st.bar_chart(dfh['HashtagName'].value_counts().head(5))
        sumry=red.get('Your Activity',{}).get('Activity Summary',{}).get('ActivitySummaryMap',{}) or {}
        tw=sumry.get('videosWatchedToTheEndSinceAccountRegistration')
        if tw is not None: st.metric('Total Videos Watched',tw)
        wh=red.get('Your Activity',{}).get('Video Watch History',{}).get('VideoWatchHistoryList',[]) or []
        if wh:
            dfw=pd.DataFrame(wh)
            tcol=next((c for c in dfw.columns if 'date' in c.lower() or 'time' in c.lower()),None)
            if tcol:
                dfw['ts']=pd.to_datetime(dfw[tcol],errors='coerce')
                dur=dfw['ts'].max()-dfw['ts'].min()
                st.subheader('Watch Session Durations')
                st.metric('Longest Session (h)',round(dur.total_seconds()/3600,2))
                # average per day
                daily= dfw.groupby(dfw['ts'].dt.date)['ts'].agg(lambda x: x.max()-x.min())
                avg_hours=round(daily.dt.total_seconds().mean()/3600,2)
                st.metric('Average Session (h)',avg_hours)
                dfw['hour']=dfw['ts'].dt.hour
                st.subheader('Video Watches by Hour')
                st.bar_chart(dfw['hour'].value_counts().sort_index())
    else:
        for sec,cont in red.items():
            if isinstance(cont,dict):
                for k,blk in cont.items():
                    if isinstance(blk,list) and blk:
                        dfx=pd.DataFrame(blk)
                        dc=next((c for c in dfx.columns if c.lower() in ['date','timestamp']),None)
                        if dc:
                            dfx['ts']=pd.to_datetime(dfx[dc],errors='coerce')
                            st.subheader(f'{sec} - {k} Over Time')
                            st.line_chart(dfx.groupby(dfx['ts'].dt.date).size().rename('count'))
    st.info('Analysis complete.')





