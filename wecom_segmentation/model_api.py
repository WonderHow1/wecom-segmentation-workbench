from dataclasses import dataclass, field
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import ssl
from tempfile import NamedTemporaryFile
from urllib.parse import SplitResult, urlsplit

from .ollama import PROMPT, ExtractionError, Observation, extraction_messages
from .store import InputError


DEFAULT_CONFIG = Path(".local/model-api.env")
CONTEXT_MESSAGES = 6


@dataclass(frozen=True)
class ApiModel:
    url: SplitResult = field(repr=False)
    model: str
    key: str = field(repr=False)

    @classmethod
    def load(cls, path: Path):
        try:
            values = {}
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                name, separator, value = line.partition("=")
                if separator and name.strip() in {"MODEL_API_BASE_URL", "MODEL_API_MODEL", "MODEL_API_KEY"}:
                    values[name.strip()] = value.strip().strip("\"'")
            return cls.validate(values["MODEL_API_BASE_URL"], values["MODEL_API_MODEL"], values["MODEL_API_KEY"])
        except (OSError, KeyError, UnicodeError):
            raise InputError(f"请在 {path} 配置模型 API 地址、模型名和密钥，见 README") from None

    @classmethod
    def validate(cls, base_url, model, key):
        try:
            url = urlsplit(base_url)
            port = url.port
        except ValueError:
            raise InputError("请填写有效的模型 API 地址") from None
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or port == 0 or any(ord(char) < 33 for char in base_url)
                or url.path.rstrip("/").endswith("/chat/completions")):
            raise InputError("MODEL_API_BASE_URL 须为 HTTPS 基础地址，例如服务商提供的 /v1 地址")
        if (not model or any(not char.isprintable() for char in model)
                or not key or any(not 33 <= ord(char) <= 126 for char in key)):
            raise InputError("请检查 MODEL_API_MODEL 和 MODEL_API_KEY")
        return cls(url, model, key)

    @classmethod
    def save(cls, path: Path, base_url, model, key):
        values = (base_url, model, key)
        if any(not isinstance(value, str) or value != value.strip().strip("\"'")
               or len(value.splitlines()) != 1 for value in values):
            raise InputError("请填写模型 API 地址、模型名和密钥，每项只填一行")
        api = cls.validate(*values)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
        try:
            temporary.write_text(f"MODEL_API_BASE_URL={base_url}\nMODEL_API_MODEL={model}\nMODEL_API_KEY={key}\n",
                                 encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return api

    def observe(self, target: dict, context: list[dict]) -> Observation:
        schema = json.dumps(Observation.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        prompt = f"{PROMPT}\n返回一个 JSON 对象，字段和值须符合此结构。\n{schema}"
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": extraction_messages(target, context[-CONTEXT_MESSAGES:], prompt),
        }, ensure_ascii=False).encode("utf-8")
        connection = None
        try:
            tls = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE"))
            connection = HTTPSConnection(self.url.hostname, port=self.url.port, timeout=60, context=tls)
            connection.request("POST", self.url.path.rstrip("/") + "/chat/completions", body=body,
                               headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status in (401, 403):
                raise ExtractionError("模型 API 鉴权失败，请检查密钥和模型权限")
            if response.status == 429:
                raise ExtractionError("模型 API 限流或额度不足，请检查额度后重试")
            if response.status != 200:
                raise ExtractionError(f"模型 API 请求失败（HTTP {response.status}），请检查地址和模型")
            choice = json.load(response)["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ExtractionError("模型 API 未完成提取，请检查模型和请求限制后重试")
            return Observation.model_validate_json(choice["message"]["content"])
        except TimeoutError:
            raise ExtractionError("模型 API 请求超时，请稍后重试") from None
        except (OSError, HTTPException):
            raise ExtractionError("无法连接模型 API，请检查网络和证书") from None
        except (ValueError, IndexError, KeyError, TypeError):
            raise ExtractionError("模型 API 返回了无效的结构化结果") from None
        finally:
            if connection is not None:
                connection.close()
