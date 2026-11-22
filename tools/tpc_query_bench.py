"""Planted edits to tpc-ds.sql, checked against the real test.

    python tools/tpc_query_bench.py <sqlglot git checkout> <worker k> <of n> <count>

Plants <count> edits (tools/prover/data_mutants.mutate_sql), asks blast/tpcquery.py
about each, and runs test_execute_tpcds with pytest on the patched tree. An edit
the check calls ok whose test does not pass is printed as VIOLATION. Needs the
packages of requirements.lock; run it in the scorer's image. Set QB_ALL=1 to run
the real test for the edits the check keeps as well.
"""
import os,pickle,random,subprocess,sys,shutil,json
SQLGLOT=os.path.abspath(sys.argv[1]); S=os.path.dirname(SQLGLOT); R=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,R+'/tools/prover'); from data_mutants import mutate_sql
REL='tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
old=open(S+'/sqlglot/'+REL,encoding='utf-8').read()
k,n,count=int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4])
rng=random.Random(1234)
texts=[]
while len(texts)<count:
    t=mutate_sql(old,rng)
    if t and t!=old: texts.append(t)
res=[]
for i,text in enumerate(texts):
    if i%n!=k: continue
    r=subprocess.run([sys.executable,R+'/blast/tpcquery.py',SQLGLOT,os.environ.get('BLAST_TPC_DEPS','/nonexistent')],input=pickle.dumps({'text':text}),capture_output=True)
    try: v=pickle.loads(r.stdout)
    except Exception: v={'ok':False,'why':'crash '+r.stderr.decode()[-100:]}
    real=None
    if v['ok'] or os.environ.get('QB_ALL'):
        wt=f'{S}/qwt{k}'; shutil.rmtree(wt,ignore_errors=True)
        subprocess.run(['git','-C',SQLGLOT,'worktree','prune']); subprocess.run(['git','-C',SQLGLOT,'worktree','add','-q','--detach',wt,'HEAD'],check=True)
        open(f'{wt}/{REL}','w',encoding='utf-8',newline='').write(text)
        p=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider','tests/test_executor.py::TestExecutor::test_execute_tpcds'],cwd=wt,capture_output=True,env=dict(os.environ,SKIP_INTEGRATION='0'))
        real=p.stdout.decode().strip().splitlines()[-1]
        subprocess.run(['git','-C',SQLGLOT,'worktree','remove','--force',wt])
    bad = v['ok'] and not (real or '').startswith('1 passed')
    print(i,v,'| real:',real,'VIOLATION' if bad else '',flush=True)
