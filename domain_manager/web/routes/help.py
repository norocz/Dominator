"""Nápověda — /help"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/help")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _require_user(request: Request) -> str:
    from fastapi import HTTPException
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
async def help_page(request: Request, user: str = Depends(_require_user)):
    return templates.TemplateResponse("help.html", {"request": request, "user": user})
