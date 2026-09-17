import os
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken

db = SQLAlchemy()


def get_cipher():
    """使用应用唯一的 SECRET_KEY 派生数据库字段加密密钥。"""
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise RuntimeError("SECRET_KEY environment variable is required")
    digest = hashlib.sha256(b"azure-manager/database-encryption/v1:" + secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)

def encrypt_secret(plain_text: str) -> str:
    if not plain_text:
        return ''
    cipher = get_cipher()
    return cipher.encrypt(plain_text.encode('utf-8')).decode('utf-8')

def decrypt_secret(cipher_text: str) -> str:
    if not cipher_text:
        return ''
    try:
        cipher = get_cipher()
        return cipher.decrypt(cipher_text.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError("stored secret cannot be decrypted with SECRET_KEY") from exc

class SystemSetting(db.Model):
    __tablename__ = 'system_settings'

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(256), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def get(cls, key: str, default: str = '') -> str:
        rec = cls.query.filter_by(key=key).first()
        return rec.value if rec else default

    @classmethod
    def set(cls, key: str, value: str):
        rec = cls.query.filter_by(key=key).first()
        if not rec:
            rec = cls(key=key, value=value)
            db.session.add(rec)
        else:
            rec.value = value
            rec.updated_at = datetime.utcnow()
        db.session.commit()

class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def is_locked(self) -> bool:
        if self.locked_until and datetime.utcnow() < self.locked_until:
            return True
        return False

    def register_failed_attempt(self):
        self.failed_attempts += 1
        if self.failed_attempts >= 5:
            # 锁定 15 分钟
            self.locked_until = datetime.utcnow() + timedelta(minutes=15)
        db.session.commit()

    def reset_failed_attempts(self):
        self.failed_attempts = 0
        self.locked_until = None
        db.session.commit()

class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"

    id = db.Column(db.Integer, primary_key=True)
    source_digest = db.Column(db.String(64), nullable=False, unique=True, index=True)
    username_digest = db.Column(db.String(64), nullable=False)
    failure_count = db.Column(db.Integer, nullable=False, default=0)
    window_started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    blocked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Account(db.Model):
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    tenant_id = db.Column(db.String(64), nullable=False)
    client_id = db.Column(db.String(64), nullable=False)
    encrypted_secret = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 关联该账户下自动发现的订阅
    subscriptions = db.relationship('Subscription', backref='account', cascade='all, delete-orphan', lazy=True)

    @property
    def client_secret(self) -> str:
        return decrypt_secret(self.encrypted_secret)

    @client_secret.setter
    def client_secret(self, value: str):
        self.encrypted_secret = encrypt_secret(value)

class Subscription(db.Model):
    __tablename__ = 'subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    subscription_id = db.Column(db.String(64), nullable=False)
    display_name = db.Column(db.String(128), nullable=False)
    state = db.Column(db.String(32), default='Enabled')
    spending_limit = db.Column(db.String(32), default='None')
    last_synced_at = db.Column(db.DateTime, default=datetime.utcnow)

class ScriptTemplate(db.Model):
    __tablename__ = 'script_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(256), default='')
    encrypted_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def content(self) -> str:
        return decrypt_secret(self.encrypted_content)

    @content.setter
    def content(self, value: str):
        self.encrypted_content = encrypt_secret(value)

class DeploymentTask(db.Model):
    __tablename__ = 'deployment_tasks'

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=True)
    task_type = db.Column(db.String(32), default='create')  # create, reinstall, change_ip, delete
    rule_name = db.Column(db.String(128), nullable=True)
    target_name = db.Column(db.String(128), nullable=False)
    resource_group = db.Column(db.String(128), nullable=False)
    status = db.Column(db.String(32), default='Pending')  # Pending, InProgress, Succeeded, Failed
    progress_msg = db.Column(db.String(256), default='任务已提交，准备执行')
    error_detail = db.Column(db.Text, nullable=True)
    error_code = db.Column(db.String(128), nullable=True)
    error_category = db.Column(db.String(64), nullable=True)
    retryable = db.Column(db.Boolean, nullable=True)
    request_id = db.Column(db.String(256), nullable=True)
    provider_operation_id = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)




class VmCache(db.Model):
    __tablename__ = 'vm_cache'

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    resource_group = db.Column(db.String(128), nullable=False)
    location = db.Column(db.String(64), nullable=False)
    vm_size = db.Column(db.String(64), default='-')
    os_type = db.Column(db.String(64), default='Linux')
    power_state = db.Column(db.String(64), default='Running')
    provisioning_state = db.Column(db.String(64), default='Succeeded')
    private_ip = db.Column(db.String(64), default='-')
    public_ip = db.Column(db.String(64), default='-')
    nic_name = db.Column(db.String(128), default='-')
    nsg_name = db.Column(db.String(128), default='-')
    image_ref = db.Column(db.String(256), default='-')
    admin_username = db.Column(db.String(64), default='azureuser')
    auth_mode = db.Column(db.String(32), default='password')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class CostCache(db.Model):
    __tablename__ = 'cost_cache'

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False, unique=True, index=True)
    total_cost = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(16), default='USD')
    status = db.Column(db.String(32), default='Success')
    message = db.Column(db.String(256), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class SkuCache(db.Model):
    __tablename__ = 'sku_cache'

    id = db.Column(db.Integer, primary_key=True)
    # 规格列表属于具体订阅；旧数据库迁移前允许为空，查询时会忽略旧的无归属记录。
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=True, index=True)
    location = db.Column(db.String(64), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    architecture = db.Column(db.String(16), nullable=True, default='x64')
    family = db.Column(db.String(64), default='')
    vcpus = db.Column(db.Integer, default=1)
    memory_gb = db.Column(db.Float, default=1.0)
    premium_io = db.Column(db.Boolean, default=False)
    accelerated_networking = db.Column(db.Boolean, default=False)
    restrictions = db.Column(db.String(256), default='')
    # 以下字段保存 Azure Resource SKUs 的完整能力与限制信息；旧字段继续保留兼容既有页面。
    tier = db.Column(db.String(32), default='')
    vcpus_available = db.Column(db.Integer, default=1)
    memory_gib = db.Column(db.Float, default=1.0)
    temp_disk_gib = db.Column(db.Float, nullable=True)
    os_disk_gib = db.Column(db.Float, nullable=True)
    max_data_disk_count = db.Column(db.Integer, nullable=True)
    max_nics = db.Column(db.Integer, nullable=True)
    gpu_count = db.Column(db.Integer, default=0)
    rdma_enabled = db.Column(db.Boolean, nullable=True)
    accelerated_networking_supported = db.Column(db.Boolean, default=False)
    accelerated_networking_required = db.Column(db.Boolean, nullable=True)
    spot_capable = db.Column(db.Boolean, default=False)
    ephemeral_os_disk_supported = db.Column(db.Boolean, default=False)
    hyperv_generations_json = db.Column(db.Text, default='[]')
    disk_controller_types_json = db.Column(db.Text, default='[]')
    availability_zones_json = db.Column(db.Text, default='[]')
    capabilities_json = db.Column(db.Text, default='{}')
    restrictions_json = db.Column(db.Text, default='[]')
    restriction_reasons_json = db.Column(db.Text, default='[]')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class VmSkuPriceCache(db.Model):
    """Azure Retail Prices 的独立缓存，避免价格刷新覆盖规格能力数据。"""

    __tablename__ = 'vm_sku_price_cache'
    __table_args__ = (
        db.UniqueConstraint(
            'subscription_id', 'location', 'sku_name', 'os_type',
            'billing_mode', 'currency', 'price_type',
            name='uq_vm_sku_price_cache_dimensions',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=True, index=True)
    location = db.Column(db.String(64), nullable=False, index=True)
    sku_name = db.Column(db.String(128), nullable=False, index=True)
    os_type = db.Column(db.String(32), nullable=False, default='Linux')
    billing_mode = db.Column(db.String(32), nullable=False, default='on_demand')
    currency = db.Column(db.String(16), nullable=False, default='USD')
    price_type = db.Column(db.String(32), nullable=False, default='hourly')
    hourly_price = db.Column(db.Float, nullable=True)
    monthly_price = db.Column(db.Float, nullable=True)
    effective_date = db.Column(db.String(64), default='')
    source = db.Column(db.String(64), default='azure_retail_prices')
    payload_json = db.Column(db.Text, default='{}')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class VmResizeOptionsCache(db.Model):
    """按订阅和 VM 目标持久化 List Available Sizes 的短时结果。"""

    __tablename__ = 'vm_resize_options_cache'
    __table_args__ = (
        db.UniqueConstraint(
            'subscription_id', 'resource_group', 'vm_name',
            name='uq_vm_resize_options_cache_target',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False, index=True)
    resource_group = db.Column(db.String(128), nullable=False)
    vm_name = db.Column(db.String(128), nullable=False)
    payload = db.Column(db.Text, nullable=False, default='{}')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class LocationCache(db.Model):
    __tablename__ = 'location_cache'
    __table_args__ = (
        db.UniqueConstraint('subscription_id', 'name', name='uq_location_cache_subscription_name'),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    display_name = db.Column(db.String(128), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class ImageCache(db.Model):
    __tablename__ = 'image_cache'
    __table_args__ = (
        db.UniqueConstraint('subscription_id', 'location', 'architecture', 'image_key', name='uq_image_cache_subscription_location_arch_key'),
    )

    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False, index=True)
    location = db.Column(db.String(64), nullable=False, index=True)
    architecture = db.Column(db.String(16), nullable=False)
    image_key = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    os_type = db.Column(db.String(32), nullable=False)
    urn = db.Column(db.String(512), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
