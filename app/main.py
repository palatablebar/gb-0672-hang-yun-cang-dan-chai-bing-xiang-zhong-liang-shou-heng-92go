from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store, billing_service
from app.archive import serialize

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
def _billing_request(payload):
    request_id=payload.get('request_id')
    if not isinstance(request_id,str) or not request_id.strip():
        raise BusinessError('BAD_REQUEST_ID','请求批次号不能为空')
    basis=payload.get('billing_basis')
    if basis is not None and basis not in {'verified','declared'}:
        raise BusinessError('BAD_BILLING_BASIS',"计费基准须为 verified 或 declared")
    return request_id.strip(), basis
@app.post('/api/runs/{run_id}/corrections')
def corrections(run_id:str,payload:dict=Body(...)):
    """补正到达：登记补正、重算受影响主单、对账与封存结论同步重出。"""
    request_id,basis=_billing_request(payload)
    state=store.get(DB_PATH,run_id)
    state,info=billing_service.apply_corrections(
        state,payload.get('corrections',[]),request_id,basis)
    if not info['idempotent']:
        state=store.replace_result(DB_PATH,run_id,state)
    return {'run_id':run_id,**info,'billing':state['billing'],
            'archive':state['archive']}
@app.post('/api/runs/{run_id}/billing/rollback')
def billing_rollback(run_id:str,payload:dict=Body(...)):
    """凭证切换中断或人工撤回：回滚到上一版凭证，原申报依据可追溯。"""
    request_id,_=_billing_request(payload)
    state=store.get(DB_PATH,run_id)
    state,info=billing_service.rollback_last(state,request_id)
    if not info['idempotent']:
        state=store.replace_result(DB_PATH,run_id,state)
    return {'run_id':run_id,**info,'billing':state['billing'],
            'archive':state['archive']}
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
