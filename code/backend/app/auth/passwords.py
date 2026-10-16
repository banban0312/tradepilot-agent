"""使用 Argon2id 安全哈希密码。"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
# 固定的虚拟账号哈希用于让“账号不存在”和“密码错误”都执行 Argon2 校验。
_DUMMY_HASH = password_hasher.hash("tradepilot-invalid-demo-password")


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        password_hasher.verify(encoded or _DUMMY_HASH, password)
        return encoded is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
