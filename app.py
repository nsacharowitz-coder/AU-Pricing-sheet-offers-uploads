import csv
import os
import hmac
import io
import json
import re
import sqlite3
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pandas as pd
import streamlit as st
from pypdf import PdfReader

DB = Path(os.environ.get('AU_DB_PATH', str(Path(__file__).with_name('au_offers.db'))))
DB.parent.mkdir(parents=True, exist_ok=True)
st.set_page_config(page_title='AU Offer Manager', page_icon='⚡', layout='wide', initial_sidebar_state='expanded')

# Require a password before displaying any client information or accessing the database.
# Set AU_APP_PASSWORD using your hosting provider's secret environment variables.
def require_login():
    secret = os.environ.get('AU_APP_PASSWORD', '')
    if not secret:
        st.error('Website access is not configured. The administrator must set AU_APP_PASSWORD.')
        st.stop()
    if not st.session_state.get('authenticated', False):
        st.title('⚡ AU Pricing Portal')
        st.info('Sign in to access client rates and supplier quotations.')
        with st.form('login_form'):
            entered = st.text_input('Password', type='password')
            submitted = st.form_submit_button('Sign in', type='primary')
        if submitted:
            if hmac.compare_digest(entered.encode('utf-8'), secret.encode('utf-8')):
                st.session_state['authenticated'] = True
                st.rerun()
            else:
                st.error('Incorrect password.')
        st.stop()
    if st.sidebar.button('Sign out'):
        st.session_state['authenticated'] = False
        st.rerun()

require_login()


CREATE = '''
CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, elec_uplift REAL DEFAULT 0, gas_uplift REAL DEFAULT 0, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS supplies (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, fuel TEXT NOT NULL, meter TEXT NOT NULL, site TEXT, postcode TEXT, day_usage REAL, night_usage REAL, kva REAL, day_rate REAL, night_rate REAL, standing REAL, kva_rate REAL, other_annual REAL DEFAULT 0, start_date TEXT, end_date TEXT, updated TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(client_id,fuel,meter));
CREATE TABLE IF NOT EXISTS offers (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, supply_id INTEGER NOT NULL, supplier TEXT NOT NULL, term INTEGER NOT NULL, day_rate REAL, night_rate REAL, standing REAL DEFAULT 0, kva_rate REAL DEFAULT 0, other_annual REAL DEFAULT 0, start_date TEXT, end_date TEXT, quote_date TEXT, source TEXT, notes TEXT, updated TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(client_id,supply_id,supplier,term));
CREATE TABLE IF NOT EXISTS declines (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, supplier TEXT NOT NULL, fuel TEXT, reason TEXT, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS supplier_terms (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, supplier TEXT NOT NULL, fuel TEXT NOT NULL DEFAULT 'Both', generator_matching TEXT, volume_tolerance TEXT, take_or_pay TEXT, pass_through TEXT, termination TEXT, payment_terms TEXT, credit_requirements TEXT, quote_validity TEXT, other_terms TEXT, source TEXT, updated TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(client_id,supplier,fuel));
'''

# DATABASE_URL enables durable PostgreSQL (e.g. Supabase). SQLite is only for local demos.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]

class PostgresConnection:
    def __init__(self):
        self.db = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=12)
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.db.rollback()
            else:
                self.db.commit()
        finally:
            self.db.close()
    def executescript(self, script):
        # Convert SQLite's auto-increment primary keys and case-insensitive collation.
        schema = script.replace('INTEGER PRIMARY KEY', 'BIGSERIAL PRIMARY KEY').replace(' UNIQUE COLLATE NOCASE', ' UNIQUE')
        for statement in schema.split(';'):
            if statement.strip():
                self.db.execute(statement)
    def execute(self, sql, args=()):
        # All application SQL uses positional ? placeholders.
        return self.db.execute(sql.replace('?', '%s'), tuple(args))
    def commit(self):
        self.db.commit()

def conn():
    if DATABASE_URL:
        db = PostgresConnection()
    else:
        local = sqlite3.connect(DB)
        local.row_factory = sqlite3.Row
        db = local
    db.executescript(CREATE)
    return db

def fetch(sql, args=()):
    with conn() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]

def execute(sql, args):
    with conn() as db:
        db.execute(sql, args)
        db.commit()

def num(v):
    if pd.isna(v): return None
    if isinstance(v, (int,float)): return float(v)
    s = str(v).replace(',','').replace('£','').replace('p/kWh','').replace('p/day','').replace('%','').strip()
    try: return float(s) if s else None
    except ValueError: return None

def text(v):
    return '' if v is None or pd.isna(v) else str(v).strip()

def hdr(s):
    return re.sub(r'[^a-z0-9]+','',str(s).lower())

SYN = {
    'meter': ['mpanbottomline','mpanbottom','mpan13digit','mpan13digits','mpannumber','mpannum','mpanmprn','mpanormprn','mpanmp rn','electricitympan','mprnnumber','gas mprn','mprnumber','mpan','mprn','mpr','pxn','supplynumber','meterreference','meterid'],
    'site': ['sitename','site','supplyaddress','address','customername'],
    'postcode': ['sitepostcode','postcode','post code'],
    'fuel': ['fuel','utility','type'],
    'day_usage': ['dayusagekwh','dayusage','daykwh','dayconsumption','dayeac','annualdayusage','eac','annualusage','aqkwh'],
    'night_usage': ['nightusagekwh','nightusage','nightkwh','nightconsumption','annualnightusage'],
    'kva': ['kva','capacity','agreedcapacity','mic'],
    'day_rate': ['dayratepencekwh','dayratepkwh','dayrate','unitratepkwh','unitratepencekwh','unitrate','singlerate'],
    'night_rate': ['nightratepencekwh','nightratepkwh','nightrate','eveningweekendratepencekwh'],
    'standing': ['standingchargepenceday','standingchargepday','standingcharge','standingpday','standing'],
    'kva_rate': ['kvaratepenceday','kvarate','capacityrate'],
    'other_annual': ['otherannualcostgbp','otherannual','additionalannualcost'],
    'start_date': ['contractstartdate','startdate','start'],
    'end_date': ['contractenddate','enddate','end'],
}

def align_df(df):
    names = {hdr(c):c for c in df.columns}
    out = pd.DataFrame()
    for key, aliases in SYN.items():
        candidate = next((names[a] for a in aliases if a in names), None)
        out[key] = df[candidate] if candidate is not None else None
    return out

def normalise_meter(v):
    """Keep meter IDs as text, including leading zeros; tolerate separators and Excel .0."""
    raw = text(v)
    if not raw: return ''
    raw = re.sub(r'\.0+$', '', raw)
    digits = re.sub(r'\D', '', raw)
    return digits if 6 <= len(digits) <= 13 else ''

def detect_header_row(df):
    """Find the real header row when a spreadsheet starts with a report title."""
    for i in range(min(18,len(df))):
        labels={hdr(v) for v in df.iloc[i].values if text(v)}
        if labels.intersection(set(SYN['meter'])):
            columns=[text(x) if text(x) else f'Unnamed_{j}' for j,x in enumerate(df.iloc[i])]
            found=df.iloc[i+1:].copy()
            found.columns=columns
            return found.reset_index(drop=True)
    return None

def usable_table(df):
    matched={hdr(c) for c in df.columns}
    if matched.intersection(SYN['meter']): return df
    # If there is only one column, support MPANs listed one per line without a heading.
    if len(df.columns)==1:
        first=normalise_meter(df.columns[0]); values=[first]+[normalise_meter(v) for v in df.iloc[:,0]]
        values=[v for v in values if v]
        if values: return pd.DataFrame({'MPAN':values})
    found=detect_header_row(pd.concat([pd.DataFrame([list(df.columns)],columns=df.columns),df],ignore_index=True))
    return found if found is not None else df

def uploaded_table(file):
    raw = file.getvalue()
    if file.name.lower().endswith('.csv'):
        return usable_table(pd.read_csv(io.BytesIO(raw), sep=None, engine='python', dtype=str))
    return usable_table(pd.read_excel(io.BytesIO(raw), dtype=str))

def pasted_table(raw):
    sep = '\t' if '\t' in raw.splitlines()[0] else ','
    return usable_table(pd.read_csv(io.StringIO(raw), sep=sep, dtype=str))

def imported_supply_rows(df, default_fuel):
    result = []
    for _, row in align_df(df).iterrows():
        d = row.to_dict()
        meter = normalise_meter(d['meter'])
        if not meter or meter.lower() == 'nan': continue
        d['meter'] = meter
        d['fuel'] = 'Gas' if 'gas' in text(d.get('fuel') or default_fuel).lower() else 'Electricity'
        for k in ('day_usage','night_usage','kva','day_rate','night_rate','standing','kva_rate','other_annual'):
            d[k] = num(d[k])
        for k in ('site','postcode','start_date','end_date'):
            d[k] = text(d[k])
        result.append(d)
    return result

def save_supply(cid, d):
    vals = [cid,d['fuel'],d['meter'],d.get('site'),d.get('postcode'),d.get('day_usage'),d.get('night_usage'),d.get('kva'),d.get('day_rate'),d.get('night_rate'),d.get('standing'),d.get('kva_rate'),d.get('other_annual') or 0,d.get('start_date'),d.get('end_date')]
    execute('''INSERT INTO supplies(client_id,fuel,meter,site,postcode,day_usage,night_usage,kva,day_rate,night_rate,standing,kva_rate,other_annual,start_date,end_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(client_id,fuel,meter) DO UPDATE SET site=excluded.site,postcode=excluded.postcode,day_usage=excluded.day_usage,night_usage=excluded.night_usage,kva=excluded.kva,day_rate=excluded.day_rate,night_rate=excluded.night_rate,standing=excluded.standing,kva_rate=excluded.kva_rate,other_annual=excluded.other_annual,start_date=excluded.start_date,end_date=excluded.end_date,updated=CURRENT_TIMESTAMP''',vals)

def pdf_text(raw):
    reader = PdfReader(io.BytesIO(raw))
    return '\n'.join(page.extract_text() or '' for page in reader.pages[:35])

def extract_offer_text(fname, raw):
    name = fname.lower()
    if name.endswith('.pdf'): return pdf_text(raw)
    if name.endswith(('.txt','.csv')): return raw.decode('utf-8',errors='replace')
    if name.endswith(('.xlsx','.xls')):
        df = pd.read_excel(io.BytesIO(raw), header=None)
        return '\n'.join(' | '.join(text(c) for c in row if text(c)) for row in df.fillna('').values.tolist())
    if name.endswith('.eml'):
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        parts=[]
        if mail['subject']: parts.append(f"Subject: {mail['subject']}")
        for part in mail.walk():
            if part.is_multipart(): continue
            ctype=part.get_content_type()
            filename=part.get_filename() or ''
            try:
                if filename.lower().endswith(('.pdf','.txt','.csv','.xlsx','.xls')):
                    data=part.get_payload(decode=True)
                    parts.append(f'\nATTACHMENT {filename}:\n'+extract_offer_text(filename,data))
                elif ctype=='text/plain' and not filename:
                    parts.append(str(part.get_content()))
            except Exception as ex:
                parts.append(f'[Could not parse attachment {filename}: {ex}]')
        return '\n'.join(parts)
    return ''

# Decline extraction suggests supplier names; the operator can review/edit before saving.
DECLINE_SUPPLIERS = {
    'British Gas': [r'\bbritish\s+gas\b',r'@britishgas\.co\.uk',r'\bBG\b'],
    'SSE': [r'\bSSE\b',r'@sse\.com'],
    'EDF': [r'\bEDF\b',r'@edfenergy\.com'],
    'Npower': [r'\bnpower\b',r'@npower\.com'],
    'Drax': [r'\bdrax\b'],
    'SEFE': [r'\bsefe\b'],
    'Shell': [r'\bshell\b'],
    'Ecotricity': [r'\becotricity\b'],
    'Crown': [r'\bcrown\s+(?:gas|energy)\b'],
    'Dyce': [r'\bdyce\b'],
    'UGP': [r'\bUGP\b',r'\butility\s+group\b'],
    'TEM': [r'\btem[- ]energy\b'],
    'TotalEnergies': [r'\btotal\s*energies\b'],
    'E.ON Next': [r'\be\.?on\s+next\b'],
    'Brook Green': [r'\bbrook\s*green\b'],
    'Osso': [r'\bosso\b'],
    'Volters': [r'\bvolters?\b'],
    'F & S Energy': [r'\bf\s*(?:&|and)\s*s\s+energy\b'],
    'Conrad Energy': [r'\bconrad\s+energy\b'],
    'Marble': [r'\bmarble\s+(?:energy|power)\b'],
    'Yu Energy': [r'\byu\s+energy\b'],
    'UK Gas Supply': [r'\buk\s+gas\s+supply\b']
}
DECLINE_SIGNAL = re.compile(r'\b(?:declin(?:e|ed|ing)|unable to (?:quote|offer|price)|cannot (?:quote|offer|price)|not (?:able to|in a position to) (?:quote|offer|price)|no quote|not quoting|will not (?:quote|offer|price)|unable to proceed|credit declined|outside our (?:risk|credit) appetite|not competitive)\b',re.I)

def find_decline_suppliers(chunk):
    return [name for name,patterns in DECLINE_SUPPLIERS.items() if any(re.search(pattern,chunk,re.I) for pattern in patterns)]

def decline_suggestions(source, filename, sender=''):
    lines=[line.strip() for line in (source or '').splitlines() if line.strip()]
    # Extract named suppliers near decline statements, or from the sender if unique.
    suggestions=[]
    sender_supplier=find_decline_suppliers(sender)
    for i,line in enumerate(lines):
        if not DECLINE_SIGNAL.search(line):continue
        context=' '.join(lines[max(0,i-2):min(len(lines),i+3)])
        names=find_decline_suppliers(context)
        if not names and len(sender_supplier)==1:
            names=sender_supplier
        # A general statement like "SSE, EDF and Dyce declined" creates three proposals.
        for name in (names or ['']):
            reason=line[:600]
            fuel='Gas' if re.search(r'\bgas\b',context,re.I) and not re.search(r'\belectri(?:c|city)\b',context,re.I) else 'Electricity'
            suggestions.append({'Include':True,'Supplier':name,'Fuel':fuel,'Reason':reason,'Source':filename})
    if not suggestions:
        suggestions.append({'Include':True,'Supplier':sender_supplier[0] if len(sender_supplier)==1 else '', 'Fuel':'Electricity','Reason':'','Source':filename})
    # Remove exact duplicates while retaining multiple independent decline reasons.
    out=[]
    seen=set()
    for row in suggestions:
        key=(row['Supplier'],row['Fuel'],row['Reason'],row['Source'])
        if key not in seen:
            out.append(row)
            seen.add(key)
    return out

def first_number(patterns, s):
    for p in patterns:
        m = re.search(p,s,re.I)
        if m:
            x=num(m.group(1))
            if x is not None: return x
    return None

def guess_details(s):
    # Extraction is a starting point only; all values must be reviewed by a person.
    mpans=re.findall(r'(?<!\d)(?:\d[\s-]?){12}\d(?!\d)',s)
    meters=list(dict.fromkeys(re.sub(r'\D','',m) for m in mpans))
    terms=[]
    for pat in [r'(?<!\d)(12|24|36|48|60)\s*(?:months|month|mth|mths)\b',r'(?<!\d)(1|2|3|4|5)\s*(?:years|year|yrs|yr)\b']:
        for m in re.finditer(pat,s,re.I):
            v=int(m.group(1)) * (12 if 'year' in m.group(0).lower() or 'yr' in m.group(0).lower() else 1)
            if v not in terms:terms.append(v)
    patterns = {
        'day_rate':[r'\bday\s*(?:unit\s*)?rate\s*[:=]?\s*([\d,.]+)\s*p',r'\bday\s*[:=]\s*([\d,.]+)'],
        'night_rate':[r'\bnight\s*(?:unit\s*)?rate\s*[:=]?\s*([\d,.]+)\s*p',r'\bnight\s*[:=]\s*([\d,.]+)'],
        'standing':[r'\bstanding\s*(?:charge)?\s*[:=]?\s*([\d,.]+)\s*p\s*(?:/|per)?\s*day'],
        'kva_rate':[r'\b(?:kva|capacity)\s*rate\s*[:=]?\s*([\d,.]+)\s*p\s*(?:/|per)?\s*(?:kva)?\s*(?:/|per)?\s*day'],
    }
    return {'meters':meters,'terms':terms, **{k:first_number(v,s) for k,v in patterns.items()}}

def offer_cost(o, sup, client):
    day=num(sup.get('day_usage'))
    night=num(sup.get('night_usage')) or 0
    if day is None: return None
    uplift=float(client['elec_uplift'] if sup['fuel']=='Electricity' else client['gas_uplift'])
    dr=num(o.get('day_rate'))
    nr=num(o.get('night_rate'))
    if dr is None: return None
    unit = ((day+night)*(dr+uplift) if nr is None else day*(dr+uplift)+night*(nr+uplift))/100
    standing=(num(o.get('standing')) or 0)*365/100
    kva=(num(sup.get('kva')) or 0)*(num(o.get('kva_rate')) or 0)*365/100
    other=num(o.get('other_annual')) or 0
    return round(unit+standing+kva+other,2)

def export_file(client,supplies,offers,declines,supplier_terms):
    buffer=io.BytesIO()
    with pd.ExcelWriter(buffer,engine='xlsxwriter',datetime_format='dd/mm/yyyy') as writer:
        book=writer.book
        heading=book.add_format({'bold':True,'font_color':'white','bg_color':'#174E74','border':0,'align':'center','valign':'vcenter'})
        fmt_money=book.add_format({'num_format':'£#,##0.00'})
        fmt_num=book.add_format({'num_format':'#,##0.00'})
        def tab(name,rows,cols):
            df=pd.DataFrame(rows,columns=cols)
            df.to_excel(writer,sheet_name=name,index=False)
            ws=writer.sheets[name]
            ws.freeze_panes(1,0)
            ws.set_row(0,28,heading)
            ws.autofilter(0,0,max(1,len(df)),len(cols)-1)
            for i,col in enumerate(cols):
                width=min(35,max(14,len(col)+3))
                if 'Site' in col:width=35
                ws.set_column(i,i,width)
            return ws
        current=[]
        for s in supplies:
            current.append({'Fuel':s['fuel'],'Site':s['site'],'Postcode':s['postcode'],'MPAN / MPRN':s['meter'],'Day usage (kWh)':s['day_usage'],'Night usage (kWh)':s['night_usage'],'kVA':s['kva'],'Day / single (p/kWh)':s['day_rate'],'Night (p/kWh)':s['night_rate'],'Standing (p/day)':s['standing'],'kVA rate (p/kVA/day)':s['kva_rate'],'Other annual (£)':s['other_annual'],'Contract start':s['start_date'],'Contract end':s['end_date']})
        currentcols=['Fuel','Site','Postcode','MPAN / MPRN','Day usage (kWh)','Night usage (kWh)','kVA','Day / single (p/kWh)','Night (p/kWh)','Standing (p/day)','kVA rate (p/kVA/day)','Other annual (£)','Contract start','Contract end']
        tab('Current Rates',current,currentcols)
        allrows=[]
        for o in offers:
            s=next((s for s in supplies if s['id']==o['supply_id']),None)
            if not s:continue
            up=client['elec_uplift'] if s['fuel']=='Electricity' else client['gas_uplift']
            cost=offer_cost(o,s,client)
            allrows.append({'Fuel':s['fuel'],'Site':s['site'],'MPAN / MPRN':s['meter'],'Supplier':o['supplier'],'Term (months)':o['term'],'Annual day kWh':s['day_usage'],'Annual night kWh':s['night_usage'],'kVA':s['kva'],'Quoted day / single p/kWh':o['day_rate'],'Quoted night p/kWh':o['night_rate'],'Uplift p/kWh':up,'Applied day / single p/kWh':None if o['day_rate'] is None else round(o['day_rate']+up,6),'Applied night p/kWh':None if o['night_rate'] is None else round(o['night_rate']+up,6),'Standing p/day':o['standing'],'kVA rate p/kVA/day':o['kva_rate'],'Other annual £':o['other_annual'],'Annual cost £':cost,'Rank':None,'Top 3':None,'Quote date':o['quote_date'],'Start date':o['start_date'],'End date':o['end_date'],'Notes':o['notes']})
        # Show each meter for every standard term, even when no quotation has been saved.
        # Additional quoted terms (e.g. 48/60 months) are included automatically.
        covered={(r['Fuel'],r['MPAN / MPRN'],r['Term (months)']) for r in allrows}
        for supply in supplies:
            for months in (12,24,36):
                key=(supply['fuel'],supply['meter'],months)
                if key in covered:
                    continue
                allrows.append({'Fuel':supply['fuel'],'Site':supply['site'],
                    'MPAN / MPRN':supply['meter'],'Supplier':'NO SAVED OFFER',
                    'Term (months)':months,'Annual day kWh':supply['day_usage'],
                    'Annual night kWh':supply['night_usage'],'kVA':supply['kva'],
                    'Quoted day / single p/kWh':None,'Quoted night p/kWh':None,
                    'Uplift p/kWh':None,'Applied day / single p/kWh':None,
                    'Applied night p/kWh':None,'Standing p/day':None,
                    'kVA rate p/kVA/day':None,'Other annual £':None,
                    'Annual cost £':None,'Rank':None,'Top 3':None,
                    'Quote date':None,'Start date':None,'End date':None,
                    'Notes':'No quotation has been saved for this meter and term.'})
        for fuel in ('Electricity','Gas'):
            rows=[r for r in allrows if r['Fuel']==fuel]
            keys=set((r['MPAN / MPRN'],r['Term (months)']) for r in rows)
            for meter,term in keys:
                ranked=sorted((r for r in rows if r['MPAN / MPRN']==meter and r['Term (months)']==term and r['Annual cost £'] is not None),key=lambda r:r['Annual cost £'])
                for rank,r in enumerate(ranked,1):
                    r['Rank']=rank
                    r['Top 3']='YES' if rank<=3 else ''
            cols=['Fuel','Site','MPAN / MPRN','Supplier','Term (months)','Annual day kWh','Annual night kWh','kVA','Quoted day / single p/kWh','Quoted night p/kWh','Uplift p/kWh','Applied day / single p/kWh','Applied night p/kWh','Standing p/day','kVA rate p/kVA/day','Other annual £','Annual cost £','Rank','Top 3','Quote date','Start date','End date','Notes']
            rows.sort(key=lambda r:(r['Term (months)'],r['Site'] or '',r['Rank'] if r['Rank'] is not None else 999,r['Supplier']))
            ws=tab(fuel+' Offers',rows,cols)
            ws.set_column(16,16,18,fmt_money)
            if rows:
                ws.conditional_format(1,17,len(rows),17,{'type':'3_color_scale'})
                ws.conditional_format(1,18,len(rows),18,{'type':'text','criteria':'containing','value':'YES','format':book.add_format({'bg_color':'#D8F0DF','font_color':'#186738','bold':True})})
        declinecols=['Supplier','Fuel','Reason / notes','Recorded']
        declinerows=[{'Supplier':d.get('supplier'),'Fuel':d.get('fuel'),
            'Reason / notes':d.get('reason'),'Recorded':d.get('updated')} for d in declines]
        decline_ws=tab('Declined Suppliers',declinerows,declinecols)
        decline_ws.set_column(0,0,25)
        decline_ws.set_column(2,2,90)
        termcols=['Supplier','Fuel','Generator matching','Volume tolerance','Take-or-pay / minimum volume','Pass-through charges','Termination / exit conditions','Payment terms','Credit requirements','Quote validity','Other contractual terms','Source / reference','Updated']
        termrows=[{'Supplier':t.get('supplier'),'Fuel':t.get('fuel'),'Generator matching':t.get('generator_matching'),'Volume tolerance':t.get('volume_tolerance'),'Take-or-pay / minimum volume':t.get('take_or_pay'),'Pass-through charges':t.get('pass_through'),'Termination / exit conditions':t.get('termination'),'Payment terms':t.get('payment_terms'),'Credit requirements':t.get('credit_requirements'),'Quote validity':t.get('quote_validity'),'Other contractual terms':t.get('other_terms'),'Source / reference':t.get('source'),'Updated':t.get('updated')} for t in supplier_terms]
        ws=tab('Supplier Terms',termrows,termcols)
        ws.set_column(2,11,30)
        ws.set_column(10,10,45)
    return buffer.getvalue()

st.title('⚡ AU Client Offer Manager')
st.caption('Client-by-client quotations • current rates • supplier uploads • adjustable uplift • Excel comparison')
clients=fetch('SELECT * FROM clients ORDER BY name')
if not DATABASE_URL:
    st.error('⚠ Temporary test database: client records can disappear after a Render restart. Set DATABASE_URL to a hosted PostgreSQL connection string before relying on saved data.')
else:
    st.caption('✅ Persistent PostgreSQL database is configured.')
if not clients:
    st.warning('No clients are stored in this deployment. On Render free hosting, the local database can be reset after a restart or redeploy. Add a test client below. For permanent client records, configure persistent storage before using real client data.')

tabs=st.tabs(['👥 Clients','📋 Current Rates','📨 Offers','📑 Supplier Terms','🚫 Declines','📊 Comparison & Export'])
with tabs[0]:
    st.subheader('Create new client')
    with st.form('newclient'):
        n=st.text_input('Client name',placeholder='e.g. Interhaze Limited')
        a,b=st.columns(2)
        e=a.number_input('Default electricity uplift (p/kWh)',value=0.0,step=0.1)
        g=b.number_input('Default gas uplift (p/kWh)',value=0.0,step=0.1)
        if st.form_submit_button('Add client',type='primary'):
            if n.strip():
                try:
                    execute('INSERT INTO clients(name,elec_uplift,gas_uplift) VALUES(?,?,?)',(n.strip(),e,g))
                    st.success(f'Added {n.strip()}');st.rerun()
                except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation): st.error('A client with that name already exists.')
            else:st.warning('Please enter a name.')
    st.markdown('**Or upload a list of new clients**')
    bulk=st.file_uploader('Client list (.csv or .xlsx). Include a Name column; optional Electricity Uplift and Gas Uplift columns.',type=['csv','xlsx'],key='bulkupload')
    if bulk:
        try:
            bdf=uploaded_table(bulk)
            names={hdr(k):k for k in bdf.columns}
            namecol=next((names[k] for k in ['name','client','clientname','customer'] if k in names),None)
            ecol=next((names[k] for k in ['electricityuplift','elecuplift','electricityupliftpkwh'] if k in names),None)
            gcol=next((names[k] for k in ['gasuplift','gasupliftpkwh'] if k in names),None)
            if not namecol: st.warning('Your upload needs a Name or Client column.')
            else:
                st.dataframe(bdf.head(15),hide_index=True,use_container_width=True)
                if st.button('Import new clients'):
                    count=0
                    for _,r in bdf.iterrows():
                        name=text(r[namecol])
                        if not name:continue
                        try:
                            execute('INSERT INTO clients(name,elec_uplift,gas_uplift) VALUES(?,?,?)',(name,num(r[ecol]) if ecol else 0,num(r[gcol]) if gcol else 0))
                            count+=1
                        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):pass
                    st.success(f'Created {count} new client(s); existing clients were left unchanged.')
                    st.rerun()
        except Exception as ex:st.error(f'Cannot read client list: {ex}')
    if clients:
        st.subheader('Client settings')
        selected_name=st.selectbox('Client to edit', [x['name'] for x in clients],key='settingsclient')
        selected=next(x for x in clients if x['name']==selected_name)
        with st.form('settings'):
            col1,col2=st.columns(2)
            x=col1.number_input('Electricity uplift (p/kWh)',value=float(selected['elec_uplift']),step=0.1)
            y=col2.number_input('Gas uplift (p/kWh)',value=float(selected['gas_uplift']),step=0.1)
            if st.form_submit_button('Save client uplift'):
                execute('UPDATE clients SET elec_uplift=?,gas_uplift=? WHERE id=?',(x,y,selected['id']))
                st.success('Saved. Costs and rankings will automatically use the new uplift.');st.rerun()
        st.dataframe(pd.DataFrame(clients)[['name','elec_uplift','gas_uplift']],hide_index=True,use_container_width=True)

# Keep the active client consistent across all other tabs.
clients=fetch('SELECT * FROM clients ORDER BY name')
if clients:
    st.sidebar.header('👥 Your clients')
    st.sidebar.caption(f'{len(clients)} client(s) currently saved')
    active_name=st.selectbox('ACTIVE CLIENT — select a client here or use the sidebar', [x['name'] for x in clients],key='activeclient')
    st.sidebar.info(f'Active client: {active_name}')
    client=next(x for x in clients if x['name']==active_name)
    cid=client['id']
    st.sidebar.success(active_name)
    st.sidebar.markdown('### Change uplift for this client')
    st.sidebar.caption('Enter pence per kWh. For example 1.0 = +1p; 0 = no uplift. Changes apply to all existing and future offers for the selected client.')
    with st.sidebar.form(f"uplift_quick_{cid}"):
        new_elec = st.number_input('Electricity uplift (p/kWh)', min_value=0.0, max_value=100.0, value=float(client['elec_uplift']), step=0.1, format='%.3f', key=f'electric_uplift_{cid}')
        new_gas = st.number_input('Gas uplift (p/kWh)', min_value=0.0, max_value=100.0, value=float(client['gas_uplift']), step=0.1, format='%.3f', key=f'gas_uplift_{cid}')
        if st.form_submit_button('💾 Save uplift', use_container_width=True):
            execute('UPDATE clients SET elec_uplift=?, gas_uplift=? WHERE id=?',(new_elec,new_gas,cid))
            st.rerun()
    st.sidebar.caption('Quoted supplier rates are preserved; annual costs, ranks and Excel exports use the saved uplift.')
    supplies=fetch('SELECT * FROM supplies WHERE client_id=? ORDER BY fuel,site,meter',(cid,))
    supply_labels={s['id']:f"{s['fuel']} | {s['meter']} | {s['site'] or '-'}" for s in supplies}
else:
    st.sidebar.warning('No saved clients. Open the Clients tab to add one. On a free Render deployment, records may be lost after redeployment.')
    client=None;cid=None;supplies=[];supply_labels={}

with tabs[1]:
    st.subheader(f'Current rates & supplies — {client["name"]}' if client else 'Current rates & supplies')
    if client:
        st.write('Upload an Excel/CSV file or paste a table copied from Excel. The first row should contain column headings such as **MPAN Bottom Line, Site Name, Day Usage, Night Usage, KVA, Day Rate, Night Rate, Standing Charge**.')
        source=st.radio('Import method',['Paste table','Upload Excel / CSV','Enter one supply'],horizontal=True,key='current_source')
        df=None
        default=st.radio('Default fuel when not listed in source',['Electricity','Gas'],horizontal=True,key='fuelcurrent')
        if source=='Paste table':
            pasted=st.text_area('Paste cells with headings',height=180,key='currentpaste')
            if pasted.strip():
                try:df=pasted_table(pasted)
                except Exception as ex:st.error(f'Could not parse table: {ex}')
        elif source=='Upload Excel / CSV':
            u=st.file_uploader('Current rates spreadsheet',type=['csv','xlsx','xls'],key='cupload')
            if u:
                try:df=uploaded_table(u)
                except Exception as ex:st.error(f'Could not open file: {ex}')
        else:
            with st.form('manual_supply'):
                c1,c2,c3=st.columns(3)
                d={'fuel':default,'meter':c1.text_input('MPAN or MPRN *'),'site':c2.text_input('Site name'),'postcode':c3.text_input('Postcode')}
                d['day_usage']=c1.number_input('Annual day / single kWh',min_value=0.,value=0.,step=1.)
                d['night_usage']=c2.number_input('Annual night kWh',min_value=0.,value=0.,step=1.)
                d['kva']=c3.number_input('kVA',min_value=0.,value=0.,step=1.)
                d['day_rate']=c1.number_input('Current day / single rate p/kWh',min_value=0.,value=0.)
                d['night_rate']=c2.number_input('Current night rate p/kWh',min_value=0.,value=0.)
                d['standing']=c3.number_input('Standing p/day',min_value=0.,value=0.)
                d['kva_rate']=c1.number_input('kVA rate p/kVA/day',min_value=0.,value=0.)
                d['other_annual']=c2.number_input('Other annual cost (£)',min_value=0.,value=0.)
                d['start_date']=c2.text_input('Contract start date (optional)')
                d['end_date']=c3.text_input('Contract end date (optional)')
                if st.form_submit_button('Save supply',type='primary'):
                    d['meter']=normalise_meter(d['meter'])
                    if d['meter']:save_supply(cid,d);st.success('Supply saved');st.rerun()
                    else:st.warning('MPAN/MPRN required')
        if df is not None:
            st.write('**Import preview**')
            rows=imported_supply_rows(df,default)
            if rows:
                st.dataframe(pd.DataFrame(rows).head(25),use_container_width=True,hide_index=True)
                st.caption(f'{len(rows)} supplies identified. Existing supplies with the same fuel and MPAN/MPRN will be updated.')
                if st.button(f'Save {len(rows)} supply records',type='primary'):
                    for row in rows:save_supply(cid,row)
                    st.success('Current rates saved');st.rerun()
            else:
                st.warning('No valid MPAN/MPRN recognised. Please check the column heading and values below.')
                st.caption('Accepted examples: MPAN, MPAN Bottom Line, MPAN Number, MPAN / MPRN, MPRN; or a single column of meter numbers.')
                st.write('Detected column headings:', list(df.columns))
                st.dataframe(df.head(5), use_container_width=True)
        st.divider()
        st.write('**Saved supplies**')
        if supplies:
            st.dataframe(pd.DataFrame(supplies).drop(columns=['client_id','updated'],errors='ignore'),use_container_width=True,hide_index=True)
        else: st.info('No meters saved for this client yet.')

with tabs[2]:
    st.subheader(f'Add or refresh supplier offers — {client["name"]}' if client else 'Add offers')
    if client:
        if not supplies:st.warning('First add a current supply/MPAN under Current Rates so quotes can be linked to the correct meter.')
        else:
            st.warning('Uploading a quotation does NOT save its rates automatically. Review and click Save / replace offer for each MPAN/MPRN and each contract term (12, 24, 36 months etc.).')
            st.caption('Accepted: PDF, EML (including supported attachments), TXT, CSV and XLSX. Extracted values must be verified before saving.')
            method=st.radio('Input method',['Upload supplier quotation','Paste offer text','Manual entry'],horizontal=True)
            rawtext=''
            filename='Manual entry'
            if method=='Upload supplier quotation':
                uploaded_files=st.file_uploader(
                    'Drag and drop supplier quotations here, or click Browse files',
                    type=['pdf','eml','txt','csv','xlsx','xls'],
                    accept_multiple_files=True,
                    key='offerfiles',
                    help='Upload multiple quotations, then review and save each offer separately.'
                )
                if uploaded_files:
                    st.success(f'{len(uploaded_files)} quotation file(s) ready for review.')
                    selected_idx=st.selectbox('Quotation to review',range(len(uploaded_files)),format_func=lambda i: uploaded_files[i].name,key='selected_offer_upload')
                    file=uploaded_files[selected_idx]
                    filename=file.name
                    try:rawtext=extract_offer_text(file.name,file.getvalue())
                    except Exception as ex:st.error(f'Could not read this quotation: {ex}')
            elif method=='Paste offer text':
                rawtext=st.text_area('Paste email / pricing details here',height=210)
                filename='Pasted text'
            guess=guess_details(rawtext) if rawtext else {'meters':[],'terms':[],'day_rate':None,'night_rate':None,'standing':None,'kva_rate':None}
            if rawtext:
                with st.expander('Extracted source text — check against supplier document'):
                    st.text_area('Extracted text',rawtext[:45000],height=300)
                if len(guess['terms'])>1:st.warning('Several contract terms found. Save a separate reviewed offer for EACH term. The form does not automatically copy one term’s rates onto another.')
                if len(guess['meters'])>1:st.warning('Several MPAN/MPRNs found. Choose the correct meter for each offer.')
            ordered=list(supply_labels)
            matching=[sid for sid in ordered if str(next(x['meter'] for x in supplies if x['id']==sid)) in guess['meters']]
            if matching:ordered=matching+[sid for sid in ordered if sid not in matching]
            with st.form('offer_entry'):
                c1,c2,c3=st.columns(3)
                chosen=c1.selectbox('Supply / MPAN / MPRN *',ordered,format_func=lambda i:supply_labels[i])
                supplier=c2.text_input('Supplier *',placeholder='e.g. Npower')
                term=c3.number_input('Contract term (months) *',min_value=1,max_value=120,value=guess['terms'][0] if guess['terms'] else 24,step=1)
                a,b,c=st.columns(3)
                day=a.number_input('Day / single unit rate (p/kWh) *',min_value=0.,value=float(guess['day_rate'] or 0.),format='%.6f')
                night=b.text_input('Night rate (p/kWh) — blank for single',value=str(guess['night_rate']) if guess['night_rate'] is not None else '')
                standing=c.number_input('Standing charge (p/day)',min_value=0.,value=float(guess['standing'] or 0.),format='%.6f')
                kva=a.number_input('kVA rate (p/kVA/day)',min_value=0.,value=float(guess['kva_rate'] or 0.),format='%.6f')
                other=b.number_input('Other fixed annual charge (£)',value=0.,format='%.2f')
                qdate=c.date_input('Quote date',value=datetime.today()).isoformat()
                start=a.text_input('Actual contract start (optional)')
                end=b.text_input('Actual contract end (optional)')
                notes=c.text_input('Notes / exclusions')
                st.info(f"Client uplift applied automatically to calculations: electricity **{client['elec_uplift']}p/kWh**, gas **{client['gas_uplift']}p/kWh**. Enter raw supplier rates without uplift.")
                if st.form_submit_button('Save / replace offer',type='primary'):
                    if not supplier.strip() or day<=0:st.error('Supply, supplier and a positive day/single rate are required.')
                    elif night.strip() and num(night) is None:st.error('Night rate must be numeric or blank.')
                    else:
                        execute('''INSERT INTO offers(client_id,supply_id,supplier,term,day_rate,night_rate,standing,kva_rate,other_annual,start_date,end_date,quote_date,source,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(client_id,supply_id,supplier,term) DO UPDATE SET day_rate=excluded.day_rate,night_rate=excluded.night_rate,standing=excluded.standing,kva_rate=excluded.kva_rate,other_annual=excluded.other_annual,start_date=excluded.start_date,end_date=excluded.end_date,quote_date=excluded.quote_date,source=excluded.source,notes=excluded.notes,updated=CURRENT_TIMESTAMP''',
                        (cid,chosen,supplier.strip(),int(term),day,num(night),standing,kva,other,start,end,qdate,filename,notes))
                        st.success(f'Saved {supplier} {term}-month offer. Any previous offer for the same meter, supplier and term has been replaced.');st.rerun()
            offers=fetch('SELECT * FROM offers WHERE client_id=? ORDER BY term,supplier',(cid,))
            if offers:
                st.divider();st.write('**Saved offers**')
                st.dataframe(pd.DataFrame(offers).drop(columns=['client_id'],errors='ignore'),hide_index=True,use_container_width=True)
                dmap={o['id']:f"{o['supplier']} – {o['term']}m – {supply_labels.get(o['supply_id'],'')}" for o in offers}
                todel=st.selectbox('Select an offer to delete (optional)',list(dmap),format_func=lambda x:dmap[x])
                if st.button('Delete selected offer'):
                    execute('DELETE FROM offers WHERE id=? AND client_id=?',(todel,cid));st.rerun()

with tabs[3]:
    st.subheader('Supplier contractual terms')
    st.caption('Record generator matching, volume tolerance and other supply conditions. Information is not assumed or inferred from rates.')
    if client:
        existing=fetch('SELECT * FROM supplier_terms WHERE client_id=? ORDER BY supplier,fuel',(cid,))
        with st.form('terms_entry'):
            c1,c2=st.columns(2)
            term_supplier=c1.text_input('Supplier name *',placeholder='e.g. EDF')
            term_fuel=c2.selectbox('Applies to',['Both','Electricity','Gas'])
            c1,c2=st.columns(2)
            generator=c1.text_area('Generator matching / renewable matching',height=80)
            volume=c2.text_area('Volume tolerance (e.g. 80–120%)',height=80)
            takepay=c1.text_input('Take-or-pay / minimum consumption')
            passthrough=c2.text_input('Pass-through charges / exclusions')
            termination=c1.text_input('Termination / early exit')
            payment=c2.text_input('Payment terms')
            credit=c1.text_input('Credit requirements / deposit')
            validity=c2.text_input('Quote validity')
            other=st.text_area('Other supplier terms / special conditions',height=80)
            source=st.text_input('Source / quotation reference')
            if st.form_submit_button('Save / update supplier terms',type='primary'):
                if not term_supplier.strip():st.error('Supplier name is required.')
                else:
                    execute('''INSERT INTO supplier_terms(client_id,supplier,fuel,generator_matching,volume_tolerance,take_or_pay,pass_through,termination,payment_terms,credit_requirements,quote_validity,other_terms,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(client_id,supplier,fuel) DO UPDATE SET generator_matching=excluded.generator_matching,volume_tolerance=excluded.volume_tolerance,take_or_pay=excluded.take_or_pay,pass_through=excluded.pass_through,termination=excluded.termination,payment_terms=excluded.payment_terms,credit_requirements=excluded.credit_requirements,quote_validity=excluded.quote_validity,other_terms=excluded.other_terms,source=excluded.source,updated=CURRENT_TIMESTAMP''',
                    (cid,term_supplier.strip(),term_fuel,generator,volume,takepay,passthrough,termination,payment,credit,validity,other,source))
                    st.success('Supplier terms saved');st.rerun()
        if existing:
            st.dataframe(pd.DataFrame(existing).drop(columns=['client_id'],errors='ignore'),hide_index=True,use_container_width=True)
            choices={t['id']:f"{t['supplier']} — {t['fuel']}" for t in existing}
            selection=st.selectbox('Remove a supplier terms record (optional)',list(choices),format_func=lambda x:choices[x])
            if st.button('Delete selected supplier terms'):
                execute('DELETE FROM supplier_terms WHERE id=? AND client_id=?',(selection,cid));st.rerun()

with tabs[4]:
    st.subheader('Declined supplier log')
    if client:
        decline_method=st.radio('Add a decline',['Drag and drop / upload','Paste decline','Manual entry'],horizontal=True,key='decline_method')
        review_rows=[]
        if decline_method=='Drag and drop / upload':
            decline_files=st.file_uploader('Drag and drop decline emails or documents (multiple suppliers supported)',type=['eml','pdf','txt','csv','xlsx','xls'],accept_multiple_files=True,key='decline_files')
            for item in (decline_files or []):
                try:
                    raw=item.getvalue()
                    sender=''
                    if item.name.lower().endswith('.eml'):
                        sender=str(BytesParser(policy=policy.default).parsebytes(raw).get('From',''))
                    review_rows.extend(decline_suggestions(extract_offer_text(item.name,raw),item.name,sender))
                except Exception as ex:
                    st.error(f'Could not read {item.name}: {ex}')
        elif decline_method=='Paste decline':
            raw_decline=st.text_area('Paste supplier decline email(s) here',height=180,key='decline_paste')
            if raw_decline.strip():
                review_rows=decline_suggestions(raw_decline,'Pasted decline')
        if review_rows:
            st.info('Supplier names, fuel and reasons below are suggestions extracted from the documents. Check and edit each row before saving. A blank supplier means the system could not identify it reliably.')
            edited=st.data_editor(pd.DataFrame(review_rows),hide_index=True,use_container_width=True,key='decline_review_editor',
                column_config={'Include':st.column_config.CheckboxColumn('Save?',default=True),
                               'Fuel':st.column_config.SelectboxColumn('Fuel',options=['Electricity','Gas','Both'],required=True)},
                num_rows='dynamic')
            if st.button('Save reviewed supplier declines',type='primary'):
                saved=0
                incomplete=0
                for _,row in edited.iterrows():
                    if not row.get('Include',True):continue
                    supplier=str(row.get('Supplier') or '').strip()
                    reason=str(row.get('Reason') or '').strip()
                    fuel=str(row.get('Fuel') or 'Electricity')
                    source=str(row.get('Source') or '')
                    if not supplier or not reason or fuel not in ['Electricity','Gas','Both']:
                        incomplete+=1
                        continue
                    if source:reason+=f' [Source: {source}]'
                    execute('INSERT INTO declines(client_id,supplier,fuel,reason) VALUES(?,?,?,?)',(cid,supplier,fuel,reason))
                    saved+=1
                if incomplete:st.warning(f'{incomplete} incomplete row(s) skipped; please fill in their supplier and reason.')
                if saved:
                    st.success(f'Saved {saved} reviewed supplier decline(s).')
                    st.rerun()
        with st.expander('Enter a decline manually'):
            with st.form('declined'):
                c1,c2=st.columns(2)
                supplier=c1.text_input('Supplier name')
                fuel=c2.selectbox('Fuel',['Electricity','Gas','Both'])
                why=st.text_area('Reason / notes',height=90)
                if st.form_submit_button('Record decline'):
                    if not supplier.strip() or not why.strip():st.error('Supplier and reason are required.')
                    else:
                        execute('INSERT INTO declines(client_id,supplier,fuel,reason) VALUES(?,?,?,?)',(cid,supplier.strip(),fuel,why.strip()))
                        st.rerun()
        d=fetch('SELECT supplier,fuel,reason,updated FROM declines WHERE client_id=? ORDER BY updated DESC',(cid,))
        if d:st.dataframe(pd.DataFrame(d),hide_index=True,use_container_width=True)

with tabs[5]:
    st.subheader('Like-for-like comparison and Excel export')
    if client:
        offers=fetch('SELECT * FROM offers WHERE client_id=?',(cid,))
        declines=fetch('SELECT supplier,fuel,reason,updated FROM declines WHERE client_id=?',(cid,))
        compare=[]
        for o in offers:
            sup=next((s for s in supplies if s['id']==o['supply_id']),None)
            if not sup:continue
            compare.append({'Fuel':sup['fuel'],'Site':sup['site'],'MPAN / MPRN':sup['meter'],'Term (months)':o['term'],'Supplier':o['supplier'],'Day / single p/kWh':o['day_rate']+(client['elec_uplift'] if sup['fuel']=='Electricity' else client['gas_uplift']) if o['day_rate'] is not None else None,'Night p/kWh':o['night_rate']+(client['elec_uplift'] if sup['fuel']=='Electricity' else client['gas_uplift']) if o['night_rate'] is not None else None,'Annual cost £':offer_cost(o,sup,client),'Rank':None})
        if compare:
            frame=pd.DataFrame(compare)
            frame['Rank']=frame.groupby(['Fuel','MPAN / MPRN','Term (months)'])['Annual cost £'].rank(method='min',ascending=True)
            frame=frame.sort_values(['Fuel','Term (months)','Site','Rank'],na_position='last')
            sel=st.multiselect('Filter by term',sorted(frame['Term (months)'].unique().tolist()),default=sorted(frame['Term (months)'].unique().tolist()))
            st.dataframe(frame[frame['Term (months)'].isin(sel)],hide_index=True,use_container_width=True)
            st.caption('Annual costs use saved annual usage, quoted rates plus the selected client uplift, 365 days standing and capacity charges, and other annual fixed charges. Comparisons are per MPAN and per term.')
        else:st.info('Add supplier offers to view a ranked comparison.')
        supplier_terms=fetch('SELECT * FROM supplier_terms WHERE client_id=? ORDER BY supplier,fuel',(cid,))
        result=export_file(client,supplies,offers,declines,supplier_terms)
        st.download_button('⬇ Download comparison Excel',result,file_name=f"{re.sub('[^A-Za-z0-9]+','_',client['name'])}_Offers_Comparison.xlsx",mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',type='primary')
        st.caption('Five tabs: Current Rates, Electricity Offers, Gas Offers, Declined Suppliers, Supplier Terms. All meters appear under 12/24/36 months even if an offer has not yet been saved.')
