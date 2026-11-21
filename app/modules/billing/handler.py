from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router = APIRouter()


@router.get('/billing')
def page(request: Request):
    return render(request, 'billing')


@router.post('/api/billing')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=7)
