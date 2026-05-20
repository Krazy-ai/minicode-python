"""模型适配器使用的 API 重试与指数退避机制。

负责处理瞬时失败（429/5xx）：自动重试、指数退避、尊重 Retry-After 头，
以及基于语义错误分类的自适应退避策略。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 最大重试次数
MAX_RETRIES = 3

# 基础退避时长（秒）
BASE_BACKOFF = 1.0

# 退避上限（60 秒）
MAX_BACKOFF = 60.0

# 抖动比例（0.5 表示在 ±50% 范围内随机化）
JITTER_FACTOR = 0.5

# 可重试的 HTTP 状态码
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# 错误的语义化分类
# ---------------------------------------------------------------------------

class ErrorCategory(str, Enum):
    """对 API 错误进行语义化分类。

    不同分类对应不同的重试策略：
    - RATE_LIMIT：服务器繁忙，需要更激进的退避
    - SERVER_ERROR：瞬时服务端问题，使用标准退避
    - NETWORK_ERROR：网络异常，可较快重试
    - AUTH_ERROR：凭证问题，不应重试（基本是永久错误）
    - INPUT_ERROR：请求参数错误，不应重试（重试也会失败）
    - OVERLOAD：模型/服务过载，使用更长退避
    - UNKNOWN：未分类，走默认策略
    """
    RATE_LIMIT = "rate_limit"       # 429
    SERVER_ERROR = "server_error"   # 500, 502, 503, 504
    NETWORK_ERROR = "network_error" # 连接被拒、超时、DNS 失败等
    AUTH_ERROR = "auth_error"       # 401, 403
    INPUT_ERROR = "input_error"     # 400, 422
    OVERLOAD = "overload"           # 529，Anthropic 特有
    UNKNOWN = "unknown"


# 各分类对应的退避倍率
_CATEGORY_BACKOFF: dict[ErrorCategory, float] = {
    ErrorCategory.RATE_LIMIT: 2.0,    # 限流场景下基础退避翻倍
    ErrorCategory.SERVER_ERROR: 1.0,  # 标准指数退避
    ErrorCategory.NETWORK_ERROR: 0.5, # 网络抖动场景快速重试
    ErrorCategory.OVERLOAD: 3.0,      # 过载场景下退避更激进
    ErrorCategory.UNKNOWN: 1.0,       # 默认值
}

# 各分类的最大重试次数覆盖
_CATEGORY_MAX_RETRIES: dict[ErrorCategory, int | None] = {
    ErrorCategory.NETWORK_ERROR: 5,   # 瞬时网络问题多重试几次
    ErrorCategory.OVERLOAD: 5,        # 过载也多重试几次
    ErrorCategory.RATE_LIMIT: 4,      # 限流多重试几次
}

# 错误信息中表示「过载」的关键词
_OVERLOAD_PATTERNS = re.compile(
    r"(?:overloaded|overload|capacity|too many requests|"
    r"temporarily unavailable|please try again later|"
    r"service is currently unavailable|api is temporarily|"
    r"capacity exceeded|high demand)",
    re.IGNORECASE,
)

# 错误信息中表示「网络层错误」的关键词
_NETWORK_ERROR_PATTERNS = re.compile(
    r"(?:connection\s*(?:refused|reset|timeout|aborted)|"
    r"timed?\s*out|dns\s*resolution|name\s*resolution|"
    r"network\s*(?:error|unreachable|down)|"
    r"socket\s*(?:error|closed)|eof\s*occurred|"
    r"ssl\s*error|certificate\s*verify|handshake\s*failed)",
    re.IGNORECASE,
)


def classify_error(error: Exception) -> ErrorCategory:
    """将错误归类到语义化的 ErrorCategory，便于自适应重试。

    优先利用 HTTP 状态码，再结合错误信息中的关键词进行综合判断，
    从而做出更智能的重试决策。
    """
    # 优先看 HTTP 状态码（最可靠）
    status_code = getattr(error, "status_code", None)
    
    if status_code is not None:
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT
        if status_code == 529:
            return ErrorCategory.OVERLOAD
        if status_code in (401, 403):
            return ErrorCategory.AUTH_ERROR
        if status_code in (400, 422, 404, 405, 409, 413, 415):
            return ErrorCategory.INPUT_ERROR
        if status_code in (500, 502, 503, 504):
            # 检查错误信息中是否暗示过载
            msg = str(error).lower()
            if _OVERLOAD_PATTERNS.search(msg):
                return ErrorCategory.OVERLOAD
            return ErrorCategory.SERVER_ERROR
    
    # 非 HTTP 错误：通过错误信息关键词识别
    msg = str(error)
    if _NETWORK_ERROR_PATTERNS.search(msg):
        return ErrorCategory.NETWORK_ERROR
    if _OVERLOAD_PATTERNS.search(msg):
        return ErrorCategory.OVERLOAD
    
    # 兜底：根据异常类型名识别
    error_type_name = type(error).__name__.lower()
    if any(name in error_type_name for name in ("timeout", "connection", "socket")):
        return ErrorCategory.NETWORK_ERROR
    
    return ErrorCategory.UNKNOWN


def is_retryable(category: ErrorCategory) -> bool:
    """判断指定错误分类是否可重试。"""
    return category in (
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.SERVER_ERROR,
        ErrorCategory.NETWORK_ERROR,
        ErrorCategory.OVERLOAD,
        ErrorCategory.UNKNOWN,
    )


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class APIRetryExhaustedError(Exception):
    """所有重试都已耗尽时抛出。"""
    
    def __init__(
        self,
        message: str,
        attempts: int,
        last_error: Exception | None = None,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error
        self.category = category


# ---------------------------------------------------------------------------
# 退避时长计算
# ---------------------------------------------------------------------------

def calculate_backoff(
    attempt: int,
    retry_after: float | None = None,
    base: float = BASE_BACKOFF,
    max_wait: float = MAX_BACKOFF,
    jitter: float = JITTER_FACTOR,
    category: ErrorCategory | None = None,
) -> float:
    """计算下一次重试前的退避时长（含指数退避和抖动）。

    支持基于错误分类的自适应退避：
    - RATE_LIMIT：2 倍基础退避，且尊重 Retry-After
    - OVERLOAD：3 倍基础退避，等待更久
    - NETWORK_ERROR：0.5 倍基础退避，快速重试
    - SERVER_ERROR：标准指数退避
    - 未知类型：标准指数退避

    参数：
        attempt: 当前重试次数（从 0 开始）
        retry_after: Retry-After 响应头中给出的秒数（可选）
        base: 基础退避时长
        max_wait: 退避上限
        jitter: 抖动比例
        category: 错误分类，用于自适应调节

    返回：
        下一次重试前需等待的秒数。
    """
    # 应用分类对应的倍率
    effective_base = base
    if category is not None:
        effective_base = base * _CATEGORY_BACKOFF.get(category, 1.0)
    
    if retry_after is not None and retry_after > 0:
        # 尊重 Retry-After，但保持分类对应的最低等待
        min_wait = effective_base * (2 ** min(attempt, 2))
        return max(min(retry_after, max_wait), min_wait)
    
    # 指数退避：effective_base * 2^attempt
    backoff = effective_base * (2 ** attempt)
    
    # 加入抖动：backoff * (1 ± jitter)
    jitter_range = backoff * jitter
    backoff = backoff + random.uniform(-jitter_range, jitter_range)
    
    # 保证为正数且不超过上限
    return max(0.1, min(backoff, max_wait))


# ---------------------------------------------------------------------------
# 重试装饰器
# ---------------------------------------------------------------------------

@dataclass
class RetryState:
    """重试过程的可观测状态。"""
    attempts: int = 0
    max_attempts: int = MAX_RETRIES
    total_wait_time: float = 0.0
    last_error: str | None = None
    last_category: ErrorCategory = ErrorCategory.UNKNOWN
    category_history: list[ErrorCategory] = field(default_factory=list)
    succeeded: bool = False


def retry_with_backoff(
    func: Callable,
    *args: Any,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    retryable_errors: set[int] = RETRYABLE_STATUS,
    on_retry: Callable[[RetryState], None] | None = None,
    **kwargs: Any,
) -> Any:
    """带自动重试和指数退避地执行函数。

    使用语义错误分类做自适应重试：
    - 限流（429）：激进退避，尊重 Retry-After
    - 服务端错误（5xx）：标准指数退避
    - 网络错误：快速重试 + 更多重试次数
    - 鉴权/参数错误：直接放弃（永久错误）
    - 过载：最长退避 + 最多重试次数

    参数：
        func: 待执行的函数
        *args: 透传给 func 的位置参数
        max_retries: 最大重试次数
        base_backoff: 基础退避时长（秒）
        max_backoff: 退避上限（秒）
        retryable_errors: 可重试的 HTTP 状态码集合
        on_retry: 每次重试时的可选回调
        **kwargs: 透传给 func 的关键字参数

    返回：
        func 成功执行后的结果。

    异常：
        APIRetryExhaustedError: 所有重试都耗尽。
    """
    state = RetryState(max_attempts=max_retries)
    
    for attempt in range(max_retries + 1):
        try:
            result = func(*args, **kwargs)
            state.succeeded = True
            state.attempts = attempt + 1
            return result
        
        except HTTPError as e:
            # 对错误进行语义化分类
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)
            
            # 不可重试的分类直接抛出
            if not is_retryable(category):
                raise
            
            # 取分类对应的最大重试次数
            cat_max = _CATEGORY_MAX_RETRIES.get(category)
            effective_max = cat_max if cat_max is not None else max_retries
            
            state.attempts = attempt + 1
            state.last_error = str(e)
            
            if attempt >= effective_max:
                raise APIRetryExhaustedError(
                    f"API call failed after {attempt + 1} attempts "
                    f"(category: {category.value}): {e}",
                    attempts=attempt + 1,
                    last_error=e,
                    category=category,
                )
            
            # 提取 Retry-After 头（如有）
            retry_after = getattr(e, "retry_after", None)
            
            # 基于错误分类计算自适应退避
            wait_time = calculate_backoff(
                attempt,
                retry_after=retry_after,
                base=base_backoff,
                max_wait=max_backoff,
                category=category,
            )
            
            state.total_wait_time += wait_time
            
            # 通知重试回调
            if on_retry:
                on_retry(state)
            
            # 等待下次重试
            time.sleep(wait_time)
        
        except Exception as e:
            # 非 HTTP 错误同样进行分类
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)
            
            if is_retryable(category) and attempt < max_retries:
                state.attempts = attempt + 1
                state.last_error = str(e)
                
                wait_time = calculate_backoff(
                    attempt,
                    base=base_backoff,
                    max_wait=max_backoff,
                    category=category,
                )
                state.total_wait_time += wait_time
                
                if on_retry:
                    on_retry(state)
                
                time.sleep(wait_time)
                continue
            
            # 不可重试的非 HTTP 错误，直接抛出
            raise


# ---------------------------------------------------------------------------
# HTTP 错误封装
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """带状态码和可选 Retry-After 的 HTTP 错误。"""
    
    def __init__(
        self,
        message: str,
        status_code: int,
        retry_after: float | None = None,
        response: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.response = response


def raise_for_status(response: Any, error_class: type[HTTPError] = HTTPError) -> None:
    """检查 HTTP 响应状态码，如有错误则抛出 HTTPError。

    这是一个通用封装，可适配不同 HTTP 客户端（urllib/requests/httpx 等）。
    """
    status_code = getattr(response, "status", None) or getattr(response, "status_code", None)
    
    if status_code is None:
        return
    
    # 提取 Retry-After 响应头
    retry_after = None
    if hasattr(response, "getheader"):
        retry_after_str = response.getheader("Retry-After")
    elif hasattr(response, "headers"):
        retry_after_str = response.headers.get("Retry-After")
    else:
        retry_after_str = None
    
    if retry_after_str:
        try:
            retry_after = float(retry_after_str)
        except (ValueError, TypeError):
            pass
    
    # 状态码 ≥ 400 视为错误
    if status_code >= 400:
        # 尝试从响应体里提取错误信息
        error_message = str(status_code)
        if hasattr(response, "read"):
            try:
                body = response.read().decode("utf-8", errors="replace")
                error_message = f"{status_code}: {body[:200]}"
            except Exception:
                pass
        elif hasattr(response, "text"):
            error_message = f"{status_code}: {response.text[:200]}"
        
        raise error_class(error_message, status_code, retry_after, response)


# ---------------------------------------------------------------------------
# 异步版本（预留）
# ---------------------------------------------------------------------------

async def retry_with_backoff_async(
    func: Callable,
    *args: Any,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    retryable_errors: set[int] = RETRYABLE_STATUS,
    on_retry: Callable[[RetryState], None] | None = None,
    **kwargs: Any,
) -> Any:
    """`retry_with_backoff` 的异步版本。

    使用 ``asyncio.sleep`` 替代 ``time.sleep`` 以避免阻塞事件循环，
    支持与同步版本完全一致的语义错误分类与自适应退避。
    """
    import asyncio
    
    state = RetryState(max_attempts=max_retries)
    
    for attempt in range(max_retries + 1):
        try:
            # 异步函数 await，同步函数直接调用
            if hasattr(func, "__await__"):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            state.succeeded = True
            state.attempts = attempt + 1
            return result
        
        except HTTPError as e:
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)
            
            if not is_retryable(category):
                raise
            
            cat_max = _CATEGORY_MAX_RETRIES.get(category)
            effective_max = cat_max if cat_max is not None else max_retries
            
            state.attempts = attempt + 1
            state.last_error = str(e)
            
            if attempt >= effective_max:
                raise APIRetryExhaustedError(
                    f"API call failed after {attempt + 1} attempts "
                    f"(category: {category.value}): {e}",
                    attempts=attempt + 1,
                    last_error=e,
                    category=category,
                )
            
            retry_after = getattr(e, "retry_after", None)
            wait_time = calculate_backoff(
                attempt,
                retry_after=retry_after,
                base=base_backoff,
                max_wait=max_backoff,
                category=category,
            )
            
            state.total_wait_time += wait_time
            
            if on_retry:
                on_retry(state)
            
            await asyncio.sleep(wait_time)
        
        except Exception as e:
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)
            
            if is_retryable(category) and attempt < max_retries:
                state.attempts = attempt + 1
                state.last_error = str(e)
                
                wait_time = calculate_backoff(
                    attempt,
                    base=base_backoff,
                    max_wait=max_backoff,
                    category=category,
                )
                state.total_wait_time += wait_time
                
                if on_retry:
                    on_retry(state)
                
                await asyncio.sleep(wait_time)
                continue
            
            raise


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def is_retryable_error(error: Exception, retryable_codes: set[int] = RETRYABLE_STATUS) -> bool:
    """利用语义分类判断一个错误是否可重试。"""
    if isinstance(error, HTTPError):
        category = classify_error(error)
        return is_retryable(category)
    # 非 HTTP 错误也走分类逻辑
    return is_retryable(classify_error(error))


def format_retry_state(state: RetryState) -> str:
    """将 RetryState 格式化为日志/展示用的字符串。"""
    if state.succeeded:
        return f"✓ Succeeded on attempt {state.attempts}"
    
    cat_summary = ""
    if state.category_history:
        from collections import Counter
        counts = Counter(c.value for c in state.category_history)
        cat_summary = f" ({', '.join(f'{k}×{v}' for k, v in counts.most_common(3))})"
    
    return (
        f"✗ Failed after {state.attempts} attempts{cat_summary}, "
        f"waited {state.total_wait_time:.1f}s total"
    )

