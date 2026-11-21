"""Planted bugs on lines TPC-DS queries reach only in their front half. For each: every
query whose front fingerprint equals the pinned one is then executed in full; a
different result there is a VIOLATION (the front check would have been wrong)."""
import gzip,os,pickle,random,sys,subprocess,json
S=os.getcwd(); R='/home/user/buildathon-2026'
sys.path.insert(0,R+'/tools/prover'); 
from mutants import candidate_mutations
TID='tests/test_executor.py::TestExecutor::test_execute_tpcds'
if sys.argv[1]=='plan':
    qm=pickle.load(gzip.open('app4/blast/tpc_map.pkl.gz'))[TID]
    rng=random.Random(int(sys.argv[2])); pool=[]
    for path,lines in sorted(qm['line2t'].items()):
        if not path.startswith('sqlglot/') or path=='sqlglot/executor/__init__.py': continue
        back=qm['exec']['line2t'].get(path,{})
        text=open(S+'/sqlglot/'+path).read()
        for kind,line,new in candidate_mutations(path,text):
            if kind!='comment' and line in lines and line not in back: pool.append((path,kind,line,new))
    rng.shuffle(pool); print(len(pool),'candidates',file=sys.stderr)
    pickle.dump(pool[:int(sys.argv[3])],open('fb_plan.pkl','wb'))
elif sys.argv[1]=='run':
    pool=pickle.load(open('fb_plan.pkl','rb')); k,n=int(sys.argv[2]),int(sys.argv[3])
    out=[]
    for i,(path,kind,line,new) in enumerate(pool):
        if i%n!=k: continue
        r=subprocess.run([sys.executable,'frontbench.py','one'],input=pickle.dumps({path:new}),capture_output=True,env=dict(os.environ,PYTHONHASHSEED='0',PYTHONDONTWRITEBYTECODE='1'))
        try: res=pickle.loads(r.stdout)
        except Exception: res={'err':r.stderr.decode()[-300:]}
        res.update(i=i,path=path,kind=kind,line=line); out.append(res)
        print(i,path,kind,line,{a:b for a,b in res.items() if a in('same_front','checked','violations','err','import_err')},flush=True)
        pickle.dump(out,open(f'fb_out_{k}.pkl','wb'))
else:
    overlay=pickle.load(sys.stdin.buffer); src=S+'/sqlglot'
    out=os.dup(1); os.dup2(2,1)
    sys.path.insert(0,'app4/blast'); sys.path.insert(0,src)
    from importstate import _Overlay
    sys.meta_path.insert(0,_Overlay({os.path.join(src,k):v for k,v in overlay.items()}))
    os.chdir(src)
    res={'same_front':0,'checked':0,'violations':[]}
    try:
        import tpcmap as T
        exp=pickle.load(open(S+'/app4/blast/tpc_expected.pkl','rb'))[TID]
        fixture,sqls,schema=T.suite(src,TID); tables={}
        from sqlglot import find_tables, parse_one
        for q,((meta,sql,_),e) in enumerate(zip(sqls,exp)):
            if e['front'] is None: continue
            try: same=T.run_front(meta,sql,schema)==(e['duckdb'],e['front'])
            except Exception: same=False
            if not same: continue
            res['same_front']+=1
            need={t.name for t in find_tables(parse_one(sql))}-tables.keys()
            if need: tables.update(T.load_tables(src,fixture,schema,need))
            try: r,d=T.run_query(meta,sql,schema,tables)
            except Exception as x: r,d=repr(x),None
            res['checked']+=1
            if r!=e['result'] or d!=e['duckdb']: res['violations'].append(q)
    except Exception as x:
        res['import_err']=repr(x)[:200]
    os.write(out,pickle.dumps(res))
