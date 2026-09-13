"""FastAPI 应用入口。

启动时会做的四件事（任何一件失败都视为配置错误，直接 fail-fast）：
1. 初始化结构化日志
2. 加载并校验全部 YAML 配置（评分权重之和必须为 1.0）
3. 生产环境安全自检
4. 打印当前降级模式清单（缺 Key 不是错误，但必须**可见**）
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.v1.router import api_router
from app.core.config import Settings, get_limits_config, get_pricing_config, get_scoring_config, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, get_request_id, new_request_id, set_request_id, setup_logging
from app.db.session import dispose_engines
from app.schemas.common import ApiMeta, ErrorBody, error_envelope

log = get_logger("main")

# 统一错误 envelope 的公共响应头。成功路径由 _request_context 中间件注入；
# 但未捕获异常会走到最外层的 ServerErrorMiddleware，**绕过所有自定义中间件**，
# 因此那里必须手动补一份，否则 5xx 响应会缺少安全头与 CORS 头。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def allowed_origins(settings: Settings) -> list[str]:
    """CORS 白名单。

    抽成函数是为了让 CORSMiddleware 与最外层异常处理器共用同一份白名单 ——
    否则两处会各自演化，出现"正常请求能跨域、出错时被浏览器拦掉"的怪现象。
    """
    return [settings.frontend_url, "http://localhost:3000", "http://127.0.0.1:3000"]



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging("DEBUG" if not settings.is_production else "INFO")

    # ── 配置 fail-fast：这里的任何异常都会阻止应用启动（这是刻意的）──
    scoring = get_scoring_config()
    limits = get_limits_config()
    pricing = get_pricing_config()
    settings.assert_safe_for_production()

    log.info(
        "应用启动",
        extra={
            "event": "app.startup",
            "context": {
                "version": __version__,
                "env": settings.env,
                "scoring_version": scoring.version,
                "limits_version": limits.version,
                "pricing_calibrated": pricing.is_fully_calibrated,
            },
        },
    )
    for mode in settings.degraded_modes():
        log.warning("降级模式生效", extra={"event": "app.degraded_mode", "context": {"mode": mode}})
    if not pricing.is_fully_calibrated:
        log.warning(
            "存在未校准的单价，成本数字仅为量级参考（不是真实支出）",
            extra={"event": "app.pricing_uncalibrated", "code": "PRICING_UNCALIBRATED"},
        )

    yield

    await dispose_engines()
    log.info("应用关闭", extra={"event": "app.shutdown"})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TripDecider API",
        version=__version__,
        description="AI 旅行路线决策器 —— 广州 MVP",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """注入 request_id + 计时 + 安全响应头。"""
        rid = request.headers.get("X-Request-Id") or new_request_id()
        set_request_id(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常处理器在 call_next 内部已被调用；走到这里说明连处理器都失败了。
            # 此时必须原样抛出交给 ServerErrorMiddleware。
            #
            # ★ 刻意**不**清空 request_id ★
            # 早期实现在这里调了 set_request_id(None)，结果最外层的未捕获异常处理器
            # 读不到 request_id —— 响应体里的 meta.request_id 变成 null，
            # 响应头也没有 X-Request-Id。可错误提示偏偏写着
            # "请把响应头里的 X-Request-Id 反馈给我们"，等于让用户反馈一个不存在的东西。
            # 每个请求开始时都会重设 request_id，所以留着它不会串号。
            log.error(
                "请求处理抛出未捕获异常",
                exc_info=True,
                extra={
                    "event": "http.unhandled",
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        set_request_id(None)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Elapsed-Ms"] = str(elapsed_ms)
        # 安全响应头（PRD §24）
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if settings.is_production:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https:; connect-src 'self'",
            )
        if elapsed_ms > 3000:
            log.warning(
                "慢请求",
                extra={
                    "event": "http.slow",
                    "latency_ms": elapsed_ms,
                    "context": {"path": request.url.path[:120]},
                },
            )
        return response

    # 中间件注册顺序：add_middleware 会 insert(0)，而 Starlette 从后往前包裹，
    # 所以**后加的在外层**。这里先注册自定义中间件、后注册 CORS，
    # 于是 CORS 在最外层 —— 这是刻意的：错误响应也必须带上 CORS 头，
    # 否则前端在浏览器里读到的是"跨域被拦"，而不是我们精心写的错误提示。
    # （例外：未捕获异常走最外层的 ServerErrorMiddleware，会绕过 CORS，
    #   那份响应头由下面的 _unhandled 处理器手动补齐。）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(settings),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-Id"],
        max_age=600,
    )

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "业务异常",
            extra={"event": "error.app", "code": str(exc.code), "context": exc.context},
        )
        body = error_envelope(
            ErrorBody(
                code=str(exc.code),
                message=exc.message,
                hint=exc.hint,
                # 统一带上请求路径：与下面的框架级 404/405 处理器的 context 对齐 ——
                # "哪个路径出错"不该取决于错误是业务层抛的还是框架层抛的。
                context={**exc.context, "path": request.url.path[:120]},
            ),
            ApiMeta(request_id=get_request_id()),
        )
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """把 404/405 等框架级错误也纳入统一 envelope，避免返回纯文本绕过错误规范。"""
        # 按路径语义给出具体错误码，而不是一律 TRIP_NOT_FOUND ——
        # 未知路径报"行程不存在"会误导排查方向（前端也会据此显示错误的提示文案）。
        path = request.url.path
        if exc.status_code == 404:
            if path.startswith("/api/v1/public"):
                code = ErrorCode.SHARE_NOT_FOUND
            elif "/trips/" in path or "/trip/" in path:
                code = ErrorCode.TRIP_NOT_FOUND
            elif "/places/" in path:
                code = ErrorCode.PLACE_NOT_FOUND
            else:
                code = ErrorCode.NOT_FOUND
        else:
            code = {403: ErrorCode.FORBIDDEN, 429: ErrorCode.RATE_LIMITED}.get(
                exc.status_code, ErrorCode.INVALID_INPUT
            )
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL
        body = error_envelope(
            ErrorBody(
                code=str(code),
                message=str(exc.detail) if isinstance(exc.detail, str) else "请求无法完成",
                hint="请检查请求路径与参数。",
                context={"status": exc.status_code, "path": request.url.path[:120]},
            ),
            ApiMeta(request_id=get_request_id()),
        )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只暴露字段级摘要，不回显完整输入体（可能含敏感内容）
        fields = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "problem": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()[:10]
        ]
        body = error_envelope(
            ErrorBody(
                code=str(ErrorCode.INVALID_INPUT),
                message="提交的内容不符合要求",
                hint="请检查表单内容后重试。",
                context={"fields": fields},
            ),
            ApiMeta(request_id=get_request_id()),
        )
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理器。它由最外层的 ServerErrorMiddleware 调用，
        因此**绕过了 _request_context 与 CORSMiddleware** ——
        安全头、X-Request-Id、CORS 头都必须在这里手动补上，
        否则 500 响应在前端看来就是一次"莫名其妙的网络错误"。
        """
        # 绝不向用户暴露堆栈（PRD §6 FR-13.2）
        log.error("未处理异常", exc_info=exc, extra={"event": "error.unhandled", "code": "INTERNAL"})
        rid = get_request_id()
        body = error_envelope(
            ErrorBody(
                code=str(ErrorCode.INTERNAL),
                message="服务出了点问题，请重试。",
                hint="若持续出现，请把响应头里的 X-Request-Id 反馈给我们。",
                context={},
            ),
            ApiMeta(request_id=rid),
        )
        headers = dict(_SECURITY_HEADERS)
        if rid:
            # 提示里让用户反馈这个头，就必须真的带上它
            headers["X-Request-Id"] = rid
        origin = request.headers.get("origin")
        if origin and origin in allowed_origins(settings):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Vary"] = "Origin"
        return JSONResponse(status_code=500, content=body, headers=headers)

    app.include_router(api_router)

    # ★ 开发设置面板只在开发环境注册 ★
    # 这里用 import 而不是顶部 import：生产进程连这个模块都不会加载，
    # 也就不会在内存里留一把"能写 .env"的能力。
    if settings.env == "development":
        from app.api.v1 import dev

        app.include_router(dev.router, prefix="/api/v1")
        log.info(
            "开发设置面板已启用",
            extra={
                "event": "app.dev_panel_enabled",
                "context": {
                    "path": "/api/v1/dev/config",
                    "auth": "ADMIN_TOKEN" if settings.admin_token else "localhost-only",
                },
            },
        )

    return app


app = create_app()
