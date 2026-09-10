"""Хранение и разбор браузерных cookies для веб-провайдеров.

Секреты лежат только в data/provider_sessions.json, никогда не пишутся в лог.
Поддерживаются JSON (Cookie-Editor/EditThisCookie), Netscape cookies.txt и
обычная строка заголовка Cookie: name=value; name2=value2.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List


class CookieError(ValueError):
    pass


_ALLOWED = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "url"}


def _same_site(value):
    if value is None:
        return None
    v = str(value).strip().lower().replace("_", "-")
    return {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no-restriction": "None",
        "unspecified": "Lax",
    }.get(v)


def _normalize_cookie(raw: dict, default_url: str) -> dict:
    name = str(raw.get("name") or "").strip()
    value = str(raw.get("value") or "")
    if not name:
        raise CookieError("В cookies есть запись без имени")

    out = {"name": name, "value": value}
    domain = str(raw.get("domain") or "").strip()
    path = str(raw.get("path") or "/").strip() or "/"
    if domain:
        out["domain"] = domain
        out["path"] = path
    else:
        out["url"] = default_url

    expires = raw.get("expires", raw.get("expirationDate", raw.get("expiration")))
    try:
        if expires not in (None, "", -1, "-1"):
            out["expires"] = float(expires)
    except (TypeError, ValueError):
        pass

    if "httpOnly" in raw:
        out["httpOnly"] = bool(raw.get("httpOnly"))
    if "secure" in raw:
        out["secure"] = bool(raw.get("secure"))
    same = _same_site(raw.get("sameSite"))
    if same:
        out["sameSite"] = same
    return {k: v for k, v in out.items() if k in _ALLOWED}


def parse_cookies(text: str, default_url: str) -> List[dict]:
    raw = (text or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise CookieError("Пустые cookies")
    if len(raw) > 200_000:
        raise CookieError("Слишком большой набор cookies")

    # JSON export: список либо {"cookies": [...]}
    if raw[:1] in "[{":
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data.get("cookies")
            if not isinstance(data, list):
                raise CookieError("JSON должен быть списком cookies")
            cookies = [_normalize_cookie(item, default_url) for item in data if isinstance(item, dict)]
            if not cookies:
                raise CookieError("В JSON не найдено cookies")
            return cookies
        except json.JSONDecodeError:
            pass

    # Netscape cookies.txt
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")]
    if lines and all("\t" in line for line in lines):
        result = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            item = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
            }
            try:
                if int(expires) > 0:
                    item["expires"] = float(expires)
            except ValueError:
                pass
            result.append(_normalize_cookie(item, default_url))
        if result:
            return result

    # Обычный Cookie header. Split только по ';', значение может содержать '='.
    result = []
    for part in raw.replace("\r", "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip():
            result.append(_normalize_cookie({"name": name.strip(), "value": value.strip()}, default_url))
    if not result:
        raise CookieError("Не удалось распознать cookies. Пришли JSON, cookies.txt или строку Cookie")
    return result


class CookieStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, List[dict]]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def get(self, provider: str) -> List[dict]:
        data = self._read().get(provider, [])
        return data if isinstance(data, list) else []

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def set(self, provider: str, cookies: List[dict]) -> None:
        data = self._read()
        data[provider] = cookies
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self, provider: str) -> None:
        data = self._read()
        data.pop(provider, None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)
