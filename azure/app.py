# coding: utf-8
import os
import sys
import json

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import time
import secrets
import threading
import logging
import hashlib
import hmac
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file, abort
import sqlite3
import fcntl
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import click
from hmac import compare_digest

from models import (db, User, LoginAttempt, Account, Subscription, SkuCache,
                    VmSkuPriceCache, VmResizeOptionsCache, LocationCache,
                    ImageCache, ScriptTemplate, DeploymentTask, VmCache,
                    CostCache, SystemSetting)
from azure_service import AzureService, PRESET_IMAGES
from sku_catalog import (
    is_retail_price_compatible,
    monthly_estimate,
    select_retail_price,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("azure_app")

MAX_VM_CREATE_COUNT = 8
MIN_DISK_SIZE_GB = 1
MAX_DISK_SIZE_GB = 4096
MAX_CUSTOM_DATA_LENGTH = 65535
MIN_FIREWALL_PRIORITY = 100
MAX_FIREWALL_PRIORITY = 4096
MAX_FIREWALL_PORT_RANGE_LENGTH = 128
VM_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ADMIN_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FIREWALL_RULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ALLOWED_FIREWALL_PROTOCOLS = {"Tcp", "Udp", "*"}
ALLOWED_FIREWALL_ACCESS = {"Allow", "Deny"}
ALLOWED_FIREWALL_DIRECTIONS = {"Inbound", "Outbound"}
ALLOWED_SERVICE_TAGS = {"*", "Internet", "VirtualNetwork", "AzureLoadBalancer"}
RETAIL_PRICE_CACHE_SOURCE = "azure_retail_prices_v2"


def public_error_message(operation="Azure 操作"):
    """给客户端返回固定的安全错误文本，详细异常只进入服务端日志。"""
    return f"{operation}失败，请查看活动日志或稍后重试。"


SKU_LOCATION_UNAVAILABLE_CODES = {
    "LocationNotAvailableForResourceType",
    "SubscriptionIsNotRegisteredForResourceType",
    "SubscriptionNotRegisteredForResourceType",
    "MissingSubscriptionRegistration",
    "ResourceTypeNotSupported",
}


def _azure_error_code(error):
    """兼容 Azure SDK 不同版本的错误码字段。"""
    candidates = [
        getattr(error, "error_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "error", None), "code", None),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def _is_sku_location_unavailable_error(error):
    """只把 Azure 明确表示地域/订阅不可用的错误转换为业务空状态。"""
    code = _azure_error_code(error)
    if code in SKU_LOCATION_UNAVAILABLE_CODES:
        return True
    status_code = getattr(error, "status_code", None)
    message = str(error or "").lower()
    return status_code in {400, 409} and (
        "location" in message
        or "region" in message
        or "subscription" in message
        or "resource type" in message
    )


def sku_location_unavailable_message(location):
    return "当前订阅未在该地域开放虚拟机服务，无法加载规格"


def parse_bounded_int(raw_value, field_name, minimum, maximum, default=None):
    value = default if raw_value in (None, "") and default is not None else raw_value
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name}必须在 {minimum} 至 {maximum} 之间")
    return parsed


def validate_vm_request(vm_prefix, location, vm_size, image_urn, custom_image_urn,
                        admin_username, admin_password, ssh_public_key,
                        disk_size_gb, count, custom_data,
                        auth_type="password", admin_password_confirm=None):
    """服务端校验创建/重装共用的资源参数，不能只依赖浏览器表单约束。"""
    if not vm_prefix or not VM_NAME_PATTERN.fullmatch(vm_prefix):
        raise ValueError("虚拟机名称只能包含小写字母、数字和连字符，且必须以字母或数字开头")
    if not location or len(location) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{0,63}", location):
        raise ValueError("地域参数无效")
    if not vm_size or len(vm_size) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", vm_size):
        raise ValueError("虚拟机规格参数无效")
    selected_image = custom_image_urn or image_urn
    if not selected_image or len(selected_image) > 1024:
        raise ValueError("镜像 URN 参数无效")
    if custom_image_urn and not re.fullmatch(r"[^:\s]{1,128}:[^:\s]{1,128}:[^:\s]{1,128}:[^:\s]{1,128}", custom_image_urn):
        raise ValueError("自定义镜像 URN 必须是 Publisher:Offer:Sku:Version 格式")
    if not admin_username or len(admin_username) > 64 or not ADMIN_USERNAME_PATTERN.fullmatch(admin_username):
        raise ValueError("管理员用户名格式无效")
    if auth_type not in {"password", "ssh_key"}:
        raise ValueError("认证方式无效")
    if len(admin_password) > 256:
        raise ValueError("管理员密码长度超出限制")
    if len(ssh_public_key) > 16384:
        raise ValueError("SSH 公钥长度超出限制")
    if auth_type == "password":
        if not admin_password:
            raise ValueError("密码认证必须填写登录密码")
        if admin_password_confirm is not None and admin_password != admin_password_confirm:
            raise ValueError("两次输入的登录密码不一致")
        if not validate_password(admin_password):
            raise ValueError(password_policy_message())
    elif not ssh_public_key:
        raise ValueError("SSH 公钥认证必须填写公钥")
    if len(custom_data) > MAX_CUSTOM_DATA_LENGTH:
        raise ValueError("初始化脚本内容超过 Azure 支持的长度限制")
    disk_size_gb = parse_bounded_int(disk_size_gb, "系统盘大小", MIN_DISK_SIZE_GB, MAX_DISK_SIZE_GB)
    count = parse_bounded_int(count, "创建数量", 1, MAX_VM_CREATE_COUNT)
    return disk_size_gb, count


def validate_reinstall_request(vm_name, image_urn, custom_image_urn, admin_username,
                               admin_password, ssh_public_key, disk_size_gb, custom_data):
    if not vm_name or not VM_NAME_PATTERN.fullmatch(vm_name.lower()):
        raise ValueError("虚拟机名称无效")
    selected_image = custom_image_urn or image_urn
    if not selected_image or len(selected_image) > 1024:
        raise ValueError("镜像 URN 参数无效")
    if not admin_username or len(admin_username) > 64 or not ADMIN_USERNAME_PATTERN.fullmatch(admin_username):
        raise ValueError("管理员用户名格式无效")
    if len(admin_password) > 256 or len(ssh_public_key) > 16384:
        raise ValueError("管理员凭据长度超出限制")
    if not admin_password and not ssh_public_key:
        raise ValueError("必须提供管理员密码或 SSH 公钥之一")
    if len(custom_data) > MAX_CUSTOM_DATA_LENGTH:
        raise ValueError("初始化脚本内容超过 Azure 支持的长度限制")
    return parse_bounded_int(disk_size_gb, "系统盘大小", MIN_DISK_SIZE_GB, MAX_DISK_SIZE_GB)


def validate_vm_size(vm_size):
    if not vm_size or len(vm_size) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", vm_size):
        raise ValueError("虚拟机规格参数无效")
    return vm_size


def _valid_port_range(port_range):
    if not port_range or len(port_range) > MAX_FIREWALL_PORT_RANGE_LENGTH:
        return False
    if port_range == "*":
        return True
    for item in port_range.split(","):
        item = item.strip()
        if not re.fullmatch(r"\d+(?:-\d+)?", item):
            return False
        parts = [int(part) for part in item.split("-")]
        if any(port < 0 or port > 65535 for port in parts):
            return False
        if len(parts) == 2 and parts[0] > parts[1]:
            return False
    return True


def _valid_source_cidr(source_cidr):
    if not source_cidr or len(source_cidr) > 512:
        return False
    for item in source_cidr.split(","):
        item = item.strip()
        if item in ALLOWED_SERVICE_TAGS:
            continue
        try:
            if "/" in item:
                ipaddress.ip_network(item, strict=False)
            else:
                ipaddress.ip_address(item)
        except ValueError:
            return False
    return True


def validate_firewall_rule(rule_name, priority, protocol, port_range, source_cidr, access, direction):
    if not rule_name or not FIREWALL_RULE_NAME_PATTERN.fullmatch(rule_name):
        raise ValueError("防火墙规则名称格式无效")
    priority = parse_bounded_int(priority, "规则优先级", MIN_FIREWALL_PRIORITY, MAX_FIREWALL_PRIORITY)
    if protocol not in ALLOWED_FIREWALL_PROTOCOLS:
        raise ValueError("防火墙协议参数无效")
    if not _valid_port_range(port_range):
        raise ValueError("目标端口范围无效")
    if not _valid_source_cidr(source_cidr):
        raise ValueError("源地址或 CIDR 无效")
    if access not in ALLOWED_FIREWALL_ACCESS or direction not in ALLOWED_FIREWALL_DIRECTIONS:
        raise ValueError("防火墙访问控制参数无效")
    return priority


app = Flask(__name__)


def resolve_trust_proxy_hops(raw_value=None):
    """Return a bounded trusted proxy hop count; zero means do not trust forwarded headers."""
    if raw_value is None:
        raw_value = os.getenv("TRUST_PROXY_HOPS", "0")
    try:
        hops = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return 0
    return hops if 1 <= hops <= 3 else 0


def configure_trusted_proxy(flask_app, raw_value=None):
    common_hops = resolve_trust_proxy_hops(raw_value)
    for_hops = resolve_trust_proxy_hops(os.getenv("TRUST_PROXY_FOR_HOPS", str(common_hops)))
    proto_hops = resolve_trust_proxy_hops(os.getenv("TRUST_PROXY_PROTO_HOPS", str(common_hops)))
    if for_hops or proto_hops:
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=for_hops, x_proto=proto_hops)
    return flask_app


configure_trusted_proxy(app)

_image_cache_refreshing = set()
_image_cache_refreshing_lock = threading.Lock()

def _refresh_dynamic_images_cache(subscription_id, tenant_id, client_id, client_secret, subscription_azure_id, loc):
    """后台刷新镜像缓存，不阻塞当前页面请求。"""
    with app.app_context():
        try:
            data = AzureService.fetch_dynamic_images(
                tenant_id, client_id, client_secret,
                subscription_azure_id, loc, force_refresh=True
            )
            ImageCache.query.filter_by(subscription_id=subscription_id, location=loc).delete(
                synchronize_session=False
            )
            for architecture in ("x64", "arm64"):
                for image in data.get(architecture, []):
                    db.session.add(ImageCache(
                        subscription_id=subscription_id,
                        location=loc,
                        architecture=architecture,
                        image_key=image["key"],
                        name=image["name"],
                        os_type=image["os_type"],
                        urn=image.get("urn", "")
                    ))
            db.session.commit()
            logger.info("后台镜像缓存刷新完成：subscription=%s location=%s", subscription_id, loc)
        except Exception:
            db.session.rollback()
            logger.exception("后台镜像缓存刷新失败：subscription=%s location=%s", subscription_id, loc)
        finally:
            db.session.remove()
            with _image_cache_refreshing_lock:
                _image_cache_refreshing.discard((subscription_id, loc))

def _schedule_image_cache_refresh(subscription_id, tenant_id, client_id, client_secret, subscription_azure_id, loc):
    key = (subscription_id, loc)
    with _image_cache_refreshing_lock:
        if key in _image_cache_refreshing:
            return False
        _image_cache_refreshing.add(key)
    try:
        submit_background_task(
            _refresh_dynamic_images_cache,
            subscription_id, tenant_id, client_id, client_secret, subscription_azure_id, loc,
        )
    except Exception:
        with _image_cache_refreshing_lock:
            _image_cache_refreshing.discard(key)
        raise
    return True

# 配置数据库与密钥 (持久化至 /app/data 挂载卷)
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data_dir = os.path.join(base_dir, "data")
os.makedirs(data_dir, exist_ok=True)
db_path = os.getenv("DATABASE_PATH") or os.path.join(data_dir, "database.db")
database_maintenance_lock = threading.RLock()
database_maintenance_lock_path = f"{db_path}.maintenance.lock"


@contextmanager
def database_maintenance():
    """跨线程/跨 Worker 串行化数据库快照和恢复操作。"""
    with database_maintenance_lock:
        with open(database_maintenance_lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def create_consistent_database_snapshot():
    snapshot = tempfile.NamedTemporaryFile(prefix=".backup-", suffix=".db", dir=data_dir, delete=False)
    snapshot_path = snapshot.name
    snapshot.close()
    try:
        with database_maintenance():
            source = sqlite3.connect(db_path, timeout=30)
            target = sqlite3.connect(snapshot_path, timeout=30)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
        return snapshot_path
    except Exception:
        if os.path.exists(snapshot_path):
            os.remove(snapshot_path)
        raise


app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "connect_args": {
        "timeout": 30,
        "check_same_thread": False
    }
}
secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.config["SECRET_KEY"] = secret_key
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

session_cookie_secure = os.getenv("SESSION_COOKIE_SECURE", "1") == "1"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=session_cookie_secure,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=session_cookie_secure,
)

@app.after_request
def add_security_headers(response):
    """为所有响应添加安全头，并仅在显式启用的 HTTPS 请求上发送 HSTS。"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; "
        "font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    if os.getenv("ENABLE_HSTS") == "1" and request.is_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


AZURE_TASK_WORKERS_DEFAULT = 2
AZURE_TASK_WORKERS_MIN = 1
AZURE_TASK_WORKERS_MAX = 8


def resolve_azure_task_workers(raw_value=None) -> int:
    """读取并约束单个进程可同时执行的 Azure 后台任务数。"""
    if raw_value is None:
        raw_value = os.getenv("AZURE_TASK_WORKERS", str(AZURE_TASK_WORKERS_DEFAULT))
    try:
        workers = int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "AZURE_TASK_WORKERS=%r 无效，已使用默认值 %s",
            raw_value,
            AZURE_TASK_WORKERS_DEFAULT,
        )
        return AZURE_TASK_WORKERS_DEFAULT
    if workers < AZURE_TASK_WORKERS_MIN or workers > AZURE_TASK_WORKERS_MAX:
        clamped = min(max(workers, AZURE_TASK_WORKERS_MIN), AZURE_TASK_WORKERS_MAX)
        logger.warning(
            "AZURE_TASK_WORKERS=%s 超出 %s..%s，已限制为 %s",
            workers,
            AZURE_TASK_WORKERS_MIN,
            AZURE_TASK_WORKERS_MAX,
            clamped,
        )
        return clamped
    return workers


AZURE_TASK_WORKERS = resolve_azure_task_workers()
app.config["AZURE_TASK_WORKERS"] = AZURE_TASK_WORKERS
background_task_executor = ThreadPoolExecutor(
    max_workers=AZURE_TASK_WORKERS,
    thread_name_prefix="azure-task",
)


def _mark_background_task_failed(task_id, error: Exception) -> None:
    """只为未完成的持久化任务写入执行器兜底失败状态。"""
    if task_id is None:
        return
    try:
        task = db.session.get(DeploymentTask, task_id)
        if task and task.status in ("Pending", "InProgress"):
            task.status = "Failed"
            task.progress_msg = "后台任务执行失败"
            task.error_detail = public_error_message()
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("后台任务失败状态写入失败：task_id=%s", task_id)


def _mark_background_task_submission_failed(task_id, error: Exception) -> None:
    """记录执行器拒绝提交时的明确失败状态。"""
    if task_id is None:
        return
    try:
        task = db.session.get(DeploymentTask, task_id)
        if task and task.status in ("Pending", "InProgress"):
            task.status = "Failed"
            task.progress_msg = "后台任务提交失败"
            task.error_detail = public_error_message("后台任务提交")
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("后台任务提交失败状态写入失败：task_id=%s", task_id)


def _mark_background_task_started(task_id) -> bool:
    """用条件更新把 Pending 原子转换为 InProgress，避免 watchdog 抢占任务。"""
    if task_id is None:
        return True
    result = db.session.execute(
        update(DeploymentTask)
        .where(
            DeploymentTask.id == task_id,
            DeploymentTask.status == "Pending",
        )
        .values(
            status="InProgress",
            updated_at=datetime.utcnow(),
        )
    )
    if result.rowcount != 1:
        db.session.rollback()
        return False
    db.session.commit()
    return True


def submit_background_task(fn, *args, task_id=None, **kwargs) -> Future:
    """在有界进程级执行器中运行后台操作，并统一处理未捕获异常。"""
    def run_task():
        with app.app_context():
            try:
                if not _mark_background_task_started(task_id):
                    logger.warning("跳过已结束或不存在的后台任务：task_id=%s", task_id)
                    return None
                return fn(*args, **kwargs)
            except Exception as exc:
                db.session.rollback()
                logger.exception("后台任务执行异常：task_id=%s", task_id)
                _mark_background_task_failed(task_id, exc)
                raise
            finally:
                db.session.remove()

    try:
        return background_task_executor.submit(run_task)
    except Exception as submission_error:
        db.session.rollback()
        _mark_background_task_submission_failed(task_id, submission_error)
        logger.exception("后台任务提交异常：task_id=%s", task_id)
        raise

db.init_app(app)

from sqlalchemy import event, inspect, text, select, insert, update, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()
    except Exception:
        pass


def password_policy_message() -> str:
    return "密码至少 12 位，并至少包含大写字母、小写字母、数字和非空白特殊字符中的三类。"


def validate_password(password: str) -> bool:
    if len(password) < 12:
        return False
    character_classes = (
        any(char.islower() for char in password),
        any(char.isupper() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() and not char.isspace() for char in password),
    )
    return sum(character_classes) >= 3


LOGIN_RATE_LIMIT_WINDOW = timedelta(minutes=15)
LOGIN_RATE_LIMIT_MAX_FAILURES = 20
LOGIN_RATE_LIMIT_BLOCK_DURATION = timedelta(minutes=15)


def _login_rate_limit_digests(ip: str, username: str) -> tuple[str, str]:
    """生成不可逆的来源和用户名摘要，永不将原始值写入数据库。"""
    secret = app.config["SECRET_KEY"].encode("utf-8")

    def digest(namespace: str, value: str) -> str:
        payload = f"azure-manager/login-rate-limit/v1/{namespace}:".encode("utf-8")
        payload += (value or "").strip().encode("utf-8")
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()

    return digest("source", ip), digest("username", username.casefold())


def is_login_rate_limited(ip: str, username: str) -> bool:
    """检查同一来源是否被封禁；数据库异常时拒绝登录。"""
    try:
        source_digest, _ = _login_rate_limit_digests(ip, username)
        attempt = LoginAttempt.query.filter_by(source_digest=source_digest).first()
        if not attempt:
            return False
        now = datetime.utcnow()
        if attempt.blocked_until and attempt.blocked_until > now:
            return True
        return (
            attempt.failure_count >= LOGIN_RATE_LIMIT_MAX_FAILURES
            and attempt.window_started_at > now - LOGIN_RATE_LIMIT_WINDOW
        )
    except Exception:
        db.session.rollback()
        logger.exception("登录限流记录读取失败，已拒绝本次登录")
        return True


def record_login_failure(ip: str, username: str) -> bool:
    """在 SQLite 写事务中原子记录失败，数据库异常时返回 False。"""
    connection = None
    try:
        source_digest, username_digest = _login_rate_limit_digests(ip, username)
        now = datetime.utcnow()
        table = LoginAttempt.__table__
        connection = db.engine.connect()
        # BEGIN IMMEDIATE 串行化读取、递增和提交，覆盖多 Worker/多连接场景。
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        attempt = connection.execute(
            select(table).where(table.c.source_digest == source_digest)
        ).mappings().first()

        if not attempt:
            connection.execute(
                insert(table).values(
                    source_digest=source_digest,
                    username_digest=username_digest,
                    failure_count=1,
                    window_started_at=now,
                    blocked_until=(
                        now + LOGIN_RATE_LIMIT_BLOCK_DURATION
                        if LOGIN_RATE_LIMIT_MAX_FAILURES <= 1
                        else None
                    ),
                    updated_at=now,
                )
            )
        elif attempt["blocked_until"] and attempt["blocked_until"] > now:
            connection.commit()
            return True
        else:
            window_expired = attempt["window_started_at"] <= now - LOGIN_RATE_LIMIT_WINDOW
            failure_count = 1 if window_expired else attempt["failure_count"] + 1
            blocked_until = (
                now + LOGIN_RATE_LIMIT_BLOCK_DURATION
                if failure_count >= LOGIN_RATE_LIMIT_MAX_FAILURES
                else None
            )
            values = {
                "username_digest": username_digest,
                "failure_count": failure_count,
                "blocked_until": blocked_until,
                "updated_at": now,
            }
            if window_expired:
                values["window_started_at"] = now
            connection.execute(
                update(table)
                .where(table.c.source_digest == source_digest)
                .values(**values)
            )

        connection.commit()
        return True
    except Exception:
        if connection is not None:
            connection.rollback()
        logger.exception("登录限流记录写入失败，已拒绝本次登录")
        return False
    finally:
        if connection is not None:
            connection.close()


def clear_login_failures(ip: str, username: str) -> bool:
    """成功认证后清除该来源的失败记录；数据库异常时拒绝登录。"""
    try:
        source_digest, _ = _login_rate_limit_digests(ip, username)
        LoginAttempt.query.filter_by(source_digest=source_digest).delete(synchronize_session=False)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        logger.exception("登录限流记录清理失败，已拒绝本次登录")
        return False


def ensure_security_schema(connection=None):
    """在可复用 SQLite 事务中无损迁移认证安全表，支持多 Worker 启动。"""
    owns_connection = connection is None
    if owns_connection:
        connection = db.engine.connect()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)")}
        if columns and "session_version" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"
            )
        LoginAttempt.__table__.create(bind=connection, checkfirst=True)
        if owns_connection:
            connection.commit()
    except OperationalError as exc:
        if owns_connection:
            connection.rollback()
        if "duplicate column name" not in str(exc).lower():
            raise
        if owns_connection:
            connection.commit()
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def ensure_deployment_task_schema(connection=None):
    """在可复用 SQLite 事务中无损迁移旧 deployment_tasks 表。"""
    owns_connection = connection is None
    if owns_connection:
        connection = db.engine.connect()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        inspector = inspect(connection)
        if "deployment_tasks" not in inspector.get_table_names():
            if owns_connection:
                connection.commit()
            return
        columns = {column["name"]: column for column in inspector.get_columns("deployment_tasks")}
        if "rule_name" not in columns:
            connection.execute(text("ALTER TABLE deployment_tasks ADD COLUMN rule_name VARCHAR(128)"))
            columns["rule_name"] = {"name": "rule_name"}
        subscription_column = columns.get("subscription_id")
        if not subscription_column or subscription_column.get("nullable", True):
            if owns_connection:
                connection.commit()
            return
        connection.execute(text("ALTER TABLE deployment_tasks RENAME TO deployment_tasks_legacy"))
        db.metadata.create_all(bind=connection)
        connection.execute(text("""
            INSERT INTO deployment_tasks
                (id, subscription_id, task_type, rule_name, target_name, resource_group, status, progress_msg, error_detail, created_at, updated_at)
            SELECT id, subscription_id, task_type, rule_name, target_name, resource_group, status, progress_msg, error_detail, created_at, updated_at
            FROM deployment_tasks_legacy
        """))
        connection.execute(text("DROP TABLE deployment_tasks_legacy"))
        if owns_connection:
            connection.commit()
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def ensure_sku_cache_schema(connection=None):
    """为规格缓存补齐订阅、架构和完整能力字段，兼容旧版 SQLite 数据库。"""
    owns_connection = connection is None
    if owns_connection:
        connection = db.engine.connect()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        inspector = inspect(connection)
        if "sku_cache" not in inspector.get_table_names():
            if owns_connection:
                connection.commit()
            return
        columns = {column["name"] for column in inspector.get_columns("sku_cache")}
        if "subscription_id" not in columns:
            connection.execute(text("ALTER TABLE sku_cache ADD COLUMN subscription_id INTEGER"))
        migration_columns = {
            "architecture": "VARCHAR(16)",
            "tier": "VARCHAR(32)",
            "vcpus_available": "INTEGER",
            "memory_gib": "FLOAT",
            "temp_disk_gib": "FLOAT",
            "os_disk_gib": "FLOAT",
            "max_data_disk_count": "INTEGER",
            "max_nics": "INTEGER",
            "gpu_count": "INTEGER",
            "rdma_enabled": "BOOLEAN",
            "accelerated_networking_supported": "BOOLEAN",
            "accelerated_networking_required": "BOOLEAN",
            "spot_capable": "BOOLEAN",
            "ephemeral_os_disk_supported": "BOOLEAN",
            "hyperv_generations_json": "TEXT",
            "disk_controller_types_json": "TEXT",
            "availability_zones_json": "TEXT",
            "capabilities_json": "TEXT",
            "restrictions_json": "TEXT",
            "restriction_reasons_json": "TEXT",
        }
        for column_name, column_type in migration_columns.items():
            if column_name not in columns:
                connection.execute(text(
                    f"ALTER TABLE sku_cache ADD COLUMN {column_name} {column_type}"
                ))
        if owns_connection:
            connection.commit()
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def initialize_database_schema():
    """用一个 SQLite 写事务串行完成建表和全部迁移，避免多 Worker 启动竞态。"""
    connection = db.engine.connect()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        db.metadata.create_all(bind=connection)
        ensure_deployment_task_schema(connection)
        ensure_security_schema(connection)
        ensure_sku_cache_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _json_value(value, default):
    if value in (None, ''):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _sku_cache_payload(item):
    """输出规格统一模型；规格行只使用 name/vcpus/memory/monthly_price。"""
    return {
        "name": item.name,
        "family": item.family or "",
        "tier": item.tier or "",
        "architecture": item.architecture,
        "vcpus": item.vcpus,
        "vcpus_available": item.vcpus_available,
        "memory_gib": item.memory_gib if item.memory_gib is not None else item.memory_gb,
        "memory_gb": item.memory_gb,
        "temp_disk_gib": item.temp_disk_gib,
        "os_disk_gib": item.os_disk_gib,
        "max_data_disk_count": item.max_data_disk_count,
        "max_nics": item.max_nics,
        "gpu_count": item.gpu_count,
        "rdma_enabled": item.rdma_enabled,
        "premium_io": item.premium_io,
        "accelerated_networking_supported": item.accelerated_networking_supported,
        "accelerated_networking_required": item.accelerated_networking_required,
        "accelerated_networking": item.accelerated_networking,
        "spot_capable": item.spot_capable,
        "ephemeral_os_disk_supported": item.ephemeral_os_disk_supported,
        "hyperv_generations": _json_value(item.hyperv_generations_json, []),
        "disk_controller_types": _json_value(item.disk_controller_types_json, []),
        "availability_zones": _json_value(item.availability_zones_json, []),
        "capabilities": _json_value(item.capabilities_json, {}),
        "restrictions_detail": _json_value(item.restrictions_json, []),
        "restriction_reasons": _json_value(item.restriction_reasons_json, []),
        "restrictions": item.restrictions or "",
        "restricted": bool(_json_value(item.restriction_reasons_json, [])),
        "selectable": not bool(_json_value(item.restriction_reasons_json, [])),
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _sku_cache_has_selectable_sku(items):
    """判断缓存中是否至少有一个当前订阅可选择的规格。"""
    return any(
        bool(item.architecture)
        and not bool(_json_value(item.restriction_reasons_json, []))
        for item in items
    )


def _sku_cache_unavailable_reason(items):
    """提取全量规格受限时最有用的 Azure 限制原因。"""
    for item in items:
        reasons = _json_value(item.restriction_reasons_json, [])
        if reasons:
            return reasons[0]
    return "NoSelectableVirtualMachineSku"


def _save_sku_cache_entry(subscription_id, location, item):
    reasons = item.get("restriction_reasons") or []
    entry = SkuCache(
        subscription_id=subscription_id,
        location=location,
        name=item["name"],
        architecture=item.get("architecture"),
        family=item.get("family", ""),
        tier=item.get("tier", ""),
        vcpus=item.get("vcpus"),
        vcpus_available=item.get("vcpus_available"),
        memory_gb=item.get("memory_gb", item.get("memory_gib")),
        memory_gib=item.get("memory_gib"),
        temp_disk_gib=item.get("temp_disk_gib"),
        os_disk_gib=item.get("os_disk_gib"),
        max_data_disk_count=item.get("max_data_disk_count"),
        max_nics=item.get("max_nics"),
        gpu_count=item.get("gpu_count", 0),
        rdma_enabled=item.get("rdma_enabled"),
        premium_io=item.get("premium_io", False),
        accelerated_networking_supported=item.get(
            "accelerated_networking_supported", item.get("accelerated_networking", False)
        ),
        accelerated_networking_required=item.get("accelerated_networking_required"),
        accelerated_networking=item.get(
            "accelerated_networking_supported", item.get("accelerated_networking", False)
        ),
        spot_capable=item.get("spot_capable", False),
        ephemeral_os_disk_supported=item.get("ephemeral_os_disk_supported", False),
        hyperv_generations_json=json.dumps(item.get("hyperv_generations", []), ensure_ascii=False),
        disk_controller_types_json=json.dumps(item.get("disk_controller_types", []), ensure_ascii=False),
        availability_zones_json=json.dumps(item.get("availability_zones", []), ensure_ascii=False),
        capabilities_json=json.dumps(item.get("capabilities", {}), ensure_ascii=False),
        restrictions_json=json.dumps(item.get("restrictions_detail", item.get("restrictions", [])), ensure_ascii=False),
        restriction_reasons_json=json.dumps(reasons, ensure_ascii=False),
    )
    db.session.add(entry)
    return entry


def _load_sku_catalog_for_resize(subscription, account, location):
    """为调整规格复用按订阅/地域持久化的完整规格能力缓存。"""
    loc = str(location or "").lower().replace(" ", "")
    cache_expiry = datetime.utcnow() - timedelta(hours=24)
    cached = SkuCache.query.filter_by(
        subscription_id=subscription.id,
        location=loc,
    ).all()
    fresh = [item for item in cached if item.updated_at and item.updated_at >= cache_expiry]
    if fresh:
        return {
            item.name.lower(): _sku_cache_payload(item)
            for item in fresh
            if item.name
        }

    try:
        fresh_skus = AzureService.fetch_and_cache_skus(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, loc
        )
        SkuCache.query.filter_by(
            subscription_id=subscription.id,
            location=loc,
        ).delete(synchronize_session=False)
        for item in fresh_skus:
            _save_sku_cache_entry(subscription.id, loc, item)
        db.session.commit()
        cached = SkuCache.query.filter_by(
            subscription_id=subscription.id,
            location=loc,
        ).all()
        return {
            item.name.lower(): _sku_cache_payload(item)
            for item in cached
            if item.name
        }
    except Exception:
        db.session.rollback()
        if cached:
            logger.warning("调整规格复用过期规格缓存：%s/%s", subscription.id, loc)
            return {
                item.name.lower(): _sku_cache_payload(item)
                for item in cached
                if item.name
            }
        raise


def _enrich_sku_prices(subscription, location, skus, os_type, billing_mode, currency):
    """按页面维度读取/刷新价格缓存；价格失败不阻断规格列表。"""
    os_type = "Windows" if str(os_type).lower() == "windows" else "Linux"
    billing_mode = "spot" if str(billing_mode).lower() == "spot" else "on_demand"
    currency = (str(currency or "USD").upper())[:16]
    ttl = timedelta(minutes=20 if billing_mode == "spot" else 360)
    cutoff = datetime.utcnow() - ttl
    names = [item.get("name") for item in skus if item.get("name")]
    rows = VmSkuPriceCache.query.filter(
        VmSkuPriceCache.subscription_id == subscription.id,
        VmSkuPriceCache.location == location,
        VmSkuPriceCache.os_type == os_type,
        VmSkuPriceCache.billing_mode == billing_mode,
        VmSkuPriceCache.currency == currency,
        VmSkuPriceCache.sku_name.in_(names or [""]),
    ).all()
    row_by_name = {row.sku_name.lower(): row for row in rows}

    def cached_price_is_usable(row):
        if not row or not row.updated_at or row.updated_at < cutoff:
            return False
        # 没有价格也是有效结果，短期内不应因同一批无报价 SKU 重复请求 Azure。
        if row.hourly_price is None:
            return (
                row.price_type == "unavailable"
                and row.source == RETAIL_PRICE_CACHE_SOURCE
            )
        if row.monthly_price is None:
            return False
        try:
            payload = json.loads(row.payload_json or "")
        except (TypeError, ValueError):
            return False
        return is_retail_price_compatible(payload, os_type, billing_mode)

    missing = [name for name in names if not cached_price_is_usable(row_by_name.get(name.lower()))]
    price_error = None
    if missing:
        try:
            records = AzureService.fetch_retail_prices(
                location,
                currency=currency,
                sku_names=missing,
            )
            for name in missing:
                selected = select_retail_price(records, name, os_type, billing_mode)
                hourly = float(selected["retailPrice"]) if selected else None
                now = datetime.utcnow()
                row = row_by_name.get(name.lower())
                if row is not None:
                    row.price_type = "hourly" if selected else "unavailable"
                    row.hourly_price = hourly
                    row.monthly_price = monthly_estimate(hourly)
                    row.effective_date = str((selected or {}).get("effectiveStartDate", ""))
                    row.payload_json = json.dumps(selected or {}, ensure_ascii=False)
                    row.source = RETAIL_PRICE_CACHE_SOURCE
                    row.updated_at = now
                    continue

                values = {
                    "subscription_id": subscription.id,
                    "location": location,
                    "sku_name": name,
                    "os_type": os_type,
                    "billing_mode": billing_mode,
                    "currency": currency,
                    "price_type": "hourly" if selected else "unavailable",
                    "hourly_price": hourly,
                    "monthly_price": monthly_estimate(hourly),
                    "effective_date": str((selected or {}).get("effectiveStartDate", "")),
                    "payload_json": json.dumps(selected or {}, ensure_ascii=False),
                    "source": RETAIL_PRICE_CACHE_SOURCE,
                    "updated_at": now,
                }
                statement = sqlite_insert(VmSkuPriceCache).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "subscription_id", "location", "sku_name", "os_type",
                        "billing_mode", "currency", "price_type",
                    ],
                    set_={
                        "hourly_price": statement.excluded.hourly_price,
                        "monthly_price": statement.excluded.monthly_price,
                        "effective_date": statement.excluded.effective_date,
                        "payload_json": statement.excluded.payload_json,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
                db.session.execute(statement)
            db.session.commit()
            rows = VmSkuPriceCache.query.filter(
                VmSkuPriceCache.subscription_id == subscription.id,
                VmSkuPriceCache.location == location,
                VmSkuPriceCache.os_type == os_type,
                VmSkuPriceCache.billing_mode == billing_mode,
                VmSkuPriceCache.currency == currency,
                VmSkuPriceCache.sku_name.in_(names or [""]),
            ).all()
            row_by_name = {row.sku_name.lower(): row for row in rows}
        except Exception as exc:
            db.session.rollback()
            price_error = "价格暂时无法获取"
            logger.warning("Azure 价格缓存刷新失败：%s", exc)

    enriched = []
    for item in skus:
        copy = dict(item)
        row = row_by_name.get(str(item.get("name") or "").lower())
        copy.update({
            "hourly_price": row.hourly_price if row else None,
            "monthly_price": row.monthly_price if row else None,
            "currency": currency,
            "billing_mode": billing_mode,
            "price_updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
        enriched.append(copy)
    return enriched, price_error

TASK_TIMEOUT_SECONDS = {"create": 3600, "reinstall": 3600, "resize": 3600, "change_ip": 1800, "sync_costs": 1200, "sync_subscriptions": 1200, "reset_credentials": 1800}
DEFAULT_TASK_TIMEOUT_SECONDS = 1800

# 同一 VM 的变更操作必须串行；不同 VM 仍可并行执行。
VM_OPERATION_TASK_TYPES = (
    "create", "start", "stop", "restart", "delete",
    "reinstall", "resize", "change_ip", "reset_credentials",
)


def reserve_vm_operation_task(subscription_id, resource_group, target_name, task_type, progress_msg):
    """原子地预留同一 VM 的操作任务，重复请求复用当前活跃任务。

    SQLite 的 BEGIN IMMEDIATE 将“检查活跃任务 + 创建任务”放进同一个短写事务，
    因此多 Worker 或多个浏览器标签页同时提交时，不会各自创建 Azure 操作。
    """
    if task_type not in VM_OPERATION_TASK_TYPES:
        raise ValueError(f"不支持的 VM 操作类型：{task_type}")

    type_params = {f"task_type_{idx}": value for idx, value in enumerate(VM_OPERATION_TASK_TYPES)}
    type_placeholders = ", ".join(f":task_type_{idx}" for idx in range(len(VM_OPERATION_TASK_TYPES)))
    query_params = {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "target_name": target_name,
        **type_params,
    }
    connection = db.engine.connect()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        active = connection.execute(
            text(f"""
                SELECT id, task_type, status, progress_msg
                FROM deployment_tasks
                WHERE subscription_id = :subscription_id
                  AND resource_group = :resource_group
                  AND target_name = :target_name
                  AND status IN ('Pending', 'InProgress')
                  AND task_type IN ({type_placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """),
            query_params,
        ).mappings().first()
        if active:
            connection.commit()
            return {
                "created": False,
                "task_id": active["id"],
                "task_type": active["task_type"],
                "status": active["status"],
                "progress_msg": active["progress_msg"] or "已有操作正在执行",
            }

        now = datetime.utcnow()
        insert_params = {
            "subscription_id": subscription_id,
            "task_type": task_type,
            "target_name": target_name,
            "resource_group": resource_group,
            "status": "Pending",
            "progress_msg": progress_msg,
            "created_at": now,
            "updated_at": now,
        }
        result = connection.execute(text("""
            INSERT INTO deployment_tasks
                (subscription_id, task_type, target_name, resource_group, status,
                 progress_msg, created_at, updated_at)
            VALUES
                (:subscription_id, :task_type, :target_name, :resource_group, :status,
                 :progress_msg, :created_at, :updated_at)
        """), insert_params)
        task_id = result.lastrowid
        connection.commit()
        return {
            "created": True,
            "task_id": task_id,
            "task_type": task_type,
            "status": "Pending",
            "progress_msg": progress_msg,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reserve_unique_task(subscription_id, resource_group, target_name, task_type, progress_msg, rule_name=None):
    """为订阅同步、账单和防火墙等非 VM 操作提供跨 Worker 去重。"""
    connection = db.engine.connect()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        active = connection.execute(text("""
            SELECT id, task_type, rule_name, status, progress_msg
            FROM deployment_tasks
            WHERE ((subscription_id = :subscription_id)
                   OR (subscription_id IS NULL AND :subscription_id IS NULL))
              AND resource_group = :resource_group
              AND target_name = :target_name
              AND task_type = :task_type
              AND status IN ('Pending', 'InProgress')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """), {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "target_name": target_name,
            "task_type": task_type,
        }).mappings().first()
        if active:
            connection.commit()
            return {"created": False, "task_id": active["id"], "task_type": active["task_type"],
                    "rule_name": active["rule_name"], "status": active["status"],
                    "progress_msg": active["progress_msg"] or "已有操作正在执行"}
        now = datetime.utcnow()
        result = connection.execute(text("""
            INSERT INTO deployment_tasks
                (subscription_id, task_type, rule_name, target_name, resource_group, status,
                 progress_msg, created_at, updated_at)
            VALUES (:subscription_id, :task_type, :rule_name, :target_name, :resource_group,
                    'Pending', :progress_msg, :created_at, :updated_at)
        """), {
            "subscription_id": subscription_id, "task_type": task_type,
            "rule_name": rule_name,
            "target_name": target_name, "resource_group": resource_group,
            "progress_msg": progress_msg, "created_at": now, "updated_at": now,
        })
        connection.commit()
        return {"created": True, "task_id": result.lastrowid, "task_type": task_type,
                "rule_name": rule_name, "status": "Pending", "progress_msg": progress_msg}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def expire_stale_tasks():
    """用带状态和时间条件的 UPDATE 标记超时任务，避免覆盖刚启动的任务。"""
    now = datetime.utcnow()
    expired_count = 0
    candidates = DeploymentTask.query.filter(
        DeploymentTask.status.in_(["Pending", "InProgress"])
    ).all()
    for task in candidates:
        timeout = TASK_TIMEOUT_SECONDS.get(
            (task.task_type or "").lower(), DEFAULT_TASK_TIMEOUT_SECONDS
        )
        cutoff = now - timedelta(seconds=timeout)
        result = db.session.execute(
            update(DeploymentTask)
            .where(
                DeploymentTask.id == task.id,
                DeploymentTask.status.in_(["Pending", "InProgress"]),
                or_(
                    DeploymentTask.updated_at <= cutoff,
                    DeploymentTask.updated_at.is_(None),
                ),
                DeploymentTask.created_at <= cutoff,
            )
            .values(
                status="Failed",
                progress_msg="任务执行超时",
                error_detail=f"超过 {timeout // 60} 分钟未收到后台进度更新，任务已停止等待。",
                updated_at=now,
            )
        )
        expired_count += result.rowcount or 0
    if expired_count:
        db.session.commit()
    else:
        db.session.rollback()
    return expired_count


def recover_interrupted_tasks():
    """回收进程重启遗留的活动任务，避免前端永久锁定且无法再次提交。

    后台 Azure poller 位于进程内，进程重启后无法安全恢复原线程；只处理至少
    90 秒没有心跳的任务，给正在优雅重载中的新旧 Worker 留出交接时间。
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=90)
    result = db.session.execute(
        update(DeploymentTask)
        .where(
            DeploymentTask.status.in_(["Pending", "InProgress"]),
            or_(
                DeploymentTask.updated_at <= cutoff,
                DeploymentTask.updated_at.is_(None),
            ),
        )
        .values(
            status="Failed",
            progress_msg="应用进程重启导致任务中断",
            error_detail="后台 Azure 操作线程已在应用重启时终止，最终云端状态未确认，请刷新资源状态后重试。",
            updated_at=now,
        )
    )
    if result.rowcount:
        db.session.commit()
        logger.warning("已回收 %s 个应用重启遗留任务", result.rowcount)
    else:
        db.session.rollback()
    return result.rowcount or 0

def task_watchdog_loop():
    while True:
        try:
            with app.app_context():
                expire_stale_tasks()
        except Exception:
            logger.exception("任务 watchdog 执行失败")
            db.session.remove()
        time.sleep(30)


login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录管理面板。"
login_manager.login_message_category = "warning"

CSRF_SESSION_KEY = "_csrf_token"

@app.before_request
def ensure_csrf_token():
    if not session.get(CSRF_SESSION_KEY):
        session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

@app.before_request
def validate_csrf_token():
    if request.method != "POST":
        return None
    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.form.get(CSRF_SESSION_KEY) or request.headers.get("X-CSRF-Token")
    if expected and supplied and compare_digest(supplied, expected):
        return None
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "error", "message": "CSRF token invalid"}), 400
    abort(400, description="CSRF token invalid")

@app.before_request
def invalidate_stale_auth_session():
    """撤销密码修改前或旧版本会话的认证状态。"""
    if not current_user.is_authenticated:
        return None
    if session.get("_auth_version") == current_user.session_version:
        return None
    session.clear()
    logout_user()
    session.clear()
    response = redirect(url_for("login"))
    response.delete_cookie(
        app.config.get("REMEMBER_COOKIE_NAME", "remember_token"),
        path=app.config.get("REMEMBER_COOKIE_PATH", "/"),
        domain=app.config.get("REMEMBER_COOKIE_DOMAIN"),
    )
    return response

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_global_vars():
    tz_name = "Asia/Shanghai"
    if current_user.is_authenticated:
        try:
            tz_name = SystemSetting.get("timezone", "Asia/Shanghai")
        except Exception:
            pass
        accounts = Account.query.all()
        scripts = ScriptTemplate.query.order_by(ScriptTemplate.created_at.desc()).all()
        first_sub = Subscription.query.first()
        return dict(all_accounts=accounts, preset_images=PRESET_IMAGES, all_scripts=scripts, default_sub=first_sub, current_timezone=tz_name, csrf_token=session.get(CSRF_SESSION_KEY))
    return dict(all_accounts=[], preset_images=PRESET_IMAGES, all_scripts=[], default_sub=None, current_timezone=tz_name, csrf_token=session.get(CSRF_SESSION_KEY))

# ========================== 时区与时间格式化过滤器 ==========================

TIMEZONE_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Singapore": 8,
    "UTC": 0,
    "Europe/London": 0,
    "Europe/Paris": 1,
    "Europe/Berlin": 1,
    "America/New_York": -5,
    "America/Los_Angeles": -8,
}

def format_datetime_tz(dt, fmt="%Y-%m-%d %H:%M:%S"):
    """将 UTC 时间根据系统设置的时区偏移转换为本地时区时间"""
    if not dt:
        return "-"
    try:
        tz_name = SystemSetting.get("timezone", "Asia/Shanghai")
    except Exception:
        tz_name = "Asia/Shanghai"
    offset_hours = TIMEZONE_OFFSETS.get(tz_name, 8)
    local_dt = dt + timedelta(hours=offset_hours)
    return local_dt.strftime(fmt)

@app.template_filter("format_dt")
def template_format_dt(dt, fmt="%Y-%m-%d %H:%M"):
    return format_datetime_tz(dt, fmt)

# ========================== CLI 命令 ==========================

@app.cli.command("initdb")
@click.option("--drop", is_flag=True, help="Drop tables before creating.")
def initdb(drop):
    """初始化数据库表"""
    if drop:
        db.drop_all()
        click.echo("Dropped old tables.")
    db.create_all()
    click.echo("Database initialized successfully.")

@app.cli.command("admin")
@click.argument("username")
@click.argument("password")
def create_admin(username, password):
    """创建或更新管理员账号"""
    if not validate_password(password):
        raise click.UsageError(password_policy_message())
    db.create_all()
    ensure_security_schema()
    user = User.query.filter_by(username=username).first()
    if user:
        user.set_password(password)
        user.reset_failed_attempts()
        user.session_version += 1
        click.echo(f"Admin user '{username}' password updated.")
    else:
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        click.echo(f"Admin user '{username}' created successfully.")
    db.session.commit()

# ========================== 认证路由 ==========================

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if User.query.count() == 0:
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            if not username or not password:
                flash("用户名或密码不能为空", "danger")
                return render_template("login.html", is_first_run=True)
            if not validate_password(password):
                flash(password_policy_message(), "danger")
                return render_template("login.html", is_first_run=True)
            confirm_password = request.form.get("confirm_password", "")
            if not confirm_password:
                flash("请再次输入密码", "danger")
                return render_template("login.html", is_first_run=True)
            if request.form.get("password", "") != confirm_password:
                flash("两次输入的密码不一致", "danger")
                return render_template("login.html", is_first_run=True)
            admin_user = User(username=username)
            admin_user.set_password(password)
            db.session.add(admin_user)
            db.session.commit()
            login_user(admin_user)
            session["_auth_version"] = admin_user.session_version
            flash("管理员账号初始化成功，欢迎使用！", "success")
            return redirect(url_for("index"))
        return render_template("login.html", is_first_run=True)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        source_ip = request.remote_addr or ""
        generic_login_error = "用户名或密码错误，请稍后再试。"
        rate_limited_login_error = "登录尝试过多，请 15 分钟后再试。"

        # 在查询用户或校验密码前先执行来源限流，避免通过更换用户名绕过限制。
        if is_login_rate_limited(source_ip, username):
            flash(rate_limited_login_error, "danger")
            return render_template("login.html")

        user = User.query.filter_by(username=username).first()
        if not user or user.is_locked():
            if not user and not record_login_failure(source_ip, username):
                flash(generic_login_error, "danger")
                return render_template("login.html")
            if user and user.is_locked():
                flash(rate_limited_login_error, "danger")
            else:
                flash(generic_login_error, "danger")
            return render_template("login.html")

        if user.check_password(password):
            # 无法安全清除限流状态时拒绝本次认证，避免在数据库异常时失去保护。
            if not clear_login_failures(source_ip, username):
                flash(generic_login_error, "danger")
                return render_template("login.html")
            user.reset_failed_attempts()
            login_user(user, remember=True)
            session["_auth_version"] = user.session_version
            flash("登录成功", "success")
            return redirect(url_for("index"))

        user.register_failed_attempt()
        if not record_login_failure(source_ip, username):
            flash(generic_login_error, "danger")
            return render_template("login.html")
        flash(generic_login_error, "danger")
        return render_template("login.html")

    return render_template("login.html", is_first_run=False)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("您已安全退出", "info")
    return redirect(url_for("login"))

# ========================== 首页与模块架构 ==========================

@app.route("/")
@login_required
def index():
    return redirect(url_for("vms_page"))

# 1. 虚拟机模块
@app.route("/vms")
@login_required
def vms_page():
    sub_id = request.args.get("sub_id", type=int)
    current_sub = Subscription.query.get(sub_id) if sub_id else None
    all_subs = Subscription.query.all()
    account = current_sub.account if current_sub else None
    return render_template(
        "vms_list.html",
        active_module="vms",
        current_sub=current_sub,
        all_subs=all_subs,
        account=account
    )

@app.route("/api/sub/vms_data")
def api_sub_vms_data():
    """本地数据库优先极速秒开，强制刷新以后台任务方式执行。"""
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    sub_id = request.args.get("sub_id", type=int)
    force_refresh = (request.args.get("refresh") == "1")

    if sub_id:
        target_subs = [Subscription.query.get_or_404(sub_id)]
    else:
        target_subs = Subscription.query.all()

    def load_cached_vms():
        cached_query = VmCache.query
        if sub_id:
            cached_query = cached_query.filter_by(subscription_id=sub_id)
        cached_vms = []
        for cv in cached_query.all():
            sub = Subscription.query.get(cv.subscription_id)
            acc_name = sub.account.name if (sub and sub.account) else ""
            sub_name = sub.display_name if sub else ""
            cached_vms.append({
                "name": cv.name,
                "resource_group": cv.resource_group,
                "location": cv.location,
                "vm_size": cv.vm_size,
                "os_type": cv.os_type,
                "power_state": cv.power_state,
                "provisioning_state": cv.provisioning_state,
                "private_ip": cv.private_ip,
                "public_ip": cv.public_ip,
                "sub_id": cv.subscription_id,
                "sub_name": sub_name,
                "account_name": acc_name
            })
        return cached_vms

    def load_active_tasks():
        subscription_ids = [subscription.id for subscription in target_subs]
        if not subscription_ids:
            return []
        active_tasks = DeploymentTask.query.filter(
            DeploymentTask.subscription_id.in_(subscription_ids),
            DeploymentTask.status.in_(["InProgress", "Pending"])
        ).order_by(DeploymentTask.created_at.desc()).limit(200).all()
        return [{
            "id": task.id,
            "target_name": task.target_name,
            "resource_group": task.resource_group,
            "task_type": task.task_type,
            "status": task.status,
            "progress_msg": task.progress_msg,
            "error_detail": None,
            "subscription_id": task.subscription_id,
            "created_at": format_datetime_tz(task.created_at, "%Y-%m-%d %H:%M:%S")
        } for task in active_tasks]

    # 1. 非强制刷新时，仅使用 15 分钟内完整有效的 SQLite 缓存
    if not force_refresh:
        cached_records = VmCache.query.filter_by(subscription_id=sub_id).all() if sub_id else VmCache.query.all()
        cache_expiry = datetime.utcnow() - timedelta(minutes=15)
        cache_has_all_subscriptions = {record.subscription_id for record in cached_records} == {subscription.id for subscription in target_subs}
        cache_is_fresh = bool(cached_records) and cache_has_all_subscriptions and all(
            record.updated_at and record.updated_at >= cache_expiry for record in cached_records
        )
        if cache_is_fresh:
            return jsonify({"status": "success", "vms": load_cached_vms(), "active_tasks": load_active_tasks(), "from_cache": True})

    first_sub_id = target_subs[0].id if target_subs else 1
    sub_label = target_subs[0].display_name if len(target_subs) == 1 else f"全部 {len(target_subs)} 个订阅"

    sync_task = None
    if force_refresh:
        sync_task = DeploymentTask(
            subscription_id=first_sub_id,
            task_type="sync_vms",
            target_name=sub_label,
            resource_group="Azure Cloud",
            status="Pending",
            progress_msg=f"正在同步【{sub_label}】虚拟机实时状态与公网 IP..."
        )
        db.session.add(sync_task)
        db.session.commit()

    sub_params_list = []
    for s in target_subs:
        acc = s.account
        sub_params_list.append({
            "sub_id": s.id,
            "sub_name": s.display_name,
            "sub_azure_id": s.subscription_id,
            "account_name": acc.name,
            "tenant_id": acc.tenant_id,
            "client_id": acc.client_id,
            "client_secret": acc.client_secret
        })

    def fetch_and_persist_sub_vms(params):
        try:
            vms = AzureService.list_vms(
                params["tenant_id"], params["client_id"], params["client_secret"],
                params["sub_azure_id"], force_refresh=True
            )
            for vm in vms:
                vm["sub_id"] = params["sub_id"]
                vm["sub_name"] = params["sub_name"]
                vm["account_name"] = params["account_name"]

            with app.app_context():
                VmCache.query.filter_by(subscription_id=params["sub_id"]).delete()
                for vm in vms:
                    db.session.add(VmCache(
                        subscription_id=params["sub_id"],
                        name=vm["name"],
                        resource_group=vm["resource_group"],
                        location=vm["location"],
                        vm_size=vm["vm_size"],
                        os_type=vm["os_type"],
                        power_state=vm["power_state"],
                        provisioning_state=vm["provisioning_state"],
                        private_ip=vm["private_ip"],
                        public_ip=vm["public_ip"],
                        nic_name=vm.get("nic_name", "-"),
                        nsg_name=vm.get("nsg_name", "-"),
                        image_ref=vm.get("image_ref", "-"),
                        admin_username=vm.get("admin_username", "azureuser"),
                        auth_mode=vm.get("auth_mode", "password"),
                        updated_at=datetime.utcnow()
                    ))
                db.session.commit()
            return {"sub_id": params["sub_id"], "vms": vms, "error": None}
        except Exception as exc:
            logger.warning(f"Failed to list VMs for sub {params['sub_azure_id']}: {exc}")
            with app.app_context():
                cached_vms = []
                for cv in VmCache.query.filter_by(subscription_id=params["sub_id"]).all():
                    cached_vms.append({
                        "name": cv.name,
                        "resource_group": cv.resource_group,
                        "location": cv.location,
                        "vm_size": cv.vm_size,
                        "os_type": cv.os_type,
                        "power_state": cv.power_state,
                        "provisioning_state": cv.provisioning_state,
                        "private_ip": cv.private_ip,
                        "public_ip": cv.public_ip,
                        "sub_id": params["sub_id"],
                        "sub_name": params["sub_name"],
                        "account_name": params["account_name"]
                    })
                return {"sub_id": params["sub_id"], "vms": cached_vms,
                        "error": public_error_message("虚拟机列表同步")}

    def execute_vm_sync(task_id=None):
        import concurrent.futures
        all_vms = []
        failed_subs = []
        # 此任务本身已经占用全局后台执行器的一个额度；内部查询保持单路，
        # 避免单次跨订阅刷新突破进程级 Azure 任务并发上限。
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            results = list(executor.map(fetch_and_persist_sub_vms, sub_params_list)) if sub_params_list else []
        for result in results:
            all_vms.extend(result["vms"])
            if result["error"]:
                failed_subs.append(result)

        if task_id:
            task = db.session.get(DeploymentTask, task_id)
            if task:
                if failed_subs:
                    task.status = "Failed"
                    task.progress_msg = "虚拟机列表同步失败，已保留旧缓存"
                    task.error_detail = "; ".join(
                        f"订阅 {item['sub_id']}: {item['error']}" for item in failed_subs
                    )
                else:
                    task.status = "Succeeded"
                    task.progress_msg = f"虚拟机列表与状态同步完成，共识别 {len(all_vms)} 台实例！"
                db.session.commit()
        return all_vms, failed_subs

    def run_vm_sync_task(task_id):
        try:
            with app.app_context():
                execute_vm_sync(task_id)
        except Exception as exc:
            logger.exception("VM 列表后台同步失败")
            with app.app_context():
                task = db.session.get(DeploymentTask, task_id)
                if task and task.status in ("Pending", "InProgress"):
                    task.status = "Failed"
                    task.progress_msg = "虚拟机列表同步失败"
                    task.error_detail = public_error_message()
                    db.session.commit()

    if force_refresh:
        submit_background_task(run_vm_sync_task, sync_task.id, task_id=sync_task.id)
        return jsonify({
            "status": "success",
            "vms": load_cached_vms(),
            "active_tasks": load_active_tasks(),
            "from_cache": True,
            "refresh_pending": True,
            "task_id": sync_task.id,
            "task_type": "sync_vms",
            "target_name": sub_label
        })

    all_vms, failed_subs = execute_vm_sync()
    return jsonify({
        "status": "success",
        "vms": all_vms,
        "active_tasks": load_active_tasks(),
        "from_cache": bool(failed_subs),
        "refresh_failed": bool(failed_subs),
        "task_id": None,
        "task_type": None,
        "target_name": None,
        "message": "Azure 同步失败，当前显示旧缓存" if failed_subs else None
    })

@app.route("/vm/create", methods=["GET", "POST"])
@login_required
def create_vm_page():
    sub_id = request.args.get("sub_id", type=int)
    if sub_id:
        subscription = Subscription.query.get(sub_id)
    else:
        subscription = Subscription.query.first()

    if not subscription:
        flash("请先在【云账户】中添加 Azure 凭据并识别订阅", "warning")
        return redirect(url_for("accounts_page"))

    account = subscription.account

    if request.method == "POST":
        vm_prefix = request.form.get("name", "").strip().lower()
        location = request.form.get("location", "eastasia")
        vm_size = request.form.get("vm_size", "Standard_B1s")
        image_urn = request.form.get("image", "").strip()
        custom_image_urn = request.form.get("custom_image_urn", "").strip()
        admin_username = request.form.get("admin_username", "azureuser").strip()
        admin_password = request.form.get("admin_password", "").strip()
        admin_password_confirm = request.form.get("admin_password_confirm", "").strip()
        ssh_public_key = request.form.get("ssh_public_key", "").strip()
        auth_type = request.form.get("auth_type", "password").strip()
        disk_size_raw = request.form.get("disk_size_gb", 30)

        # 安全读取 CustomData：优先从选择的 script_id 加密模板解密注入，绝不在前端暴露明文
        script_id = request.form.get("script_id", type=int)
        custom_data = request.form.get("custom_data", "")
        if script_id:
            tpl = db.session.get(ScriptTemplate, script_id)
            if tpl and tpl.content:
                custom_data = tpl.content

        try:
            disk_size_gb, count = validate_vm_request(
                vm_prefix, location, vm_size, image_urn, custom_image_urn,
                admin_username, admin_password, ssh_public_key,
                disk_size_raw, request.form.get("count", 1), custom_data,
                auth_type, admin_password_confirm
            )
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))

        accelerated_networking = (request.form.get("accelerated_networking") == "true")
        spot_instance = (request.form.get("spot_instance") == "true")

        chosen_image = custom_image_urn if custom_image_urn else image_urn
        selected_image_architecture = None

        location_key = location.lower().replace(" ", "")
        selected_sku = SkuCache.query.filter_by(
            subscription_id=subscription.id, location=location_key, name=vm_size
        ).first()
        if not selected_sku:
            flash("所选虚拟机规格已过期，请重新加载当前地域的规格列表", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        if selected_sku.restrictions:
            flash("所选虚拟机规格当前受限，无法创建", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        if not selected_sku.architecture:
            flash("所选规格缺少 Azure 架构信息，请重新加载规格列表", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        if spot_instance and selected_sku.spot_capable is not True:
            flash("所选规格或当前订阅不支持 Spot 抢占式计费", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        if accelerated_networking and selected_sku.accelerated_networking_supported is not True:
            flash("所选规格不支持网络加速", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        if selected_sku.accelerated_networking_required is True:
            accelerated_networking = True
        if accelerated_networking and custom_image_urn:
            flash("自定义镜像无法确认网络加速驱动，请使用官方动态镜像或关闭网络加速", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))
        selected_image_architecture = selected_sku.architecture

        if not custom_image_urn:
            cached_image = ImageCache.query.filter_by(
                subscription_id=subscription.id, location=location_key,
                architecture=selected_image_architecture, urn=image_urn
            ).first()
            if cached_image and cached_image.urn:
                chosen_image = cached_image.urn
                selected_image_architecture = cached_image.architecture
                if auth_type == "ssh_key" and (cached_image.os_type or "").lower() == "windows":
                    flash("Windows 镜像不支持仅使用 SSH 公钥认证，请改用密码认证", "danger")
                    return redirect(url_for("create_vm_page", sub_id=subscription.id))
            elif cached_image:
                flash("当前镜像缓存缺少真实 URN，请重新加载镜像列表", "danger")
                return redirect(url_for("create_vm_page", sub_id=subscription.id))
            else:
                flash("所选动态镜像不在当前地域缓存中，请重新加载镜像列表", "danger")
                return redirect(url_for("create_vm_page", sub_id=subscription.id))
        elif auth_type == "ssh_key" and "windows" in custom_image_urn.lower():
            flash("Windows 镜像不支持仅使用 SSH 公钥认证，请改用密码认证", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))

        if not admin_password and not ssh_public_key:
            flash("必须提供管理员密码或 SSH 公钥之一", "danger")
            return redirect(url_for("create_vm_page", sub_id=subscription.id))

        tenant_id = account.tenant_id
        client_id = account.client_id
        client_secret = account.client_secret
        subscription_azure_id = subscription.subscription_id
        create_subscription_id = subscription.id

        def async_deploy(name_tag, task_id):
            rg_name = f"rg-{name_tag}"
            with app.app_context():
                def cb(msg):
                    try:
                        t = db.session.get(DeploymentTask, task_id)
                        if t:
                            t.status = "InProgress"
                            t.progress_msg = msg
                            db.session.commit()
                    except Exception:
                        pass

                try:
                    logger.info(f"Starting deployment for {name_tag} in {location}")
                    AzureService.create_vm_complete(
                        tenant_id, client_id, client_secret,
                        subscription_azure_id, rg_name, location, name_tag,
                        vm_size, chosen_image, admin_username,
                        admin_password if admin_password else None,
                        ssh_public_key if ssh_public_key else None,
                        disk_size_gb, custom_data if custom_data else None,
                        accelerated_networking, spot_instance,
                        progress_callback=cb,
                        image_architecture=selected_image_architecture
                    )
                    # Azure 创建成功后，先同步 VmCache，再把任务标记为完成。
                    # 否则前端收到成功状态后立即刷新，可能早于新 VM 写入列表缓存。
                    cache_sync_error = None
                    try:
                        t = db.session.get(DeploymentTask, task_id)
                        if t:
                            t.progress_msg = "云端创建完成，正在同步虚拟机列表缓存..."
                            db.session.commit()

                        vms = AzureService.list_vms(tenant_id, client_id, client_secret, subscription_azure_id, force_refresh=True)
                        VmCache.query.filter_by(subscription_id=create_subscription_id).delete()
                        for v in vms:
                            rec = VmCache(
                                subscription_id=create_subscription_id,
                                name=v["name"],
                                resource_group=v["resource_group"],
                                location=v["location"],
                                vm_size=v["vm_size"],
                                os_type=v["os_type"],
                                power_state=v["power_state"],
                                provisioning_state=v["provisioning_state"],
                                private_ip=v["private_ip"],
                                public_ip=v["public_ip"],
                                nic_name=v.get("nic_name", "-"),
                                nsg_name=v.get("nsg_name", "-"),
                                image_ref=v.get("image_ref", "-"),
                                admin_username=v.get("admin_username", "azureuser"),
                                auth_mode=v.get("auth_mode", "password"),
                                updated_at=datetime.utcnow()
                            )
                            db.session.add(rec)
                        db.session.commit()
                        AzureService._vm_cache.pop(f"vms_{subscription_azure_id}", None)
                    except Exception as cache_err:
                        db.session.rollback()
                        cache_sync_error = public_error_message("虚拟机列表缓存同步")
                        logger.warning(f"Failed to auto-update VmCache after deploy: {cache_err}")

                    t = db.session.get(DeploymentTask, task_id)
                    if t:
                        t.status = "Succeeded"
                        t.progress_msg = "创建成功并已开机" if not cache_sync_error else "创建成功，但列表缓存同步失败，请刷新列表"
                        t.error_detail = cache_sync_error
                        db.session.commit()
                    logger.info(f"Deployment succeeded for {name_tag}")
                except Exception as err:
                    logger.exception("创建虚拟机失败：%s", name_tag)
                    err_msg = public_error_message("创建虚拟机")
                    try:
                        t = db.session.get(DeploymentTask, task_id)
                        if t:
                            t.status = "Failed"
                            t.progress_msg = "创建失败"
                            t.error_detail = err_msg
                            db.session.commit()
                    except Exception:
                        pass

        created_tasks = []
        for i in range(count):
            tag = f"{vm_prefix}-{i+1}" if count > 1 else vm_prefix
            reservation = reserve_vm_operation_task(
                subscription.id, f"rg-{tag}", tag, "create",
                "任务已创建，正在排队启动..."
            )
            created_tasks.append({"id": reservation["task_id"], "target_name": tag})
            if reservation["created"]:
                submit_background_task(async_deploy, tag, reservation["task_id"], task_id=reservation["task_id"])

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "submitted",
                "tasks": created_tasks,
                "first_task_id": created_tasks[0]["id"] if created_tasks else None,
                "target_name": created_tasks[0]["target_name"] if created_tasks else vm_prefix
            })

        flash(f"已创建 {count} 个部署任务，可在【活动日志】或列表中实时查看进度！", "success")
        return redirect(url_for("vms_page", sub_id=subscription.id))

    return render_template(
        "vm_create.html",
        active_module="vms",
        subscription=subscription,
        account=account
    )

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>")
@login_required
def vm_detail(sub_id, resource_group, vm_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    active_task = DeploymentTask.query.filter(
        DeploymentTask.subscription_id == sub_id,
        DeploymentTask.resource_group == resource_group,
        DeploymentTask.target_name == vm_name,
        DeploymentTask.status.in_(["InProgress", "Pending"])
    ).order_by(DeploymentTask.created_at.desc()).first()
    active_task_type = active_task.task_type if active_task else None
    active_task_snapshot = None
    if active_task:
        active_task_snapshot = {
            "id": active_task.id,
            "target_name": active_task.target_name,
            "task_type": active_task.task_type,
            "status": active_task.status,
            "progress_msg": active_task.progress_msg,
            "error_detail": None,
        }


    # 1. 优先从 SQLite 缓存极速读取 VM 基础属性（如果缓存完整且包含有效 IP/系统信息直接返回）
    vm_rec = VmCache.query.filter_by(subscription_id=sub_id, name=vm_name).first()
    if vm_rec and vm_rec.nic_name != '-' and vm_rec.nsg_name != '-':
        vm = {
            "name": vm_rec.name,
            "resource_group": vm_rec.resource_group,
            "location": vm_rec.location,
            "vm_size": vm_rec.vm_size,
            "os_type": vm_rec.os_type,
            "power_state": vm_rec.power_state,
            "provisioning_state": vm_rec.provisioning_state,
            "private_ip": vm_rec.private_ip,
            "public_ip": vm_rec.public_ip,
            "nic_name": vm_rec.nic_name,
            "nsg_name": vm_rec.nsg_name,
            "image_ref": vm_rec.image_ref,
            "admin_username": vm_rec.admin_username,
            "detected_users": [vm_rec.admin_username],
            "auth_mode": vm_rec.auth_mode
        }
        return render_template("vm_detail.html", active_module="vms", subscription=subscription, account=account, vm=vm, vm_is_arm=AzureService.is_arm_vm_size(vm.get("vm_size", "")), active_task_type=active_task_type, active_task_snapshot=active_task_snapshot)

    # 2. 若本地缓存信息不完整或首次访问，调用 API 穿透拉取并同步回填缓存
    try:
        vm = AzureService.get_vm_detail(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, resource_group, vm_name
        )
        # 同步回填/更新 VmCache
        if not vm_rec:
            vm_rec = VmCache(subscription_id=sub_id, name=vm_name)
            db.session.add(vm_rec)
        vm_rec.resource_group = resource_group
        vm_rec.location = vm.get("location", "")
        vm_rec.vm_size = vm.get("vm_size", "-")
        vm_rec.os_type = vm.get("os_type", "Linux")
        vm_rec.power_state = vm.get("power_state", "Running")
        vm_rec.provisioning_state = vm.get("provisioning_state", "Succeeded")
        vm_rec.private_ip = vm.get("private_ip", "-")
        vm_rec.public_ip = vm.get("public_ip", "-")
        vm_rec.nic_name = vm.get("nic_name", "-")
        vm_rec.nsg_name = vm.get("nsg_name", "-")
        vm_rec.image_ref = vm.get("image_ref", "-")
        vm_rec.admin_username = vm.get("admin_username", "azureuser")
        vm_rec.auth_mode = vm.get("auth_mode", "password")
        vm_rec.updated_at = datetime.utcnow()
        db.session.commit()

        return render_template("vm_detail.html", active_module="vms", subscription=subscription, account=account, vm=vm, vm_is_arm=AzureService.is_arm_vm_size(vm.get("vm_size", "")), active_task_type=active_task_type, active_task_snapshot=active_task_snapshot)
    except Exception:
        logger.exception("获取虚拟机详情失败：%s", vm_name)
        flash(public_error_message("获取虚拟机详情"), "danger")
        return redirect(url_for("vms_page", sub_id=sub_id))

# 2. 费用模块
@app.route("/costs")
@login_required
def costs_page():
    sub_id = request.args.get("sub_id", type=int)
    current_sub = Subscription.query.get(sub_id) if sub_id else None
    all_subs = Subscription.query.all()
    target_subs = [current_sub] if current_sub else all_subs
    return render_template(
        "costs.html",
        active_module="costs",
        current_sub=current_sub,
        all_subs=all_subs,
        target_subs=target_subs
    )

# ========================== 账单费用模块 (异步刷新与秒级开播) ==========================

@app.route("/api/sub/<int:sub_id>/costs_refresh_action", methods=["POST"])
@login_required
def api_sub_costs_refresh_action(sub_id):
    """立即创建 InProgress 任务，并后台异步刷新 Azure 账单"""
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    target_name = f"{subscription.display_name} 费用账单"
    reservation = reserve_unique_task(
        sub_id, "Azure Cost Management", target_name, "sync_costs",
        f"正在从 Azure Cost Management 查询【{subscription.display_name}】最新账单..."
    )
    task_id = reservation["task_id"]
    if not reservation["created"]:
        return jsonify({"status": "in_progress", "task_id": task_id,
                        "target_name": target_name, "task_type": "sync_costs",
                        "message": "该订阅已有账单刷新任务正在执行。"})

    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    sub_azure_id = subscription.subscription_id
    sub_name = subscription.display_name

    def async_fetch_costs():
        with app.app_context():
            costs_info = None
            fetch_error = None
            try:
                costs_info = AzureService.get_subscription_costs(tenant_id, client_id, client_secret, sub_azure_id, force_refresh=True)
            except Exception:
                logger.exception("账单刷新失败：%s", sub_name)
                fetch_error = public_error_message("账单刷新")

            try:
                db.session.rollback()
                if costs_info:
                    cost_rec = CostCache.query.filter_by(subscription_id=sub_id).first()
                    if not cost_rec:
                        cost_rec = CostCache(subscription_id=sub_id)
                        db.session.add(cost_rec)

                    cost_rec.total_cost = costs_info.get("total_cost", 0.0)
                    cost_rec.currency = costs_info.get("currency", "USD")
                    cost_rec.status = costs_info.get("status", "Success")
                    cost_rec.message = costs_info.get("message", "")
                    cost_rec.updated_at = datetime.utcnow()

                t = db.session.get(DeploymentTask, task_id)
                if t:
                    if costs_info and costs_info.get("status") == "Success":
                        t.status = "Succeeded"
                        t.progress_msg = f"【{sub_name}】账单同步成功，本月消费: ${costs_info.get('total_cost')} {costs_info.get('currency')}"
                    elif costs_info and costs_info.get("status") == "Warning":
                        t.status = "Succeeded"
                        t.progress_msg = f"【{sub_name}】账单同步完成 (部分数据受限)"
                        t.error_detail = costs_info.get("message", "")
                    else:
                        t.status = "Failed"
                        t.progress_msg = f"【{sub_name}】账单查询失败"
                        t.error_detail = fetch_error or public_error_message("账单刷新")
                db.session.commit()
            except Exception as commit_err:
                logger.error(f"Failed to commit async task status: {commit_err}")
                db.session.rollback()
            finally:
                db.session.remove()

    submit_background_task(async_fetch_costs, task_id=task_id)
    return jsonify({"status": "submitted", "task_id": task_id, "target_name": target_name, "task_type": "sync_costs"})


@app.route("/api/sub/<int:sub_id>/costs")
@login_required
def api_sub_costs(sub_id):
    """费用本地秒开模式"""
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    force_refresh = (request.args.get("refresh") == "1")

    # 优先从 SQLite 本地账单缓存读取 (10ms 秒开)
    if not force_refresh:
        cost_rec = CostCache.query.filter_by(subscription_id=sub_id).first()
        if cost_rec and (datetime.utcnow() - cost_rec.updated_at).total_seconds() < 1800:
            return jsonify({
                "status": cost_rec.status,
                "total_cost": cost_rec.total_cost,
                "currency": cost_rec.currency,
                "message": cost_rec.message,
                "from_cache": True
            })

    costs_info = AzureService.get_subscription_costs(account.tenant_id, account.client_id, account.client_secret, subscription.subscription_id, force_refresh=force_refresh)

    cost_rec = CostCache.query.filter_by(subscription_id=sub_id).first()
    if not cost_rec:
        cost_rec = CostCache(subscription_id=sub_id)
        db.session.add(cost_rec)

    cost_rec.total_cost = costs_info.get("total_cost", 0.0)
    cost_rec.currency = costs_info.get("currency", "USD")
    cost_rec.status = costs_info.get("status", "Success")
    cost_rec.message = costs_info.get("message", "")
    cost_rec.updated_at = datetime.utcnow()

    # 当用户主动点击刷新账单时，向活动日志静默写入一条任务记录
    if force_refresh:
        is_success = (costs_info.get("status") == "Success")
        log_task = DeploymentTask(
            subscription_id=sub_id,
            task_type="sync_costs",
            target_name=f"{subscription.display_name} 费用账单",
            resource_group="Azure Cost Management",
            status="Succeeded" if is_success else "Failed",
            progress_msg=f"【{subscription.display_name}】账单同步成功，本月实际消费: ${costs_info.get('total_cost')} {costs_info.get('currency')}" if is_success else f"【{subscription.display_name}】账单同步受限/失败",
            error_detail=costs_info.get("message", "") if not is_success else None
        )
        db.session.add(log_task)

    db.session.commit()

    return jsonify(costs_info)

# 3. 活动日志模块

def _task_rule_name(task):
    rule_name = getattr(task, "rule_name", None)
    if rule_name:
        return rule_name
    if task.task_type in ("firewall_add", "firewall_delete"):
        match = re.search(r"防火墙(?:安全)?规则【([^】]+)】", task.progress_msg or "")
        if match:
            return match.group(1)
    return None


def _task_payload(t):
    return {
        "id": t.id,
        "record_type": "task",
        "deletable": t.status not in ("Pending", "InProgress"),
        "target_name": t.target_name,
        "resource_group": t.resource_group,
        "task_type": t.task_type,
        "rule_name": _task_rule_name(t),
        "status": t.status,
        "progress_msg": t.progress_msg,
        "error_detail": public_error_message("任务") if t.status == "Failed" and t.error_detail else None,
        "subscription_id": t.subscription_id,
        "created_at": format_datetime_tz(t.created_at, "%Y-%m-%d %H:%M:%S")
    }

@app.route("/tasks")
@app.route("/tasks/<int:sub_id>")
@login_required
def tasks_page(sub_id=None):
    target_sub_id = sub_id or request.args.get("sub_id", type=int)
    current_sub = Subscription.query.get(target_sub_id) if target_sub_id else None
    return render_template(
        "tasks.html",
        active_module="tasks",
        current_sub=current_sub
    )

@app.route("/api/tasks/list")
def api_tasks_list():
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    sub_id = request.args.get("sub_id", type=int)
    if sub_id:
        tasks = DeploymentTask.query.filter_by(subscription_id=sub_id).order_by(DeploymentTask.created_at.desc()).limit(100).all()
    else:
        tasks = DeploymentTask.query.order_by(DeploymentTask.created_at.desc()).limit(200).all()

    return jsonify({
        "tasks": [{
            "id": t.id,
            "target_name": t.target_name,
            "resource_group": t.resource_group,
            "task_type": t.task_type,
            "rule_name": _task_rule_name(t),
            "status": t.status,
            "progress_msg": t.progress_msg,
            "error_detail": public_error_message("任务") if t.status == "Failed" and t.error_detail else None,
            "subscription_id": t.subscription_id,
            "created_at": format_datetime_tz(t.created_at, "%Y-%m-%d %H:%M:%S")
        } for t in tasks]
    })


@app.route("/api/activity/list")
@login_required
def api_activity_list():
    """活动日志仅展示可清理的 Azure 管理任务。"""
    sub_id = request.args.get("sub_id", type=int)
    task_query = DeploymentTask.query
    if sub_id:
        task_query = task_query.filter_by(subscription_id=sub_id)
    tasks = task_query.order_by(DeploymentTask.created_at.desc()).limit(100).all()
    return jsonify({"records": [_task_payload(task) for task in tasks]})


@app.route("/api/tasks/clear", methods=["POST"])
@login_required
def api_tasks_clear():
    sub_id = request.args.get("sub_id", type=int)
    query = DeploymentTask.query.filter(~DeploymentTask.status.in_(["Pending", "InProgress"]))
    if sub_id:
        query = query.filter_by(subscription_id=sub_id)
    deleted_count = query.delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"status": "success", "deleted_count": deleted_count, "active_tasks_kept": True})

@app.route("/api/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def api_tasks_delete(task_id):
    task = db.session.get(DeploymentTask, task_id)
    if not task:
        return jsonify({"status": "error", "message": "活动记录不存在"}), 404
    if task.status in ("Pending", "InProgress"):
        return jsonify({"status": "error", "message": "进行中的任务不能删除"}), 409
    db.session.delete(task)
    db.session.commit()
    return jsonify({"status": "success"})

# 4. 云账户模块
@app.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts_page():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_account":
            name = request.form.get("name", "").strip()
            api_string = request.form.get("api_string", "").strip()
            tenant_id = request.form.get("tenant_id", "").strip()
            client_id = request.form.get("client_id", "").strip()
            client_secret = request.form.get("client_secret", "").strip()

            if api_string:
                cleaned_str = api_string.strip().strip("'").strip('"')
                parts = [p.strip().strip("'").strip('"') for p in cleaned_str.split("|") if p.strip()]
                if len(parts) >= 3:
                    client_id = parts[0]
                    client_secret = parts[1]
                    tenant_id = parts[2]
                elif len(parts) == 1 and ("{" in cleaned_str or "appId" in cleaned_str):
                    import json
                    try:
                        j_data = json.loads(cleaned_str)
                        client_id = j_data.get("appId") or j_data.get("clientId")
                        client_secret = j_data.get("password") or j_data.get("clientSecret")
                        tenant_id = j_data.get("tenant") or j_data.get("tenantId")
                    except Exception:
                        pass

            tenant_id = tenant_id.strip().strip("'").strip('"') if tenant_id else ""
            client_id = client_id.strip().strip("'").strip('"') if client_id else ""
            client_secret = client_secret.strip().strip("'").strip('"') if client_secret else ""

            if not name or not tenant_id or not client_id or not client_secret:
                flash("请完整填写账户名称及 Azure Service Principal 凭据信息", "danger")
                return redirect(url_for("accounts_page"))

            try:
                subs_data = AzureService.list_subscriptions(tenant_id, client_id, client_secret)
                if not subs_data:
                    flash("连接成功，但此凭据未被分配任何可用的 Azure 订阅角色（需 Contributor 或 Reader 权限）", "warning")

                account = Account(name=name, tenant_id=tenant_id, client_id=client_id)
                account.client_secret = client_secret
                db.session.add(account)
                db.session.flush()

                for s in subs_data:
                    sub_obj = Subscription(
                        account_id=account.id,
                        subscription_id=s["subscription_id"],
                        display_name=s["display_name"],
                        state=s["state"],
                        spending_limit=s["spending_limit"]
                    )
                    db.session.add(sub_obj)

                db.session.commit()
                flash(f"账户 '{name}' 添加成功，已自动识别并同步 {len(subs_data)} 个订阅！", "success")
                return redirect(url_for("accounts_page"))
            except Exception as e:
                logger.exception("Azure 凭据验证失败")
                flash(public_error_message("Azure 凭据验证"), "danger")
                return redirect(url_for("accounts_page"))

    accounts = Account.query.all()
    return render_template("accounts.html", active_module="accounts", accounts=accounts)

@app.route("/account/<int:account_id>/sync_action", methods=["POST"])
@login_required
def account_sync_action(account_id):
    account = Account.query.get_or_404(account_id)
    acc_name = account.name
    first_sub = account.subscriptions[0] if account.subscriptions else None
    sub_pk = first_sub.id if first_sub else None

    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret

    # reserve_unique_task 原子写入 status="Pending"，再提交后台执行。
    reservation = reserve_unique_task(
        sub_pk, "Azure Active Directory", acc_name,
        task_type="sync_subscriptions",
        progress_msg=f"正在从 Azure Active Directory 同步账户【{acc_name}】的订阅..."
    )
    sync_task_id = reservation["task_id"]
    if not reservation["created"]:
        message = f"账户【{acc_name}】已有订阅同步任务正在执行。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"status": "in_progress", "task_id": sync_task_id, "acc_name": acc_name,
                            "task_type": "sync_subscriptions", "message": message})
        flash(message, "warning")
        return redirect(url_for("accounts_page"))

    def async_sync_subscriptions():
        with app.app_context():
            try:
                subs_data = AzureService.list_subscriptions(tenant_id, client_id, client_secret)
                acc = db.session.get(Account, account_id)
                if not acc:
                    raise ValueError("Azure 账户不存在或已被删除")
                existing_sub_ids = {s.subscription_id: s for s in acc.subscriptions}
                new_ids = set()
                for item in subs_data:
                    new_ids.add(item["subscription_id"])
                    if item["subscription_id"] in existing_sub_ids:
                        sub = existing_sub_ids[item["subscription_id"]]
                        sub.display_name = item["display_name"]
                        sub.state = item["state"]
                        sub.spending_limit = item["spending_limit"]
                        sub.last_synced_at = datetime.utcnow()
                    else:
                        db.session.add(Subscription(
                            account_id=acc.id,
                            subscription_id=item["subscription_id"],
                            display_name=item["display_name"],
                            state=item["state"],
                            spending_limit=item["spending_limit"]
                        ))
                for old_id, old_sub in existing_sub_ids.items():
                    if old_id not in new_ids:
                        old_sub.state = "Removed"
                        old_sub.last_synced_at = datetime.utcnow()
                task = db.session.get(DeploymentTask, sync_task_id)
                if task:
                    task.status = "Succeeded"
                    task.progress_msg = f"账户【{acc_name}】订阅同步成功，已识别 {len(subs_data)} 个有效订阅。"
                db.session.commit()
            except Exception as exc:
                logger.exception("账户订阅同步失败")
                error_message = public_error_message("账户订阅同步")
                db.session.rollback()
                task = db.session.get(DeploymentTask, sync_task_id)
                if task:
                    task.status = "Failed"
                    task.progress_msg = f"同步账户【{acc_name}】订阅失败"
                    task.error_detail = error_message
                db.session.commit()
            finally:
                db.session.remove()

    submit_background_task(async_sync_subscriptions, task_id=sync_task_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "submitted", "task_id": sync_task_id, "acc_name": acc_name, "task_type": "sync_subscriptions"})
    flash(f"已提交账户【{acc_name}】订阅同步任务，请在右上角活动通知查看进度。", "info")
    return redirect(url_for("accounts_page"))

@app.route("/account/<int:account_id>/delete_action", methods=["POST"])
@login_required
def account_delete_action(account_id):
    account = Account.query.get_or_404(account_id)
    name = account.name
    subscription_ids = [subscription.id for subscription in account.subscriptions]
    active_task = None
    if subscription_ids:
        active_task = DeploymentTask.query.filter(
            DeploymentTask.subscription_id.in_(subscription_ids),
            DeploymentTask.status.in_(["Pending", "InProgress"])
        ).first()
    if active_task:
        message = "该账户存在正在执行的 Azure 任务，完成或失败后才能删除账户。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"status": "error", "message": message}), 409
        flash(message, "warning")
        return redirect(url_for("accounts_page"))
    db.session.delete(account)
    db.session.commit()
    flash(f"账户 '{name}' 及其关联订阅已删除", "info")
    return redirect(url_for("accounts_page"))

# 5. 设置模块
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    sub_tab = request.args.get("tab", "general")
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_timezone":
            selected_tz = request.form.get("timezone", "Asia/Shanghai").strip()
            SystemSetting.set("timezone", selected_tz)
            flash(f"常规时区设置已成功保存为：{selected_tz}", "success")
            return redirect(url_for("settings_page", tab="general"))

        elif action == "restore_backup":
            file = request.files.get("backup_file")
            if not file or not file.filename:
                flash("请选择需要恢复的 .db 数据库备份文件", "danger")
                return redirect(url_for("settings_page", tab="general"))

            if not file.filename.endswith(".db"):
                flash("备份文件格式不正确，仅支持 .db 格式数据库文件", "danger")
                return redirect(url_for("settings_page", tab="general"))

            if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
                flash("备份文件过大，最大允许 16 MB", "danger")
                return redirect(url_for("settings_page", tab="general"))

            temp_restore_path = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".restore-", suffix=".db", dir=data_dir, delete=False) as temp_file:
                    temp_restore_path = temp_file.name
                file.save(temp_restore_path)
                if os.path.getsize(temp_restore_path) > app.config["MAX_CONTENT_LENGTH"]:
                    raise ValueError("备份文件超过 16 MB 大小限制")
                with open(temp_restore_path, "rb") as backup_stream:
                    if backup_stream.read(16) != b"SQLite format 3\x00":
                        raise ValueError("备份文件不是有效的 SQLite 数据库")
                test_conn = sqlite3.connect(temp_restore_path)
                test_cursor = test_conn.cursor()
                test_cursor.execute("PRAGMA quick_check")
                if test_cursor.fetchone()[0] != "ok":
                    raise ValueError("SQLite 完整性检查失败")
                test_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
                if not test_cursor.fetchone():
                    test_conn.close()
                    os.remove(temp_restore_path)
                    flash("备份文件验证失败：未检测到有效的基础数据表！", "danger")
                    return redirect(url_for("settings_page", tab="general"))
                test_conn.close()

                with database_maintenance():
                    db.session.remove()
                    db.engine.dispose()
                    source = sqlite3.connect(temp_restore_path, timeout=30)
                    target = sqlite3.connect(db_path, timeout=30)
                    try:
                        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        source.backup(target)
                        target.commit()
                    finally:
                        target.close()
                        source.close()
                    db.engine.dispose()

                if temp_restore_path and os.path.exists(temp_restore_path):
                    os.remove(temp_restore_path)

                initialize_database_schema()
                db.session.commit()

                flash("数据库备份已成功恢复！请重启面板进程使所有 Worker 重新载入数据库连接。", "success")
                return redirect(url_for("settings_page", tab="general"))
            except Exception as e:
                logger.exception("恢复数据库失败")
                if temp_restore_path and os.path.exists(temp_restore_path):
                    os.remove(temp_restore_path)
                db.session.rollback()
                flash(public_error_message("恢复数据库"), "danger")
                return redirect(url_for("settings_page", tab="general"))

        elif action == "add_script":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            content = request.form.get("content", "").strip()

            if not name or not content:
                flash("脚本名称和内容不能为空", "danger")
                return redirect(url_for("settings_page", tab="user"))

            tpl = ScriptTemplate(name=name, description=description)
            tpl.content = content
            db.session.add(tpl)
            db.session.commit()
            flash(f"初始化脚本 {name} 已加密保存！", "success")
            return redirect(url_for("settings_page", tab="user"))

        elif action == "delete_script":
            script_id = int(request.form.get("script_id", 0))
            tpl = db.session.get(ScriptTemplate, script_id)
            if tpl:
                name = tpl.name
                db.session.delete(tpl)
                db.session.commit()
                db.session.commit()
                flash(f"脚本模板 {name} 已删除", "info")
            return redirect(url_for("settings_page", tab="user"))

        elif action == "change_password":
            old_pwd = request.form.get("old_password", "").strip()
            new_pwd = request.form.get("new_password", "").strip()
            confirm_pwd = request.form.get("confirm_password", "").strip()
            if not current_user.check_password(old_pwd):
                flash("当前旧密码输入错误，请重新输入", "danger")
            elif not validate_password(new_pwd):
                flash(password_policy_message(), "danger")
            elif new_pwd != confirm_pwd:
                flash("两次输入的新密码不一致，请重新输入", "danger")
            else:
                current_user.set_password(new_pwd)
                current_user.session_version += 1
                db.session.commit()
                logout_user()
                session.pop("_auth_version", None)
                flash("管理员密码修改成功，请使用新密码重新登录。", "success")
                return redirect(url_for("login"))
            return redirect(url_for("settings_page", tab="user"))

    scripts = ScriptTemplate.query.order_by(ScriptTemplate.created_at.desc()).all()
    return render_template("settings.html", active_module="settings", scripts=scripts, sub_tab=sub_tab)

@app.route("/settings/backup/download")
@login_required
def download_backup():
    """导出并下载面板 SQLite 数据库备份文件"""
    if not os.path.exists(db_path):
        flash("数据库文件不存在，无法导出备份", "danger")
        return redirect(url_for("settings_page", tab="general"))

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    download_filename = f"azure_manager_backup_{now_str}.db"
    try:
        snapshot_path = create_consistent_database_snapshot()
    except Exception:
        logger.exception("创建数据库快照失败")
        flash(public_error_message("导出数据库备份"), "danger")
        return redirect(url_for("settings_page", tab="general"))
    response = send_file(
        snapshot_path,
        as_attachment=True,
        download_name=download_filename,
        mimetype="application/x-sqlite3"
    )
    response.call_on_close(lambda: os.path.exists(snapshot_path) and os.remove(snapshot_path))
    return response

@app.route("/api/script/<int:script_id>")
@login_required
def api_get_script(script_id):
    tpl = ScriptTemplate.query.get_or_404(script_id)
    return jsonify({"status": "success", "content": tpl.content})

# ========================== VM 单机运维操作 ==========================

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/resize", methods=["POST"])
@login_required
def vm_resize(sub_id, resource_group, vm_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    try:
        target_vm_size = validate_vm_size(request.form.get("target_vm_size", "").strip())
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    sub_pk = subscription.id
    subscription_azure_id = subscription.subscription_id

    reservation = reserve_vm_operation_task(
        sub_pk, resource_group, vm_name, "resize",
        f"已提交调整规格至【{target_vm_size}】的任务..."
    )
    task_id = reservation["task_id"]
    if not reservation["created"]:
        message = f"虚拟机当前已有【{reservation['progress_msg']}】，已继续跟踪原任务。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "in_progress",
                "task_id": task_id,
                "target_name": vm_name,
                "task_type": reservation["task_type"],
                "message": message,
            })
        flash(message, "warning")
        return redirect(url_for(
            "vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name
        ))

    def async_resize():
        with app.app_context():
            def update_resize_progress(message):
                try:
                    task = db.session.get(DeploymentTask, task_id)
                    if task:
                        task.progress_msg = message
                        db.session.commit()
                except Exception:
                    db.session.rollback()

            try:
                result = AzureService.resize_vm(
                    tenant_id, client_id, client_secret, subscription_azure_id,
                    resource_group, vm_name, target_vm_size,
                    progress_callback=update_resize_progress
                )
                vm_cache = VmCache.query.filter_by(
                    subscription_id=sub_pk,
                    resource_group=resource_group,
                    name=vm_name,
                ).first()
                confirmed_size = result.get("new_size")
                final_power_state = result.get("final_power_state")
                if vm_cache:
                    if confirmed_size:
                        vm_cache.vm_size = confirmed_size
                    if final_power_state and final_power_state.lower() != "unknown":
                        vm_cache.power_state = final_power_state
                    vm_cache.updated_at = datetime.utcnow()

                AzureService._vm_cache.pop(f"vms_{subscription_azure_id}", None)
                AzureService.invalidate_vm_resize_options_cache(
                    tenant_id, client_id, client_secret, subscription_azure_id,
                    resource_group, vm_name
                )
                VmResizeOptionsCache.query.filter_by(
                    subscription_id=sub_pk, resource_group=resource_group, vm_name=vm_name
                ).delete(synchronize_session=False)
                task = db.session.get(DeploymentTask, task_id)
                if task:
                    task.status = "Succeeded"
                    task.progress_msg = f"虚拟机规格已调整为【{confirmed_size}】"
                    task.error_detail = None
                db.session.commit()
            except Exception as error:
                logger.exception("调整虚拟机规格失败：%s", vm_name)
                db.session.rollback()
                vm_cache = VmCache.query.filter_by(
                    subscription_id=sub_pk,
                    resource_group=resource_group,
                    name=vm_name,
                ).first()
                confirmed_size = getattr(error, "confirmed_size", None)
                confirmed_power_state = getattr(error, "confirmed_power_state", None)
                if vm_cache:
                    if confirmed_size:
                        vm_cache.vm_size = confirmed_size
                    if confirmed_power_state and confirmed_power_state.lower() != "unknown":
                        vm_cache.power_state = confirmed_power_state
                    if confirmed_size or (
                        confirmed_power_state and confirmed_power_state.lower() != "unknown"
                    ):
                        vm_cache.updated_at = datetime.utcnow()

                AzureService._vm_cache.pop(f"vms_{subscription_azure_id}", None)
                AzureService.invalidate_vm_resize_options_cache(
                    tenant_id, client_id, client_secret, subscription_azure_id,
                    resource_group, vm_name
                )
                VmResizeOptionsCache.query.filter_by(
                    subscription_id=sub_pk, resource_group=resource_group, vm_name=vm_name
                ).delete(synchronize_session=False)
                task = db.session.get(DeploymentTask, task_id)
                if task:
                    task.status = "Failed"
                    task.progress_msg = "调整虚拟机规格失败"
                    task.error_detail = public_error_message("调整虚拟机规格")
                db.session.commit()

    submit_background_task(async_resize, task_id=task_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({
            "status": "submitted",
            "task_id": task_id,
            "target_name": vm_name,
            "task_type": "resize",
        })

    flash(f"已提交虚拟机规格调整任务，可在详情页或【活动日志】中查看进度！", "info")
    return redirect(url_for(
        "vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name
    ))

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/action/<action_type>", methods=["POST"])
@login_required
def vm_action(sub_id, resource_group, vm_name, action_type):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    sub_pk = subscription.id
    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    sub_azure_id = subscription.subscription_id

    action_map = {
        "start": ("开机", AzureService.start_vm),
        "stop": ("关机释放", AzureService.stop_vm),
        "restart": ("重启", AzureService.restart_vm),
        "delete": ("销毁删除", AzureService.delete_vm_and_resources)
    }

    if action_type not in action_map:
        flash("未知操作指令", "danger")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    action_name, action_func = action_map[action_type]

    reservation = reserve_vm_operation_task(
        sub_pk, resource_group, vm_name, action_type,
        f"正在向 Azure 下发【{action_name}】指令..."
    )
    task_id = reservation["task_id"]
    if not reservation["created"]:
        message = f"虚拟机当前已有【{reservation['progress_msg']}】，已继续跟踪原任务。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "in_progress",
                "task_id": task_id,
                "target_name": vm_name,
                "task_type": reservation["task_type"],
                "message": message,
            })
        flash(message, "warning")
        if action_type == "delete":
            return redirect(url_for("vms_page", sub_id=sub_id))
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    def async_do():
        with app.app_context():
            try:
                def update_action_progress(message):
                    try:
                        task = db.session.get(DeploymentTask, task_id)
                        if task and task.status in ("Pending", "InProgress"):
                            task.progress_msg = message
                            db.session.commit()
                    except Exception:
                        db.session.rollback()
                        logger.exception("写入虚拟机操作进度失败：task_id=%s", task_id)

                action_func(
                    tenant_id, client_id, client_secret, sub_azure_id,
                    resource_group, vm_name,
                    progress_callback=update_action_progress,
                )
                # 只有当 Azure 彻底执行成功后，才更新/删除本地 VmCache 和内存缓存
                if action_type == "delete":
                    VmCache.query.filter_by(subscription_id=sub_pk, name=vm_name).delete()
                else:
                    rec = VmCache.query.filter_by(subscription_id=sub_pk, name=vm_name).first()
                    if rec:
                        if action_type == "start":
                            rec.power_state = "Running"
                        elif action_type == "stop":
                            rec.power_state = "Deallocated"
                        elif action_type == "restart":
                            rec.power_state = "Running"
                        rec.updated_at = datetime.utcnow()
                # 清除内存缓存以强制刷新
                AzureService._vm_cache.pop(f"vms_{sub_pk}", None)

                t = db.session.get(DeploymentTask, task_id)
                if t:
                    t.status = "Succeeded"
                    t.progress_msg = f"虚拟机【{action_name}】已执行完成！"
                db.session.commit()
            except Exception as e:
                logger.exception("虚拟机操作失败：%s/%s", action_type, vm_name)
                err_msg = public_error_message("虚拟机操作")
                try:
                    db.session.rollback()
                    t = db.session.get(DeploymentTask, task_id)
                    if t:
                        t.status = "Failed"
                        t.progress_msg = f"虚拟机【{action_name}】失败"
                        t.error_detail = err_msg
                    db.session.commit()
                except Exception:
                    pass

    submit_background_task(async_do, task_id=task_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "submitted", "task_id": task_id, "target_name": vm_name, "task_type": action_type})

    flash(f"已下发虚拟机【{action_name}】任务，实时进度可在页面看板或【活动日志】中查看！", "info")

    if action_type == "delete":
        return redirect(url_for("vms_page", sub_id=sub_id))
    return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

# ========================== 更换公网 IP ==========================

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/change_ip", methods=["POST"])
@login_required
def vm_change_ip(sub_id, resource_group, vm_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    sub_pk = subscription.id
    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    sub_azure_id = subscription.subscription_id

    reservation = reserve_vm_operation_task(
        sub_pk, resource_group, vm_name, "change_ip",
        "1/3 正在准备更换公网 IP..."
    )
    task_id = reservation["task_id"]
    if not reservation["created"]:
        message = f"虚拟机当前已有【{reservation['progress_msg']}】，已继续跟踪原任务。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "in_progress",
                "task_id": task_id,
                "target_name": vm_name,
                "task_type": reservation["task_type"],
                "message": message,
            })
        flash(message, "warning")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    def async_change():
        with app.app_context():
            def cb(msg):
                try:
                    t = db.session.get(DeploymentTask, task_id)
                    if t:
                        t.progress_msg = msg
                        db.session.commit()
                except Exception:
                    pass

            try:
                new_ip = AzureService.change_public_ip(
                    tenant_id, client_id, client_secret,
                    sub_azure_id, resource_group, vm_name,
                    progress_callback=cb
                )
                # 即刻同步更新本地 VmCache 中的新公网 IP
                rec = VmCache.query.filter_by(subscription_id=sub_pk, name=vm_name).first()
                if rec:
                    rec.public_ip = new_ip
                    rec.updated_at = datetime.utcnow()
                AzureService._vm_cache.pop(f"vms_{sub_pk}", None)
                db.session.commit()

                t = db.session.get(DeploymentTask, task_id)
                if t:
                    t.status = "Succeeded"
                    t.progress_msg = f"公网 IP 已更换成功！新 IP: {new_ip}"
                    db.session.commit()
            except Exception as e:
                logger.exception("更换公网 IP 失败：%s", vm_name)
                err_msg = public_error_message("更换公网 IP")
                t = db.session.get(DeploymentTask, task_id)
                if t:
                    t.status = "Failed"
                    t.progress_msg = "更换公网 IP 失败"
                    t.error_detail = err_msg
                    db.session.commit()

    submit_background_task(async_change, task_id=task_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "submitted", "task_id": task_id, "target_name": vm_name, "task_type": "change_ip"})

    flash(f"已触发更换公网 IP 任务，可在详情页或【活动日志】中实时查看进度！", "info")
    return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

# ========================== 重装系统 (事务流程) ==========================

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/reinstall", methods=["POST"])
@login_required
def vm_reinstall(sub_id, resource_group, vm_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    image_urn = request.form.get("image", "").strip()
    custom_image_urn = request.form.get("custom_image_urn", "").strip()
    admin_username = request.form.get("admin_username", "azureuser").strip()
    admin_password = request.form.get("admin_password", "").strip()
    ssh_public_key = request.form.get("ssh_public_key", "").strip()
    disk_size_raw = request.form.get("disk_size_gb", 30)

    # 安全读取 CustomData：优先从选择的 script_id 加密模板解密注入，绝不在前端暴露明文
    script_id = request.form.get("script_id", type=int)
    custom_data = request.form.get("custom_data", "")
    if script_id:
        tpl = db.session.get(ScriptTemplate, script_id)
        if tpl and tpl.content:
            custom_data = tpl.content

    try:
        disk_size_gb = validate_reinstall_request(
            vm_name, image_urn, custom_image_urn, admin_username,
            admin_password, ssh_public_key, disk_size_raw, custom_data
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    chosen_image = custom_image_urn if custom_image_urn else image_urn

    sub_pk = subscription.id
    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    subscription_azure_id = subscription.subscription_id

    def async_reinstall(task_id):
        logger.info(f"Starting OS reinstall transaction for {vm_name}")
        with app.app_context():
            def update_reinstall_progress(msg):
                try:
                    t = db.session.get(DeploymentTask, task_id)
                    if t:
                        t.status = "InProgress"
                        t.progress_msg = msg
                        db.session.commit()
                except Exception:
                    pass

            try:
                selected_image_architecture = None
                selected_image_os_type = None
                selected_image_ref = None
                if not custom_image_urn:
                    vm_cache = VmCache.query.filter_by(subscription_id=sub_pk, name=vm_name).first()
                    image_location = (vm_cache.location if vm_cache else "").lower().replace(" ", "")
                    cached_image = ImageCache.query.filter_by(
                        subscription_id=sub_pk, location=image_location, urn=chosen_image
                    ).first()
                    if not cached_image or not cached_image.urn:
                        raise Exception("所选动态镜像不是有效的真实 URN，或不在当前地域镜像缓存中，请重新打开重装窗口探测")
                    selected_image_architecture = cached_image.architecture
                    selected_image_os_type = cached_image.name or cached_image.os_type or "Linux"
                    selected_image_ref = cached_image.urn
                else:
                    selected_image_ref = custom_image_urn

                update_reinstall_progress("任务已创建，正在启动重装流程...")
                success, msg = AzureService.reinstall_vm_os(
                    tenant_id, client_id, client_secret,
                    subscription_azure_id, resource_group, vm_name,
                    chosen_image, admin_username,
                    admin_password if admin_password else None,
                    ssh_public_key if ssh_public_key else None,
                    disk_size_gb, custom_data if custom_data else None,
                    progress_callback=update_reinstall_progress,
                    image_architecture=selected_image_architecture
                )
            except Exception as e:
                success = False
                logger.exception("重装系统失败：%s", vm_name)
                msg = public_error_message("重装系统")

            try:
                t = db.session.get(DeploymentTask, task_id)
                if t:
                    if success:
                        t.status = "Succeeded"
                        t.progress_msg = "重装成功，新系统已就绪并开机。"
                        if msg and "旧系统盘清理失败" in msg:
                            t.progress_msg = "重装成功，但旧系统盘清理失败，请检查 Azure 资源。"
                        rec = VmCache.query.filter_by(subscription_id=sub_pk, name=vm_name).first()
                        if rec:
                            rec.auth_mode = "ssh_key" if ssh_public_key else "password"
                            rec.updated_at = datetime.utcnow()
                            rec.os_type = selected_image_os_type or PRESET_IMAGES.get(chosen_image, {}).get("name", "Linux")
                            rec.image_ref = selected_image_ref or rec.image_ref
                        AzureService._vm_cache.pop(f"vms_{sub_pk}", None)
                    else:
                        t.status = "Failed"
                        t.progress_msg = "重装失败，请查看活动日志或稍后重试。"
                        if msg and "回滚" in msg:
                            t.progress_msg = "重装失败，系统已尝试回滚，请核查虚拟机状态。"
                        t.error_detail = public_error_message("重装系统")
                    db.session.commit()
            except Exception:
                pass

    reservation = reserve_vm_operation_task(
        sub_pk, resource_group, vm_name, "reinstall", "已提交换盘重装任务..."
    )
    if not reservation["created"]:
        message = f"虚拟机当前已有【{reservation['progress_msg']}】，已继续跟踪原任务。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "in_progress",
                "task_id": reservation["task_id"],
                "target_name": vm_name,
                "task_type": reservation["task_type"],
                "message": message,
            })
        flash(message, "warning")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    submit_background_task(async_reinstall, reservation["task_id"], task_id=reservation["task_id"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "submitted", "task_id": reservation["task_id"], "target_name": vm_name, "task_type": "reinstall"})

    flash(f"已触发虚拟机 {vm_name} 系统重装任务，可在【活动日志】中查看实时进度！", "info")
    return redirect(url_for("vms_page", sub_id=sub_id))

# ========================== 重置管理员凭据 ==========================

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/reset_credentials", methods=["POST"])
@login_required
def vm_reset_credentials(sub_id, resource_group, vm_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account

    os_type = request.form.get("os_type", "Linux")
    auth_mode = request.form.get("auth_mode", "password")
    username = request.form.get("username", "").strip()
    credential_input = request.form.get("credential_input", "").strip()

    if not credential_input:
        flash("请输入要重置的新凭据", "danger")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    # 强校验：通过缓存与 Azure 探测判断 VM 是否处于运行中状态 (非 running 状态下 VMAccess 扩展必失败)
    vm_cache = VmCache.query.filter_by(subscription_id=sub_id, resource_group=resource_group, name=vm_name).first()
    if vm_cache and "running" not in (vm_cache.power_state or "").lower():
        err_msg = "虚拟机当前未处于运行中状态，Azure VMAccess 扩展无法生效，请先开机后再重置凭据！"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"status": "error", "message": err_msg}), 400
        flash(err_msg, "danger")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    password = None
    ssh_key = None
    if auth_mode == "ssh_key" or credential_input.startswith("ssh-") or credential_input.startswith("ecdsa-"):
        ssh_key = credential_input
    else:
        password = credential_input

    sub_pk = subscription.id
    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    subscription_azure_id = subscription.subscription_id

    reservation = reserve_vm_operation_task(
        sub_pk, resource_group, vm_name, "reset_credentials",
        f"正在通过 Azure VMAccess 扩展在线重置用户【{username}】的凭据..."
    )
    task_id = reservation["task_id"]
    if not reservation["created"]:
        message = f"虚拟机当前已有【{reservation['progress_msg']}】，已继续跟踪原任务。"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "status": "in_progress",
                "task_id": task_id,
                "target_name": vm_name,
                "task_type": reservation["task_type"],
                "message": message,
            })
        flash(message, "warning")
        return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

    def async_reset():
        with app.app_context():
            try:
                AzureService.reset_credentials(
                    tenant_id, client_id, client_secret,
                    subscription_azure_id, resource_group, vm_name,
                    os_type, username, password, ssh_key
                )
                t = db.session.get(DeploymentTask, task_id)
                if t:
                    t.status = "Succeeded"
                    t.progress_msg = f"用户【{username}】的凭据已在线重置成功！"
                    db.session.commit()
            except Exception as e:
                logger.exception("重置凭据失败：%s", vm_name)
                err_msg = public_error_message("重置凭据")
                t = db.session.get(DeploymentTask, task_id)
                if t:
                    t.status = "Failed"
                    t.progress_msg = "重置凭据失败"
                    t.error_detail = err_msg
                    db.session.commit()

    submit_background_task(async_reset, task_id=task_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"status": "submitted", "task_id": task_id, "target_name": vm_name, "task_type": "reset_credentials"})

    flash(f"已触发重置凭据任务，可在页面任务进度栏中查看！", "info")
    return redirect(url_for("vm_detail", sub_id=sub_id, resource_group=resource_group, vm_name=vm_name))

# ========================== 防火墙 (NSG) 规则管理 ==========================

@app.route("/sub/<int:sub_id>/vm/<resource_group>/<nsg_name>/firewall", methods=["GET", "POST"])
@login_required
def vm_firewall(sub_id, resource_group, nsg_name):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    vm_name = request.args.get("vm_name", "").strip()

    sub_pk = subscription.id
    tenant_id = account.tenant_id
    client_id = account.client_id
    client_secret = account.client_secret
    subscription_azure_id = subscription.subscription_id

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_rule":
            rule_name = request.form.get("rule_name", "").strip()
            priority = request.form.get("priority", 1000)
            protocol = request.form.get("protocol", "Tcp")
            port_range = request.form.get("port_range", "").strip()
            source_cidr = request.form.get("source_cidr", "*").strip()
            access = request.form.get("access", "Allow")
            direction = request.form.get("direction", "Inbound")
            try:
                priority = validate_firewall_rule(
                    rule_name, priority, protocol, port_range,
                    source_cidr, access, direction
                )
            except ValueError as exc:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                    return jsonify({"status": "error", "message": str(exc)}), 400
                flash(str(exc), "danger")
                return redirect(url_for("vm_firewall", sub_id=sub_id, resource_group=resource_group, nsg_name=nsg_name, vm_name=vm_name))

            reservation = reserve_unique_task(
                sub_pk, resource_group, nsg_name,
                task_type="firewall_add",
                progress_msg=f"已提交添加防火墙规则【{rule_name}】任务...",
                rule_name=rule_name,
            )
            task_id = reservation["task_id"]
            if not reservation["created"]:
                return jsonify({"status": "in_progress", "task_id": task_id,
                                "task_type": "firewall_add", "target_name": nsg_name,
                                "rule_name": reservation.get("rule_name"),
                                "message": "该安全组已有添加规则任务正在执行。"})

            def async_firewall_add():
                with app.app_context():
                    try:
                        task = db.session.get(DeploymentTask, task_id)
                        if task:
                            task.status = "InProgress"
                            task.progress_msg = f"正在向 Azure 写入防火墙规则【{rule_name}】..."
                            db.session.commit()
                        AzureService.add_nsg_rule(
                            tenant_id, client_id, client_secret,
                            subscription_azure_id, resource_group, nsg_name,
                            rule_name, priority, protocol, port_range, source_cidr, access, direction
                        )
                        task = db.session.get(DeploymentTask, task_id)
                        if task:
                            task.status = "Succeeded"
                            task.progress_msg = f"防火墙安全规则【{rule_name}】已成功写入安全组【{nsg_name}】！"
                        db.session.commit()
                    except Exception as e:
                        logger.exception("添加防火墙规则失败")
                        err_msg = public_error_message("添加防火墙规则")
                        try:
                            db.session.rollback()
                            task = db.session.get(DeploymentTask, task_id)
                            if task:
                                task.status = "Failed"
                                task.progress_msg = f"添加防火墙安全规则【{rule_name}】失败"
                                task.error_detail = err_msg
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                    finally:
                        db.session.remove()

            submit_background_task(async_firewall_add, task_id=task_id)
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                return jsonify({"status": "submitted", "task_id": task_id, "task_type": "firewall_add", "target_name": nsg_name, "rule_name": rule_name, "nsg_name": nsg_name})
            flash(f"已提交添加防火墙安全规则【{rule_name}】任务！", "info")
            return redirect(url_for("vm_firewall", sub_id=sub_id, resource_group=resource_group, nsg_name=nsg_name))

        elif action == "delete_rule":
            rule_name = request.form.get("rule_name", "").strip()
            if not rule_name or not FIREWALL_RULE_NAME_PATTERN.fullmatch(rule_name):
                message = "防火墙规则名称格式无效"
                if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                    return jsonify({"status": "error", "message": message}), 400
                flash(message, "danger")
                return redirect(url_for("vm_firewall", sub_id=sub_id, resource_group=resource_group, nsg_name=nsg_name, vm_name=vm_name))
            reservation = reserve_unique_task(
                sub_pk, resource_group, nsg_name,
                task_type="firewall_delete",
                progress_msg=f"已提交删除防火墙规则【{rule_name}】任务...",
                rule_name=rule_name,
            )
            task_id = reservation["task_id"]
            if not reservation["created"]:
                return jsonify({"status": "in_progress", "task_id": task_id,
                                "task_type": "firewall_delete", "target_name": nsg_name,
                                "rule_name": reservation.get("rule_name"),
                                "message": "该安全组已有删除规则任务正在执行。"})

            def async_firewall_delete():
                with app.app_context():
                    try:
                        task = db.session.get(DeploymentTask, task_id)
                        if task:
                            task.status = "InProgress"
                            task.progress_msg = f"正在从 Azure 删除防火墙规则【{rule_name}】..."
                            db.session.commit()
                        AzureService.delete_nsg_rule(
                            tenant_id, client_id, client_secret,
                            subscription_azure_id, resource_group, nsg_name, rule_name
                        )
                        task = db.session.get(DeploymentTask, task_id)
                        if task:
                            task.status = "Succeeded"
                            task.progress_msg = f"防火墙安全规则【{rule_name}】已成功从安全组【{nsg_name}】中移除！"
                        db.session.commit()
                    except Exception as e:
                        logger.exception("删除防火墙规则失败")
                        err_msg = public_error_message("删除防火墙规则")
                        try:
                            db.session.rollback()
                            task = db.session.get(DeploymentTask, task_id)
                            if task:
                                task.status = "Failed"
                                task.progress_msg = f"删除防火墙安全规则【{rule_name}】失败"
                                task.error_detail = err_msg
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                    finally:
                        db.session.remove()

            submit_background_task(async_firewall_delete, task_id=task_id)
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                return jsonify({"status": "submitted", "task_id": task_id, "task_type": "firewall_delete", "target_name": nsg_name, "rule_name": rule_name, "nsg_name": nsg_name})
            flash(f"已提交删除防火墙安全规则【{rule_name}】任务！", "info")
            return redirect(url_for("vm_firewall", sub_id=sub_id, resource_group=resource_group, nsg_name=nsg_name))

    rules = []
    error_msg = None
    try:
        rules = AzureService.get_nsg_rules(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, resource_group, nsg_name
        )
    except Exception as e:
        logger.exception("读取防火墙规则失败：%s", nsg_name)
        error_msg = public_error_message("读取防火墙规则")

    return render_template(
        "firewall.html",
        active_module="vms",
        subscription=subscription,
        account=account,
        resource_group=resource_group,
        nsg_name=nsg_name,
        vm_name=vm_name,
        rules=rules,
        error_msg=error_msg
    )

# ========================== RESTful API 接口 (规格缓存 & 监控图表) ==========================

@app.route("/api/sub/<int:sub_id>/locations")
@login_required
def api_get_locations(sub_id):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    cache_expiry = datetime.utcnow() - timedelta(hours=24)
    cached = LocationCache.query.filter(
        LocationCache.subscription_id == subscription.id,
        LocationCache.updated_at >= cache_expiry
    ).order_by(LocationCache.id.asc()).all()

    if cached:
        return jsonify({
            "status": "success",
            "source": "cache",
            "locations": [{
                "name": location.name,
                "display_name": location.display_name
            } for location in cached]
        })

    try:
        fresh_locations = AzureService.list_locations(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id
        )
        LocationCache.query.filter_by(subscription_id=subscription.id).delete(synchronize_session=False)
        for item in fresh_locations:
            db.session.add(LocationCache(
                subscription_id=subscription.id,
                name=item["name"],
                display_name=item["display_name"]
            ))
        db.session.commit()
        return jsonify({
            "status": "success",
            "source": "fresh",
            "locations": fresh_locations
        })
    except Exception:
        logger.exception("Azure 缓存接口请求失败")
        db.session.rollback()
        return jsonify({"status": "error", "message": public_error_message("Azure 缓存请求")}), 500

@app.route("/api/sub/<int:sub_id>/skus/<location>")
@login_required
def api_get_skus(sub_id, location):
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    loc = location.lower().replace(" ", "")
    os_type = "Windows" if request.args.get("os_type", "Linux").lower() == "windows" else "Linux"
    billing_mode = "spot" if request.args.get("billing_mode", "on_demand").lower() == "spot" else "on_demand"
    currency = request.args.get("currency", "USD")

    cached = SkuCache.query.filter(
        SkuCache.subscription_id == subscription.id,
        SkuCache.location == loc,
        SkuCache.updated_at >= datetime.utcnow() - timedelta(hours=24)
    ).all()

    if cached:
        if not _sku_cache_has_selectable_sku(cached):
            return jsonify({
                "status": "unavailable",
                "availability": "subscription",
                "reason_code": _sku_cache_unavailable_reason(cached),
                "message": "当前订阅在该地域没有可用虚拟机规格",
                "billing_mode": billing_mode,
                "os_type": os_type,
                "skus": [],
            })
        return jsonify({
            "status": "cached",
            "billing_mode": billing_mode,
            "os_type": os_type,
            "skus": [_sku_cache_payload(item) for item in cached],
        })

    try:
        fresh_skus = AzureService.fetch_and_cache_skus(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, loc
        )

        SkuCache.query.filter_by(subscription_id=subscription.id, location=loc).delete()
        for item in fresh_skus:
            _save_sku_cache_entry(subscription.id, loc, item)
        db.session.commit()

        if not fresh_skus:
            return jsonify({
                "status": "unavailable",
                "availability": "subscription",
                "reason_code": "NoVirtualMachineSku",
                "message": sku_location_unavailable_message(loc),
                "billing_mode": billing_mode,
                "os_type": os_type,
                "skus": [],
            })

        cached_skus = SkuCache.query.filter_by(
            subscription_id=subscription.id,
            location=loc,
        ).all()
        if not _sku_cache_has_selectable_sku(cached_skus):
            return jsonify({
                "status": "unavailable",
                "availability": "subscription",
                "reason_code": _sku_cache_unavailable_reason(cached_skus),
                "message": "当前订阅在该地域没有可用虚拟机规格",
                "billing_mode": billing_mode,
                "os_type": os_type,
                "skus": [],
            })
        return jsonify({
            "status": "fresh", "billing_mode": billing_mode, "os_type": os_type,
            "skus": [_sku_cache_payload(item) for item in cached_skus],
        })
    except Exception as error:
        logger.exception("规格缓存接口请求失败")
        if _is_sku_location_unavailable_error(error):
            return jsonify({
                "status": "unavailable",
                "availability": "subscription",
                "reason_code": _azure_error_code(error) or "LocationUnavailable",
                "message": sku_location_unavailable_message(loc),
                "billing_mode": billing_mode,
                "os_type": os_type,
                "skus": [],
            })
        return jsonify({"status": "error", "message": public_error_message("规格缓存请求")}), 500


@app.route("/api/sub/<int:sub_id>/sku_prices/<location>")
@login_required
def api_get_sku_prices(sub_id, location):
    """只刷新价格字段，避免切换计费模式时重新传输和渲染规格目录。"""
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    loc = location.lower().replace(" ", "")
    os_type = "Windows" if request.args.get("os_type", "Linux").lower() == "windows" else "Linux"
    billing_mode = "spot" if request.args.get("billing_mode", "on_demand").lower() == "spot" else "on_demand"
    currency = request.args.get("currency", "USD")

    cached = SkuCache.query.filter(
        SkuCache.subscription_id == subscription.id,
        SkuCache.location == loc,
        SkuCache.updated_at >= datetime.utcnow() - timedelta(hours=24),
    ).all()
    if not cached:
        # 浏览器可能仍命中 24 小时会话规格缓存，而服务端规格缓存刚好过期。
        # 价格接口不能因此直接返回空列表，否则前端会把空价格记住到本次会话。
        try:
            fresh_skus = AzureService.fetch_and_cache_skus(
                account.tenant_id, account.client_id, account.client_secret,
                subscription.subscription_id, loc
            )
            SkuCache.query.filter_by(
                subscription_id=subscription.id,
                location=loc,
            ).delete(synchronize_session=False)
            for item in fresh_skus:
                _save_sku_cache_entry(subscription.id, loc, item)
            db.session.commit()
            if not fresh_skus:
                return jsonify({
                    "status": "unavailable",
                    "source": "unavailable",
                    "availability": "subscription",
                    "reason_code": "NoVirtualMachineSku",
                    "message": sku_location_unavailable_message(loc),
                    "os_type": os_type,
                    "billing_mode": billing_mode,
                    "prices": [],
                })
            cached = SkuCache.query.filter_by(
                subscription_id=subscription.id,
                location=loc,
            ).all()
        except Exception as error:
            logger.exception("价格接口补充规格缓存失败：%s", loc)
            db.session.rollback()
            if _is_sku_location_unavailable_error(error):
                return jsonify({
                    "status": "unavailable",
                    "source": "unavailable",
                    "availability": "subscription",
                    "reason_code": _azure_error_code(error) or "LocationUnavailable",
                    "message": sku_location_unavailable_message(loc),
                    "os_type": os_type,
                    "billing_mode": billing_mode,
                    "prices": [],
                })
            return jsonify({
                "status": "success",
                "source": "unavailable",
                "os_type": os_type,
                "billing_mode": billing_mode,
                "prices": [],
            })

    try:
        sku_payloads, price_error = _enrich_sku_prices(
            subscription,
            loc,
            [_sku_cache_payload(item) for item in cached],
            os_type,
            billing_mode,
            currency,
        )
        return jsonify({
            "status": "success",
            "source": "cache",
            "os_type": os_type,
            "billing_mode": billing_mode,
            "price_error": price_error,
            "prices": [{
                "name": item.get("name"),
                "hourly_price": item.get("hourly_price"),
                "monthly_price": item.get("monthly_price"),
                "currency": item.get("currency"),
                "price_updated_at": item.get("price_updated_at"),
            } for item in sku_payloads],
        })
    except Exception:
        logger.exception("价格接口请求失败")
        db.session.rollback()
        return jsonify({"status": "error", "message": public_error_message("价格请求")}), 500

@app.route("/api/sub/<int:sub_id>/images/<location>")
@login_required
def api_get_dynamic_images(sub_id, location):
    """动态拉取并缓存指定地域下 Azure 官方当前真实可用的镜像列表"""
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    loc = location.lower().replace(" ", "")
    force_refresh = (request.args.get("refresh") == "1")
    requested_architecture = request.args.get("architecture", "").strip().lower()
    if requested_architecture not in ("", "x64", "arm64"):
        return jsonify({"status": "error", "message": "不支持的镜像架构参数"}), 400
    cache_expiry = datetime.utcnow() - timedelta(hours=24)
    cached = ImageCache.query.filter_by(subscription_id=subscription.id, location=loc).order_by(ImageCache.id.asc()).all()
    cached_architectures = {image.architecture for image in cached if image.urn}
    has_requested_architecture = (requested_architecture in cached_architectures if requested_architecture else bool(cached_architectures & {"x64", "arm64"}))

    if not force_refresh and cached and has_requested_architecture:
        images = {"location": loc, "x64": [], "arm64": []}
        for image in cached:
            if image.architecture in images and image.urn:
                images[image.architecture].append({"name": image.name, "os_type": image.os_type, "urn": image.urn})
        relevant_images = [image for image in cached if not requested_architecture or image.architecture == requested_architecture]
        is_stale = any(not image.updated_at or image.updated_at < cache_expiry for image in relevant_images)
        refreshing = False
        if is_stale:
            refreshing = _schedule_image_cache_refresh(subscription.id, account.tenant_id, account.client_id, account.client_secret, subscription.subscription_id, loc)
        latest_updated = max((image.updated_at for image in relevant_images if image.updated_at), default=None)
        images["updated_at"] = latest_updated.timestamp() if latest_updated else None
        return jsonify({"status": "success", "source": "cache", "refreshing": refreshing, "images": images})

    try:
        data = AzureService.fetch_dynamic_images(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, loc, force_refresh=True
        )
        ImageCache.query.filter_by(subscription_id=subscription.id, location=loc).delete(synchronize_session=False)
        for architecture in ("x64", "arm64"):
            for image in data.get(architecture, []):
                db.session.add(ImageCache(
                    subscription_id=subscription.id,
                    location=loc,
                    architecture=architecture,
                    image_key=image["key"],
                    name=image["name"],
                    os_type=image["os_type"],
                    urn=image.get("urn", "")
                ))
        db.session.commit()
        public_images = {"location": loc, "x64": [], "arm64": []}
        for architecture in ("x64", "arm64"):
            public_images[architecture] = [
                {"name": image["name"], "os_type": image["os_type"], "urn": image["urn"]}
                for image in data.get(architecture, []) if image.get("urn")
            ]
        return jsonify({"status": "success", "source": "fresh", "images": public_images})
    except Exception:
        logger.exception("Azure 缓存接口请求失败")
        db.session.rollback()
        return jsonify({"status": "error", "message": public_error_message("Azure 缓存请求")}), 500

@app.route("/api/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/resize_options")
def api_vm_resize_options(sub_id, resource_group, vm_name):
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    try:
        resize_cache = VmResizeOptionsCache.query.filter_by(
            subscription_id=subscription.id,
            resource_group=resource_group,
            vm_name=vm_name,
        ).first()
        cache_cutoff = datetime.utcnow() - timedelta(seconds=60)
        if resize_cache and resize_cache.updated_at and resize_cache.updated_at >= cache_cutoff:
            options = json.loads(resize_cache.payload or "{}")
            options["source"] = "database"
            options["cached_at"] = resize_cache.updated_at.replace(tzinfo=timezone.utc).timestamp()
        else:
            options = AzureService.list_vm_resize_options(
                account.tenant_id, account.client_id, account.client_secret,
                subscription.subscription_id, resource_group, vm_name,
                use_cache=True,
                resource_sku_provider=lambda location: _load_sku_catalog_for_resize(
                    subscription, account, location
                ),
            )
            if resize_cache is None:
                resize_cache = VmResizeOptionsCache(
                    subscription_id=subscription.id,
                    resource_group=resource_group,
                    vm_name=vm_name,
                )
                db.session.add(resize_cache)
            resize_cache.payload = json.dumps(options, ensure_ascii=False)
            resize_cache.updated_at = datetime.utcnow()
            db.session.commit()

        # 规格目录与价格是独立缓存维度。无论命中哪一层规格缓存，
        # 都必须从价格缓存重新关联，避免把旧的错误价格随规格缓存返回。
        if options.get("location") and options.get("sizes"):
            priced_sizes, price_error = _enrich_sku_prices(
                subscription,
                options["location"],
                options["sizes"],
                options.get("current_os_type", "Linux"),
                options.get("current_billing_mode", "on_demand"),
                request.args.get("currency", "USD"),
            )
            options["sizes"] = priced_sizes
            options["price_error"] = price_error
        return jsonify({
            "status": "success",
            "source": options.get("source"),
            "cached_at": options.get("cached_at"),
            "current_size": options.get("current_size"),
            "power_state": options.get("power_state"),
            "supported": options.get("supported", False),
            "unsupported_reason": options.get("unsupported_reason"),
            "current_architecture": options.get("current_architecture"),
            "current_nic_accelerated_networking": options.get("current_nic_accelerated_networking"),
            "current_os_type": options.get("current_os_type"),
            "current_billing_mode": options.get("current_billing_mode"),
            "price_error": options.get("price_error"),
            "sizes": options.get("sizes", []),
        })
    except Exception:
        logger.exception("读取虚拟机可调整规格失败：%s", vm_name)
        return jsonify({
            "status": "error",
            "message": public_error_message("读取虚拟机可调整规格"),
        }), 500


@app.route("/api/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/reinstall_metadata")
@login_required
def api_reinstall_metadata(sub_id, resource_group, vm_name):
    """读取 Azure 当前 VM 规格，避免重装弹窗使用过期的本地缓存架构。"""
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    vm_cache = VmCache.query.filter_by(subscription_id=sub_id, name=vm_name).first()
    if vm_cache and vm_cache.location and vm_cache.vm_size:
        return jsonify({
            "status": "success",
            "source": "cache",
            "vm_size": vm_cache.vm_size,
            "location": vm_cache.location,
            "architecture": "arm64" if AzureService.is_arm_vm_size(vm_cache.vm_size) else "x64",
            "cached_at": vm_cache.updated_at.timestamp() if vm_cache.updated_at else None
        })

    try:
        vm = AzureService.get_vm_detail(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, resource_group, vm_name
        )
        vm_size = vm.get("vm_size", "")
        return jsonify({
            "status": "success",
            "vm_size": vm_size,
            "location": vm.get("location", ""),
            "architecture": "arm64" if AzureService.is_arm_vm_size(vm_size) else "x64"
        })
    except Exception:
        logger.exception("读取当前虚拟机规格失败：%s", vm_name)
        return jsonify({"status": "error", "message": public_error_message("读取当前虚拟机规格")}), 500

@app.route("/api/sub/<int:sub_id>/vm/<resource_group>/<vm_name>/metrics")
def api_vm_metrics(sub_id, resource_group, vm_name):
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    subscription = Subscription.query.get_or_404(sub_id)
    account = subscription.account
    try:
        hours = parse_bounded_int(request.args.get("hours", 1), "监控时间范围", 1, 168)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    try:
        data = AzureService.get_vm_metrics(
            account.tenant_id, account.client_id, account.client_secret,
            subscription.subscription_id, resource_group, vm_name, hours=hours
        )
        return jsonify({"status": "success", "data": data})
    except Exception:
        logger.exception("读取虚拟机监控指标失败：%s", vm_name)
        return jsonify({"status": "error", "message": public_error_message("读取虚拟机监控指标")}), 500

with app.app_context():
    initialize_database_schema()
    recover_interrupted_tasks()
    threading.Thread(target=task_watchdog_loop, name="task-watchdog", daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8888))
    app.run(host="0.0.0.0", port=port, debug=False)
