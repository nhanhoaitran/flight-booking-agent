import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    api_key: SecretStr
    base_url: str
    model: str
    temperature: float = Field(default=0, ge=0, le=2)
    timeout: float = Field(default=30, gt=0, le=120)
    max_tokens: int = Field(default=8192, ge=128, le=16384)

    @classmethod
    def load(cls, model_name=None):
        load_dotenv(ROOT / '.env', override=False)
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        model = model_name or os.environ.get('OPENAI_MODEL', '').strip()
        base_url = os.environ.get('OPENAI_BASE_URL', '').strip()
        if not key or key == 'your_api_key_here' or not model or model == 'your-provider-model-id':
            raise ValueError('Điền OPENAI_API_KEY và OPENAI_MODEL trong .env trước khi chạy LLM.')
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('OPENAI_BASE_URL phải là URL API hợp lệ, không chứa khóa hay mật khẩu.')
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('API từ xa phải dùng HTTPS để bảo vệ khóa.')
        try:
            return cls(api_key=key, model=model, base_url=base_url,
                       temperature=float(os.environ.get('LLM_TEMPERATURE', '0')),
                       timeout=float(os.environ.get('LLM_TIMEOUT_SECONDS', '30')),
                       max_tokens=int(os.environ.get('LLM_MAX_TOKENS', '8192')))
        except ValueError:
            raise ValueError('Tham số temperature, timeout hoặc max_tokens không hợp lệ.') from None
