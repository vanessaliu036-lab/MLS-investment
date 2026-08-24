"""引擎軌進場基準回測:MA20(月線) vs MA5 —— 2026-08-24 決策依據,可重跑。

用法:先把引擎 DB 抓下來(唯讀備份,不要直接讀線上檔):
    ssh mls "cd /opt/mls-screen && python3 -c \"
    import sqlite3;c=sqlite3.connect('file:mls.db?mode=ro',uri=True)
    b=sqlite3.connect('/tmp/snap.db');c.backup(b);b.close();c.close()\""
    scp mls:/tmp/snap.db <SCRATCH>/engine.db
然後改下面的 S 指到那個目錄再跑。

結論見 memory/entry-line-ma20-vs-ma5-backtest.md:MA5 每一項都更差,不換;
但「等回測才進」本身把選股 edge 吃掉(不等直接買 +0.59% vs 等 MA20 -0.26%)。
"""
import sqlite3, statistics as st
S='/private/tmp/claude-501/-Users-vanessaliu-Desktop-mls-intraday/b5acda3b-a0f5-4a36-81f9-8098105d32f9/scratchpad'
c=sqlite3.connect(S+'/engine.db')
bars={}
for code,d,o,h,l,cl,ma5,ma20 in c.execute(
    "select code,data_date,open,high,low,close,ma5,ma20 from daily_bar"):
    bars.setdefault(code,{})[d]=dict(o=o,h=h,l=l,c=cl,ma5=ma5,ma20=ma20)
dates=sorted({d for m in bars.values() for d in m})
nxt={d:dates[i+1] for i,d in enumerate(dates[:-1])}
nxt2={d:dates[i+2] for i,d in enumerate(dates[:-2])}

rows=c.execute("select data_date,code,track,trigger_price from candidate_pool "
               "where track='引擎軌'").fetchall()
res={'ma20':[], 'ma5':[]}
trig_is_ma20=0; n=0; skipped=0
for d0,code,track,trig in rows:
    b0=bars.get(code,{}).get(d0)
    d1=nxt.get(d0)
    if not b0 or not d1: skipped+=1; continue
    b1=bars.get(code,{}).get(d1)
    if not b1 or not b0.get('ma20') or not b0.get('ma5'): skipped+=1; continue
    n+=1
    if trig and abs(trig-b0['ma20'])<0.01: trig_is_ma20+=1
    d2=nxt2.get(d0); b2=bars.get(code,{}).get(d2) if d2 else None
    for key,line in (('ma20',b0['ma20']),('ma5',b0['ma5'])):
        reach = b1['l'] is not None and b1['l']<=line
        hold  = reach and b1['c']>=line
        r1 = (b1['c']/line-1)*100 if reach else None
        r2 = (b2['c']/line-1)*100 if (reach and b2) else None
        res[key].append(dict(reach=reach,hold=hold,r1=r1,r2=r2,
                             gap=(b0['c']/line-1)*100))
print(f'引擎軌樣本 {n} 筆(池日 {dates[0]}~{dates[-1]},{len(dates)} 個交易日);略過 {skipped}')
print(f'trigger_price == MA20 的比例: {trig_is_ma20}/{n}')
print()
hdr=f"{'基準':<6}{'距線中位%':>10}{'T+1觸及率':>11}{'觸及且收在線上':>15}{'進場後T+1報酬中位%':>20}{'T+2報酬中位%':>15}"
print(hdr); print('-'*len(hdr)*2)
for key in ('ma20','ma5'):
    a=res[key]
    reach=[x for x in a if x['reach']]
    hold=[x for x in a if x['hold']]
    r1=[x['r1'] for x in reach if x['r1'] is not None]
    r2=[x['r2'] for x in reach if x['r2'] is not None]
    print(f"{key.upper():<6}{st.median([x['gap'] for x in a]):>10.1f}"
          f"{len(reach)/len(a)*100:>10.1f}%{(len(hold)/len(reach)*100 if reach else 0):>14.1f}%"
          f"{(st.median(r1) if r1 else float('nan')):>20.2f}{(st.median(r2) if r2 else float('nan')):>15.2f}")

print()
print('=== 對照組(排除大盤/動能背景) ===')
base=[]
for d0,code,track,trig in rows:
    b0=bars.get(code,{}).get(d0); d1=nxt.get(d0)
    if not b0 or not d1: continue
    b1=bars.get(code,{}).get(d1)
    if not b1: continue
    base.append((b1['c']/b0['c']-1)*100)
print(f'同一批引擎軌 T0收盤→T1收盤 報酬中位: {st.median(base):+.2f}% (n={len(base)})')
allr=[]
for code,m in bars.items():
    for d,b in m.items():
        d1=nxt.get(d)
        if d1 and d1 in m and b['c']:
            allr.append((m[d1]['c']/b['c']-1)*100)
print(f'51 檔全池 逐日 T→T+1 報酬中位: {st.median(allr):+.2f}% (n={len(allr)})')
print()
per={}
for d0,code,track,trig in rows: per[d0]=per.get(d0,0)+1
print('每個池日的引擎軌檔數:', dict(sorted(per.items())))
