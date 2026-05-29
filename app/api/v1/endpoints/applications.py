from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

from app.core.database import get_db
from app.core.responses import error_response, success_response
from app.dependencies.auth import get_current_user, require_valid_access_token
from app.models.user import User
from app.schemas.application import AutoFillConfirmRequest, AutoFillJobCreateRequest, ApplicationCreateRequest, ApplicationUpdateRequest
from app.services.auto_fill_service import cancel_auto_fill_job, confirm_auto_fill_job, create_auto_fill_job, get_auto_fill_job
from app.services.application_service import (
    create_application,
    get_application_detail,
    get_my_by_category,
    get_my_category_summary,
    list_categories,
    list_my_applications,
    soft_delete_application,
    update_application,
    withdraw_application,
)
from app.services.errors import ServiceError

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("/categories")
def list_categories_api(request: Request, _: dict = Depends(require_valid_access_token)):
    return success_response(request=request, message="获取成功", data=list_categories())


@router.post("")
def create_application_api(
    request: Request,
    payload: ApplicationCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = create_application(db, user, payload)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="创建成功", data=data)


@router.get("/my")
def list_my_applications_api(
    request: Request,
    status: str | None = Query(default=None),
    award_type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=100),
    keyword: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = list_my_applications(
            db,
            user,
            status=status,
            award_type=award_type,
            category=category,
            keyword=keyword,
            page=page,
            size=size,
        )
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="获取成功", data=data)


@router.get("/my/category-summary")
def category_summary_api(
    request: Request,
    term: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = get_my_category_summary(db, user, term=term)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="获取成功", data=data)


@router.get("/my/by-category")
def by_category_api(
    request: Request,
    category: str = Query(...),
    sub_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    term: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = get_my_by_category(
            db,
            user,
            category=category,
            sub_type=sub_type,
            status=status,
            term=term,
            page=page,
            size=size,
        )
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="获取成功", data=data)


@router.post("/auto-fill/jobs")
def create_auto_fill_job_api(
    request: Request,
    payload: AutoFillJobCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = create_auto_fill_job(db, user, payload)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="自动填报任务已创建", data=data)


@router.get("/auto-fill/jobs/{job_id}")
def get_auto_fill_job_api(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = get_auto_fill_job(db, user, job_id)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="获取成功", data=data)


@router.delete("/auto-fill/jobs/{job_id}")
def cancel_auto_fill_job_api(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        cancel_auto_fill_job(db, user, job_id)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="已取消", data={})


@router.post("/auto-fill/jobs/{job_id}/confirm")
def confirm_auto_fill_job_api(
    request: Request,
    job_id: str,
    payload: AutoFillConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = confirm_auto_fill_job(db, user, job_id, payload)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="自动填报已提交", data=data)


@router.get("/{application_id}")
def detail_api(
    request: Request,
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = get_application_detail(db, user, application_id)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="获取成功", data=data)


@router.put("/{application_id}")
def update_api(
    request: Request,
    application_id: int,
    payload: ApplicationUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = update_application(db, user, application_id, payload)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="更新成功", data=data)


@router.post("/{application_id}/withdraw")
def withdraw_api(
    request: Request,
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = withdraw_application(db, user, application_id)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="撤回成功", data=data)


@router.delete("/{application_id}")
def delete_api(
    request: Request,
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        soft_delete_application(db, user, application_id)
    except ServiceError as exc:
        return error_response(request=request, code=exc.code, message=exc.message)
    return success_response(request=request, message="删除成功", data={})
