"""Per day, from your local transcripts: browser screenshots and their real image
tokens (from the returned image size), main chat vs sub-agents, and which
sub-agent types Claude picked. Use it to check screenshot_scale_cap and lean-browser.

usage: python3 scripts/measure_browser.py [SINCE_EPOCH]   (default: last 7 days)"""
import json,glob,os,sys,time,collections
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tokentrim.replay import image_dims,image_tokens
since=float(sys.argv[1]) if len(sys.argv)>1 else time.time()-7*86400
D=collections.defaultdict(lambda:collections.Counter())
for f in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl'))+glob.glob(os.path.expanduser('~/.claude/projects/*/*/subagents/*.jsonl')):
    if os.path.getmtime(f)<since:continue
    sub='/subagents/' in f
    names={}
    for l in open(f,errors='ignore'):
        try:d=json.loads(l)
        except Exception:continue
        ts=d.get('timestamp');m=d.get('message') or {}
        if not ts:continue
        t=time.mktime(time.strptime(ts[:19],'%Y-%m-%dT%H:%M:%S'))-time.timezone
        if t<since:continue
        day=ts[:10]
        c=m.get('content')
        if not isinstance(c,list):continue
        for b in c:
            if not isinstance(b,dict):continue
            if b.get('type')=='tool_use':
                names[b['id']]=b['name']
                if b['name'] in('Agent','Task'):
                    D[day]['agent:'+str((b.get('input')or{}).get('subagent_type'))]+=1
                if 'Browser' in b['name'] or 'chrome' in b['name']:
                    D[day]['browser_calls'+('_sub' if sub else '_main')]+=1
            elif b.get('type')=='tool_result':
                n=names.get(b.get('tool_use_id'),'')
                if not('Browser' in n or 'chrome' in n):continue
                for p in b.get('content') if isinstance(b.get('content'),list) else []:
                    if isinstance(p,dict) and p.get('type')=='image':
                        dims=image_dims((p.get('source')or{}).get('data',''))
                        if dims:
                            k='_sub' if sub else '_main'
                            D[day]['shots'+k]+=1;D[day]['shot_tok'+k]+=image_tokens(*dims)
for day in sorted(D):
    c=D[day];print(day,dict(c))
