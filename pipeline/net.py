"""Camada de rede auditavel do pipeline VC.

Contratos de ingestao (Gate 1 da especificacao):
  * Todo fetch registra User-Agent declarado e intervalo >= MIN_INTERVAL_S.
  * SEC EDGAR exige User-Agent com contato e no maximo 10 req/s; usamos 0.2 s (5 req/s).
  * Cada resposta e persistida em data/raw/ (cache por hash da URL) e registrada em
    data/manifest.jsonl com status, bytes, sha256, encoding e timestamp UTC.
  * Offline replay: VC_OFFLINE=1 faz o pipeline ler exclusivamente o cache, tornando
    a execucao reproduzivel sem rede.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MANIFEST_PATH = ROOT / "data" / "manifest.jsonl"

# User-Agent academico declarado, conforme exigido pela SEC EDGAR.
ACADEMIC_UA = "PesquisaAcademicaVC/1.0 (pesquisador@universidade.edu.br)"

# 10 req/s max na SEC => 0.2 s de intervalo garante margem de 2x.
MIN_INTERVAL_S = 0.2
# Fontes governamentais brasileiras: intervalo mais folgado para nao sobrecarregar.
DEFAULT_INTERVAL_S = 0.3

_LAST_CALL: dict[str, float] = {}
_LOCK = threading.Lock()
_SSL_CTX: ssl.SSLContext | None = None


class FetchError(RuntimeError):
    """Falha de rede apos esgotadas as tentativas."""


def _ssl_context() -> ssl.SSLContext:
    """Contexto TLS com verificacao de certificado (bundle certifi quando disponivel)."""
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import certifi  # type: ignore

        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - fallback defensivo
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower()


def _cache_path(url: str) -> Path:
    return RAW_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".bin")


def _throttle(host: str, min_interval: float) -> None:
    with _LOCK:
        last = _LAST_CALL.get(host)
        now = time.monotonic()
        if last is not None:
            wait = min_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
        _LAST_CALL[host] = time.monotonic()


def _append_manifest(entry: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Fetched:
    """Resultado de um fetch, com metadados para o Gate 1."""

    url: str
    status: int
    content: bytes
    from_cache: bool
    encoding: str | None
    fetch_ms: int

    @property
    def text(self) -> str:
        encoding = self.encoding or _guess_encoding(self.content)
        return self.content.decode(encoding, errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def _guess_encoding(raw: bytes, content_type: str | None = None) -> str:
    if content_type and "charset=" in content_type.lower():
        return content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    try:
        raw[:4096].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin1"


def fetch(
    url: str,
    *,
    source: str,
    user_agent: str = ACADEMIC_UA,
    min_interval: float | None = None,
    retries: int = 3,
    timeout: int = 90,
    force: bool = False,
    decompress: bool = True,
    prefer_encoding: str | None = None,
) -> Fetched:
    """Baixa uma URL respeitando UA, rate-limit, retry/backoff e cache auditavel."""
    offline = os.environ.get("VC_OFFLINE") == "1"
    cache = _cache_path(url)
    interval = min_interval if min_interval is not None else (
        0.2 if _host(url).endswith("sec.gov") else DEFAULT_INTERVAL_S
    )

    if cache.exists() and not force:
        raw = cache.read_bytes()
        _append_manifest(
            {
                "ts_utc": _now(),
                "source": source,
                "url": url,
                "status": 200,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "user_agent": user_agent,
                "min_interval_s": interval,
                "cache": True,
                "offline": offline,
                "gate1_ok": True,
            }
        )
        return Fetched(url, 200, raw, True, prefer_encoding, 0)

    if offline:
        raise FetchError(f"VC_OFFLINE=1 e sem cache para {url}")

    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    }
    started = time.monotonic()
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        _throttle(_host(url), interval)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type")
                status = resp.status
            content_encoding = resp.headers.get("Content-Encoding")
            if decompress:
                body = _maybe_decompress(body, content_encoding)
            elapsed = int((time.monotonic() - started) * 1000)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(body)
            _append_manifest(
                {
                    "ts_utc": _now(),
                    "source": source,
                    "url": url,
                    "status": status,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "user_agent": user_agent,
                    "min_interval_s": interval,
                    "cache": False,
                    "attempt": attempt,
                    "elapsed_ms": elapsed,
                    "content_type": ctype,
                    "gate1_ok": status == 200 and bool(user_agent),
                }
            )
            return Fetched(url, status, body, False, prefer_encoding, elapsed)
        except urllib.error.HTTPError as exc:
            last_err = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            _append_manifest(
                {
                    "ts_utc": _now(),
                    "source": source,
                    "url": url,
                    "status": exc.code,
                    "bytes": 0,
                    "attempt": attempt,
                    "user_agent": user_agent,
                    "min_interval_s": interval,
                    "error": f"HTTP {exc.code}",
                    "cache": False,
                    "offline": offline,
                    "gate1_ok": False,
                }
            )
            if exc.code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                backoff = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
                time.sleep(min(backoff, 30.0))
                continue
            raise FetchError(f"HTTP {exc.code} em {url}") from exc
        except Exception as exc:  # noqa: BLE001 - rede heterogenea
            last_err = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
                continue
    raise FetchError(f"Falha de rede em {url}: {last_err}")


def _maybe_decompress(body: bytes, content_encoding: str | None) -> bytes:
    """Descomprime respostas gzip/deflate que o servidor enviou sem o cliente pedir."""
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    if body[:2] == b"PK":
        return body
    if content_encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return body
    return body


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_csv(text: str, delimiter: str) -> list[dict[str, str]]:
    """CSV estrito com cabecalho, tolerante a linhas vazias."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any((v or "").strip() for v in row.values())]


def manifest_entries() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    out = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
