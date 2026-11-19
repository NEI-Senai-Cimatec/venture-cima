#!/usr/bin/env python3
"""Screenshots + medicao runtime do dashboard executivo em Chrome headless.

Uso:
    python tools/screenshots.py --html dashboard/index.html --out audit/shots

Para cada viewport (375x812, 768x1024, 1440x900) o script:
  1. captura um PNG (--window-size + --screenshot) e confere a largura real do PNG;
  2. injeta um probe JS numa copia TEMPORARIA do HTML e roda --dump-dom para medir,
     no DOM renderizado: document.scrollWidth vs window.innerWidth (overflow
     horizontal), elementos que estouram o viewport, menor font-size computado,
     tabular-nums+nowrap efetivos em celulas monetarias, existencia de h1/tabela/
     linhas/figuras e erros de JS capturados por window.onerror.
  3. grava <out>/shots_report.json consumido pelo Gate 4 de pipeline/gates.py.

Exit codes: 0 = 3 PNGs com largura correta; 2 = Chrome ausente/indisponivel ou
captura incompleta (sempre com o erro exato no JSON; nunca sucesso otimista).

Nota: o probe NAO modifica o dashboard entregue; a copia instrumentada e' criada
dentro de <out> e removida no final.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

BASE_FLAGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--force-device-scale-factor=1",
]

VIEWPORTS = [(375, 812), (768, 1024), (1440, 900)]

PROBE_SCRIPT = r"""
<script id="__audit_probe__">
(function(){
  var errors = [];
  window.addEventListener('error', function(e){
    try { errors.push(String((e && (e.message || e.type)) || 'error')); } catch(_) {}
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    try { errors.push('unhandledrejection: ' + String((e && e.reason) || '')); } catch(_) {}
  });
  function textOf(el){
    var t = '';
    for (var k = 0; k < el.childNodes.length; k++) {
      if (el.childNodes[k].nodeType === 3) t += el.childNodes[k].nodeValue;
    }
    return t;
  }
  function measure(){
    try {
      var de = document.documentElement;
      var body = document.body;
      var vw = window.innerWidth;
      var vh = window.innerHeight;
      var overflow = [];
      var all = document.querySelectorAll('body *');
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || st.position === 'fixed') continue;
        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if ((r.right > vw + 1 || r.left < -1) && overflow.length < 30) {
          overflow.push({tag: el.tagName, cls: String(el.className || '').slice(0, 80),
                         left: Math.round(r.left), right: Math.round(r.right),
                         width: Math.round(r.width)});
        }
      }
      var minFont = null, minFontEl = '';
      for (var j = 0; j < all.length; j++) {
        var e2 = all[j];
        var cs = getComputedStyle(e2);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        var direct = textOf(e2);
        if (!direct.replace(/\s+/g, '')) continue;
        var fs = parseFloat(cs.fontSize);
        if (fs > 0 && (minFont === null || fs < minFont)) {
          minFont = fs;
          minFontEl = e2.tagName + '.' + String(e2.className || '').slice(0, 60);
        }
      }
      var cells = document.querySelectorAll('table td, table th');
      var MONEY_RE = /(R\$|US\$|\$)\s?[0-9]|[0-9]+(?:[.,][0-9]+)?\s?%/;
      var moneyCells = 0, moneyOk = 0, moneyFail = [];
      function moneyTarget(cell) {
        // o requisito e sobre o VALOR exibido: a propriedade pode estar no span
        // interno (.num/.m-money), nao no <td>. Mede o elemento que contem o valor.
        var nodes = cell.querySelectorAll('*');
        for (var n = 0; n < nodes.length; n++) {
          var t = nodes[n].textContent || '';
          if (MONEY_RE.test(t)) return nodes[n];
        }
        return cell;
      }
      for (var m = 0; m < cells.length; m++) {
        var txt = cells[m].textContent || '';
        if (!MONEY_RE.test(txt)) continue;
        moneyCells++;
        var target = moneyTarget(cells[m]);
        var ccs = getComputedStyle(target);
        var tab = String(ccs.fontVariantNumeric || '').indexOf('tabular-nums') >= 0;
        var nw = ccs.whiteSpace === 'nowrap';
        if (tab && nw) moneyOk++;
        else if (moneyFail.length < 5) moneyFail.push({tag: target.tagName,
          cls: String(target.className || '').slice(0, 60), tabular: tab, nowrap: nw,
          text: (target.textContent || '').trim().slice(0, 30)});
      }
      var result = {
        innerWidth: vw,
        innerHeight: vh,
        documentScrollWidth: de ? de.scrollWidth : null,
        bodyScrollWidth: body ? body.scrollWidth : null,
        horizontal_overflow: !!((de && de.scrollWidth > vw + 1) || (body && body.scrollWidth > vw + 1)),
        overflowElements: overflow,
        minFontPx: minFont,
        minFontElement: minFontEl,
        h1: document.querySelectorAll('h1').length,
        tables: document.querySelectorAll('table').length,
        tableRows: document.querySelectorAll('table tbody tr').length,
        figures: document.querySelectorAll('figure').length,
        figuresWithCaption: document.querySelectorAll('figure figcaption').length,
        moneyCells: moneyCells,
        moneyCellsTabularNowrap: moneyOk,
        moneyCellsNotCompliant: moneyFail,
        jsErrors: errors,
        readyState: document.readyState
      };
      var pre = document.createElement('pre');
      pre.id = '__audit_runtime__';
      pre.textContent = 'AUDIT_RUNTIME_JSON:' + JSON.stringify(result);
      (document.body || de).appendChild(pre);
    } catch (err) {
      try {
        var pre2 = document.createElement('pre');
        pre2.id = '__audit_runtime__';
        pre2.textContent = 'AUDIT_RUNTIME_ERROR:' + String((err && (err.message || err)) || err);
        (document.body || document.documentElement).appendChild(pre2);
      } catch (_) {}
    }
  }
  function start(){ setTimeout(measure, 300); }
  if (document.readyState === 'complete' || document.readyState === 'interactive') { start(); }
  else { window.addEventListener('load', start); }
  window.addEventListener('load', function(){ setTimeout(measure, 800); });
})();
</script>
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def find_chrome(explicit: str | None = None) -> Path | None:
    for candidate in ([explicit] if explicit else []) + CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    which = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    return Path(which) if which else None


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)
    except OSError:
        return None


def _run_chrome(chrome: Path, args: list[str], timeout: int) -> dict:
    started = time.time()
    try:
        # Chrome escreve stdout/stderr em ANSI/UTF-8 misto no Windows; decodificar
        # com encoding fixo quebra a thread leitora do subprocess. errors="replace"
        # garante que o diagnostico do Chrome nunca derrube a medicao.
        proc = subprocess.run([str(chrome)] + args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return {
            "exit_code": proc.returncode,
            "elapsed_s": round(time.time() - started, 2),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "elapsed_s": round(time.time() - started, 2),
            "error": f"timeout de {timeout}s",
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
        }
    except OSError as exc:
        return {"exit_code": None, "elapsed_s": 0, "error": f"{type(exc).__name__}: {exc}",
                "stdout": "", "stderr": ""}


def capture_screenshot(chrome: Path, url: str, width: int, height: int, out_png: Path,
                       profile_dir: Path, timeout: int, dpr: float = 1.0) -> dict:
    if out_png.exists():
        out_png.unlink()
    # Chrome no Windows clampa a janela em ~500 px; para medir larguras CSS reais
    # (375/768) usamos device scale factor e janela fisica = css * dpr.
    phys_w = max(1, int(round(width * dpr)))
    phys_h = max(1, int(round(height * dpr)))
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = BASE_FLAGS + [
        f"--user-data-dir={profile_dir}",
        f"--window-size={phys_w},{phys_h}",
        f"--force-device-scale-factor={dpr:g}",
        "--virtual-time-budget=8000",
        "--hide-scrollbars",
        f"--screenshot={out_png}",
        url,
    ]
    result = _run_chrome(chrome, args, timeout)
    result.update({"width": width, "height": height, "dpr": dpr, "path": str(out_png),
                   "exists": out_png.is_file()})
    if out_png.is_file():
        result["bytes"] = out_png.stat().st_size
        result["dims"] = png_size(out_png)
        result["dims_ok"] = bool(result["dims"] and abs(result["dims"][0] - width * dpr) <= 2)
    else:
        result["bytes"] = 0
        result["dims"] = None
        result["dims_ok"] = False
    return result


def build_probe_html(html: str) -> str:
    # Usar lambda para o replacement: PROBE_SCRIPT contem \s, \d etc. de JS que
    # seriam interpretados como escapes invalidos se passados como string literal.
    if re.search(r"</body\s*>", html, re.I):
        return re.sub(r"</body\s*>", lambda match: PROBE_SCRIPT + "</body>", html, count=1, flags=re.I)
    return html + PROBE_SCRIPT


def extract_runtime(dom_text: str) -> dict:
    """Extrai o JSON do probe do DOM serializado pelo Chrome (--dump-dom)."""
    match = re.search(r'<pre[^>]*id="__audit_runtime__"[^>]*>(.*?)</pre>', dom_text, re.S | re.I)
    payload = match.group(1) if match else ""
    if not payload:
        fallback = re.search(r"AUDIT_RUNTIME_JSON:(\{.*?\})\s*$", dom_text, re.S)
        payload = fallback.group(1) if fallback else ""
    if not payload:
        return {"checked": False, "error": "probe JS nao executou/nao serializou resultado no DOM"}
    payload = html_mod.unescape(payload).strip()
    if payload.startswith("AUDIT_RUNTIME_JSON:"):
        payload = payload[len("AUDIT_RUNTIME_JSON:"):]
    if payload.startswith("AUDIT_RUNTIME_ERROR:"):
        return {"checked": False, "error": payload[len("AUDIT_RUNTIME_ERROR:"):][:500]}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {"checked": False, "error": f"JSON do probe invalido: {exc}"}
    if not isinstance(data, dict):
        return {"checked": False, "error": "probe retornou tipo inesperado"}
    data["checked"] = True
    return data


def measure_viewport(chrome: Path, probe_url: str, width: int, height: int, profile_dir: Path,
                     timeout: int, dump_dom_path: Path | None, dpr: float = 1.0) -> dict:
    phys_w = max(1, int(round(width * dpr)))
    phys_h = max(1, int(round(height * dpr)))
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = BASE_FLAGS + [
        f"--user-data-dir={profile_dir}",
        f"--window-size={phys_w},{phys_h}",
        f"--force-device-scale-factor={dpr:g}",
        "--virtual-time-budget=8000",
        "--dump-dom",
        probe_url,
    ]
    result = _run_chrome(chrome, args, timeout)
    dom_text = result.get("stdout") or ""
    if "<html" not in dom_text.lower() and "<html" in (result.get("stderr") or "").lower():
        dom_text = result["stderr"]
    if "<html" not in dom_text.lower() and "<!doctype" not in dom_text.lower():
        return {"checked": False, "error": "dump-dom nao retornou HTML; stderr: " +
                                           str(result.get("stderr", ""))[-600:]}
    if dump_dom_path is not None:
        try:
            dump_dom_path.write_text(dom_text, encoding="utf-8", errors="replace")
        except OSError:
            pass
    runtime = extract_runtime(dom_text)
    runtime["dom_bytes"] = len(dom_text.encode("utf-8"))
    return runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screenshots + medicao runtime (scrollWidth/JS/fontes) do dashboard.")
    parser.add_argument("--html", default=str(ROOT / "dashboard" / "index.html"))
    parser.add_argument("--out", default=str(ROOT / "audit" / "shots"))
    parser.add_argument("--json", default=None, help="relatorio JSON (default <out>/shots_report.json)")
    parser.add_argument("--chrome", default=None, help="caminho explicito do Chrome/Edge")
    parser.add_argument("--timeout", type=int, default=120, help="timeout por invocacao do Chrome (s)")
    parser.add_argument("--no-measure", dest="measure", action="store_false", default=True,
                        help="nao injetar probe JS / nao medir scrollWidth")
    parser.add_argument("--keep-dom", dest="keep_dom", action="store_true", default=True)
    parser.add_argument("--no-keep-dom", dest="keep_dom", action="store_false")
    parser.add_argument("--dpr", type=float, default=1.0,
                        help="device scale factor para emular largura CSS < 500 px no Windows")
    parser.add_argument("--viewports", default=None,
                        help="larguras CSS separadas por virgula (ex.: 375,1440); default 375,768,1440")
    parser.add_argument("--hash", default=None,
                        help="fragmento/deep-link a abrir (ex.: insights, fundos, metodo)")
    parser.add_argument("--no-shots", dest="shots", action="store_false", default=True,
                        help="mede runtime (probe/scrollWidth) sem tentar PNG (util quando --screenshot trava)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    html_path = Path(args.html)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.json) if args.json else out_dir / "shots_report.json"
    report: dict = {
        "generated_at": utc_now(),
        "html": str(html_path),
        "out_dir": str(out_dir),
        "viewports": {},
        "shots_enabled": args.shots,
        "overflow": {"measured": False, "per_viewport": {}},
        "js_errors": [],
        "dom_check": {"checked": False, "detail": "nao executado"},
        "dpr": args.dpr,
        "hash": args.hash,
        "error": None,
        "limitations": [],
    }

    if not html_path.is_file():
        report["error"] = f"HTML ausente: {html_path}"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[screenshots] ERRO: {report['error']}", file=sys.stderr)
        return 2

    chrome = find_chrome(args.chrome)
    report["chrome"] = str(chrome) if chrome else None
    if chrome is None:
        report["error"] = ("Chrome/Edge nao encontrado. Defina CHROME_PATH ou --chrome. "
                           "Screenshots PENDING (nao ha aprovacao otimista).")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[screenshots] ERRO: {report['error']}", file=sys.stderr)
        return 2

    html = html_path.read_text(encoding="utf-8", errors="replace")
    probe_path = out_dir / ".audit_probe.html"
    probe_path.write_text(build_probe_html(html), encoding="utf-8")
    frag = ("#" + args.hash.lstrip("#")) if args.hash else ""
    url = html_path.resolve().as_uri() + frag
    probe_url = probe_path.resolve().as_uri() + frag
    tmp_root = out_dir / f".tmp_shots_{time.time_ns()}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    failures = 0
    runtime_available = 0
    all_js_errors: list[str] = []
    try:
        viewports = VIEWPORTS
        if args.viewports:
            wanted = {int(x) for x in args.viewports.split(",") if x.strip().isdigit()}
            viewports = [(w, h) for (w, h) in VIEWPORTS if w in wanted]
            if not viewports:
                raise SystemExit("--viewports nao casou com 375/768/1440")
        for width, height in viewports:
            key = str(width)
            png = out_dir / f"dashboard-{width}.png"
            if args.shots:
                shot = capture_screenshot(chrome, url, width, height, png, tmp_root / f"profile_shot_{width}",
                                          args.timeout, args.dpr)
            else:
                shot = {"enabled": False, "note": "--no-shots", "exists": False}
            runtime: dict = {"checked": False, "error": "medicao desabilitada (--no-measure)"}
            if args.measure:
                dump_path = out_dir / "rendered_dom.html" if (args.keep_dom and width == 1440) else None
                runtime = measure_viewport(chrome, probe_url, width, height,
                                           tmp_root / f"profile_dom_{width}", args.timeout, dump_path,
                                           args.dpr)
                if runtime.get("checked"):
                    runtime_available += 1
                    all_js_errors.extend(str(err) for err in runtime.get("jsErrors") or [])
                    report["overflow"]["per_viewport"][key] = {
                        "innerWidth": runtime.get("innerWidth"),
                        "documentScrollWidth": runtime.get("documentScrollWidth"),
                        "bodyScrollWidth": runtime.get("bodyScrollWidth"),
                        "horizontal_overflow": runtime.get("horizontal_overflow"),
                        "overflowElements": runtime.get("overflowElements") or [],
                    }
            entry = {
                "path": str(png), "width": width, "height": height,
                "exists": shot.get("exists"), "bytes": shot.get("bytes"),
                "dims": shot.get("dims"), "dims_ok": shot.get("dims_ok"),
                "elapsed_s": shot.get("elapsed_s"), "exit_code": shot.get("exit_code"),
                "stderr_tail": (shot.get("stderr") or shot.get("error") or "")[-1500:],
                "runtime": runtime,
                "runtime_error": runtime.get("error") if not runtime.get("checked") else None,
            }
            report["viewports"][key] = entry
            if not shot.get("dims_ok"):
                failures += 1
            if not args.quiet:
                status = "OK" if shot.get("dims_ok") else "FALHOU"
                rt_status = "runtime OK" if runtime.get("checked") else f"runtime indisponivel: {runtime.get('error')}"
                print(f"[screenshots] {width}x{height}: shot={status}; {rt_status}", file=sys.stderr)

        report["overflow"]["measured"] = runtime_available > 0
        report["js_errors"] = all_js_errors
        # dom_check com base no runtime de 1440 (ou ultimo disponivel)
        runtime_1440 = (report["viewports"].get("1440") or {}).get("runtime") or {}
        if runtime_1440.get("checked"):
            ok = bool(runtime_1440.get("h1", 0) >= 1 and runtime_1440.get("tables", 0) >= 1
                      and runtime_1440.get("tableRows", 0) >= 1)
            report["dom_check"] = {
                "checked": True,
                "ok": ok,
                "detail": (f"DOM renderizado 1440px: h1={runtime_1440.get('h1')}, "
                           f"tables={runtime_1440.get('tables')}, tr={runtime_1440.get('tableRows')}, "
                           f"figures={runtime_1440.get('figures')} "
                           f"(com figcaption={runtime_1440.get('figuresWithCaption')}); "
                           f"script DOM bytes={runtime_1440.get('dom_bytes')}"),
                "dom_path": str(out_dir / "rendered_dom.html") if (out_dir / "rendered_dom.html").exists() else None,
                "h1": runtime_1440.get("h1"),
                "tables": runtime_1440.get("tables"),
                "table_rows": runtime_1440.get("tableRows"),
                "figures": runtime_1440.get("figures"),
            }
        else:
            report["dom_check"] = {
                "checked": False,
                "detail": "probe de DOM nao executou em nenhum viewport: " +
                          str(runtime_1440.get("error") or "sem runtime"),
            }
        if runtime_available == 0:
            report["limitations"].append(
                "Probe JS de runtime nao executou (dump-dom sem marcador). Overflow/scrollWidth nao medidos."
            )
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass
        if not args.keep_dom:
            try:
                (out_dir / "rendered_dom.html").unlink()
            except OSError:
                pass
        shutil.rmtree(tmp_root, ignore_errors=True)

    captured = sum(1 for viewport in report["viewports"].values() if viewport.get("dims_ok"))
    if failures:
        first_error = next((viewport.get("stderr_tail") for viewport in report["viewports"].values()
                            if not viewport.get("dims_ok") and viewport.get("stderr_tail")), "")
        report["error"] = (
            f"{failures}/{len(VIEWPORTS)} screenshots falharam. Possivel bloqueio de sandbox "
            f"(named pipes/mojo). stderr: {str(first_error)[:900]}"
        )
        report["limitations"].append(
            "Sem screenshots nao e' possivel verificar larguras; medicao runtime fica incompleta."
        )
    report["captured"] = captured
    report["total_viewports"] = len(VIEWPORTS)
    report["runtime_measured_viewports"] = runtime_available
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[screenshots] relatorio: {report_path} ({captured}/{len(VIEWPORTS)} PNGs, "
          f"{runtime_available}/{len(VIEWPORTS)} runtime)")
    if failures:
        print(f"[screenshots] ERRO: {report['error']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
