from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
import uuid
from copy import deepcopy
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

from ont_tr069_configurator import APP_VERSION, expand_targets, process_target
from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "web_data"
HISTORY_FILE = DATA_DIR / "history.json"
CONFIG_FILE = BASE_DIR / "config.json"

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return deepcopy(default)


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def default_config() -> dict[str, Any]:
    return {
        "ont": {
            "protocol": "https",
            "port": 80,
            "targets": [],
            "username": "",
            "password": "",
        },
        "tr069": {
            "acs_url": "",
            "acs_username": "",
            "acs_password": "",
            "connection_request_username": "",
            "connection_request_password": "",
            "informing_interval": 43200,
            "informing_time": "0001-01-01T00:00:00Z",
            "dscp": 0,
        },
        "browser": {
            "timeout_ms": 30000,
            "ignore_https_errors": True,
            "tr069_path": "/html/ssmp/tr069/tr069.asp",
            "headed_finish_wait_ms": 500,
            "save_success_debug": False,
        },
    }


def load_base_config() -> dict[str, Any]:
    config = default_config()
    saved = load_json_file(CONFIG_FILE, {})
    for section, values in saved.items():
        if isinstance(values, dict) and isinstance(config.get(section), dict):
            config[section].update(values)
        else:
            config[section] = values
    return config


def sanitize_config_for_response(config: dict[str, Any]) -> dict[str, Any]:
    safe = deepcopy(config)
    for section, keys in {
        "ont": ["password"],
        "tr069": ["acs_password", "connection_request_password"],
    }.items():
        for key in keys:
            if safe.get(section, {}).get(key):
                safe[section][key] = "********"
    return safe


def append_history(job: dict[str, Any]) -> None:
    history = load_json_file(HISTORY_FILE, [])
    history.insert(0, job)
    write_json_file(HISTORY_FILE, history[:200])


def job_to_xml(job: dict[str, Any]) -> bytes:
    root = Element("lote")
    root.set("id", str(job.get("id", "")))
    root.set("status", str(job.get("status", "")))

    metadata = SubElement(root, "metadata")
    for key in ("created_at", "started_at", "finished_at", "total", "success_count", "error_count", "dry_run"):
        child = SubElement(metadata, key)
        child.text = "" if job.get(key) is None else str(job.get(key, ""))

    successes = SubElement(root, "sucessos")
    errors = SubElement(root, "erros")

    for result in job.get("results", []):
        parent = successes if result.get("status") == "SUCESSO" else errors
        ont = SubElement(parent, "ont")
        SubElement(ont, "ip").text = str(result.get("target", ""))
        SubElement(ont, "status").text = str(result.get("status", ""))
        if result.get("error"):
            SubElement(ont, "erro").text = str(result.get("error", ""))

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="utf-8")


def history_to_xml(history: list[dict[str, Any]]) -> bytes:
    root = Element("historico")
    for job in history:
        lote = SubElement(root, "lote")
        lote.set("id", str(job.get("id", "")))
        lote.set("status", str(job.get("status", "")))
        for key in ("created_at", "finished_at", "total", "success_count", "error_count"):
            child = SubElement(lote, key)
            child.text = "" if job.get(key) is None else str(job.get(key, ""))

        successes = SubElement(lote, "sucessos")
        errors = SubElement(lote, "erros")
        for result in job.get("results", []):
            parent = successes if result.get("status") == "SUCESSO" else errors
            ont = SubElement(parent, "ont")
            SubElement(ont, "ip").text = str(result.get("target", ""))
            SubElement(ont, "status").text = str(result.get("status", ""))
            if result.get("error"):
                SubElement(ont, "erro").text = str(result.get("error", ""))

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="utf-8")


def excel_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"<Cell><Data ss:Type=\"String\">{escape(text)}</Data></Cell>"


def excel_row(values: list[Any]) -> str:
    return "<Row>" + "".join(excel_cell(value) for value in values) + "</Row>"


def worksheet(name: str, rows: list[list[Any]]) -> str:
    body = "\n".join(excel_row(row) for row in rows)
    return (
        f"<Worksheet ss:Name=\"{escape(name)}\">"
        f"<Table>{body}</Table>"
        "</Worksheet>"
    )


def job_to_xls(job: dict[str, Any]) -> bytes:
    success_rows = [["IP", "Modelo", "Perfil", "Status", "Erro"]]
    error_rows = [["IP", "Modelo", "Perfil", "Status", "Erro"]]
    for result in job.get("results", []):
        row = [
            result.get("target", ""),
            result.get("model", ""),
            result.get("profile", ""),
            result.get("status", ""),
            result.get("error", ""),
        ]
        if result.get("status") == "SUCESSO":
            success_rows.append(row)
        else:
            error_rows.append(row)

    summary_rows = [
        ["Campo", "Valor"],
        ["ID", job.get("id", "")],
        ["Status", job.get("status", "")],
        ["Criado em", job.get("created_at", "")],
        ["Iniciado em", job.get("started_at", "")],
        ["Finalizado em", job.get("finished_at", "")],
        ["Total", job.get("total", 0)],
        ["Sucesso", job.get("success_count", 0)],
        ["Erro", job.get("error_count", 0)],
    ]
    return excel_workbook(
        [
            worksheet("Resumo", summary_rows),
            worksheet("Sucessos", success_rows),
            worksheet("Erros", error_rows),
        ]
    )


def history_to_xls(history: list[dict[str, Any]]) -> bytes:
    summary_rows = [["Data", "Status", "Total", "Sucesso", "Erro", "ID"]]
    success_rows = [["Data", "IP", "Modelo", "Perfil", "Status", "Erro", "Lote"]]
    error_rows = [["Data", "IP", "Modelo", "Perfil", "Status", "Erro", "Lote"]]

    for job in history:
        date = job.get("finished_at") or job.get("created_at", "")
        summary_rows.append(
            [
                date,
                job.get("status", ""),
                job.get("total", 0),
                job.get("success_count", 0),
                job.get("error_count", 0),
                job.get("id", ""),
            ]
        )
        for result in job.get("results", []):
            row = [
                date,
                result.get("target", ""),
                result.get("model", ""),
                result.get("profile", ""),
                result.get("status", ""),
                result.get("error", ""),
                job.get("id", ""),
            ]
            if result.get("status") == "SUCESSO":
                success_rows.append(row)
            else:
                error_rows.append(row)

    return excel_workbook(
        [
            worksheet("Resumo", summary_rows),
            worksheet("Sucessos", success_rows),
            worksheet("Erros", error_rows),
        ]
    )


def excel_workbook(worksheets: list[str]) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?mso-application progid="Excel.Sheet"?>\n'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:html="http://www.w3.org/TR/REC-html40">'
        + "".join(worksheets)
        + "</Workbook>"
    )
    return xml.encode("utf-8")


def parse_targets(text: str) -> list[str]:
    return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


def build_job_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = load_base_config()
    config["ont"].update(
        {
            "protocol": payload.get("protocol") or config["ont"].get("protocol", "https"),
            "port": int(payload.get("port") or config["ont"].get("port") or 80),
            "targets": parse_targets(payload.get("targets", "")),
            "username": payload.get("ont_username") or config["ont"].get("username", ""),
            "password": payload.get("ont_password") or config["ont"].get("password", ""),
        }
    )
    config["tr069"].update(
        {
            "acs_url": payload.get("acs_url") or config["tr069"].get("acs_url", ""),
            "acs_username": payload.get("acs_username") or config["tr069"].get("acs_username", ""),
            "acs_password": payload.get("acs_password") or config["tr069"].get("acs_password", ""),
            "connection_request_username": payload.get("connection_request_username")
            or config["tr069"].get("connection_request_username", ""),
            "connection_request_password": payload.get("connection_request_password")
            or config["tr069"].get("connection_request_password", ""),
            "informing_interval": int(payload.get("informing_interval") or 43200),
            "informing_time": payload.get("informing_time") or "0001-01-01T00:00:00Z",
            "dscp": int(payload.get("dscp") or 0),
        }
    )
    config["browser"].update(
        {
            "timeout_ms": int(payload.get("timeout_ms") or config["browser"].get("timeout_ms", 30000)),
            "tr069_path": payload.get("tr069_path") or config["browser"].get("tr069_path"),
        }
    )
    return config


async def run_job(job_id: str, config: dict[str, Any], dry_run: bool) -> None:
    targets = expand_targets(config["ont"])
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["total"] = len(targets)
        JOBS[job_id]["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        context = await browser.new_context(ignore_https_errors=True)
        for index, target in enumerate(targets, start=1):
            result = await process_target(context, config, target, headed=False, dry_run=dry_run)
            with JOBS_LOCK:
                JOBS[job_id]["results"].append(result)
                JOBS[job_id]["completed"] = index
        await browser.close()

    with JOBS_LOCK:
        results = JOBS[job_id]["results"]
        JOBS[job_id]["status"] = "finished"
        JOBS[job_id]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        JOBS[job_id]["success_count"] = sum(1 for item in results if item["status"] == "SUCESSO")
        JOBS[job_id]["error_count"] = sum(1 for item in results if item["status"] == "ERRO")
        append_history(deepcopy(JOBS[job_id]))


def run_job_thread(job_id: str, config: dict[str, Any], dry_run: bool) -> None:
    try:
        asyncio.run(run_job(job_id, config, dry_run))
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            JOBS[job_id]["error"] = str(exc)
            append_history(deepcopy(JOBS[job_id]))


def html_page() -> bytes:
    return INDEX_HTML.replace("${APP_VERSION}", APP_VERSION).encode("utf-8")


class WebHandler(BaseHTTPRequestHandler):
    server_version = "OntTr069Web/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def check_auth(self) -> bool:
        return True

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_xml(self, data: bytes, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_xls(self, data: bytes, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.ms-excel; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = html_page()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.check_auth():
            self.send_json({"error": "Token invalido."}, 401)
            return

        if parsed.path == "/api/defaults":
            self.send_json(sanitize_config_for_response(load_base_config()))
            return

        if parsed.path == "/api/jobs":
            with JOBS_LOCK:
                jobs = list(JOBS.values())[::-1]
            self.send_json(jobs)
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            self.send_json(job or {"error": "Job nao encontrado."}, 200 if job else 404)
            return

        if parsed.path == "/api/history":
            self.send_json(load_json_file(HISTORY_FILE, []))
            return

        if parsed.path == "/api/history/export.xml":
            self.send_xml(history_to_xml(load_json_file(HISTORY_FILE, [])), "historico_onts.xml")
            return

        if parsed.path == "/api/history/export.xls":
            self.send_xls(history_to_xls(load_json_file(HISTORY_FILE, [])), "historico_onts.xls")
            return

        if parsed.path.startswith("/api/history/") and parsed.path.endswith("/export.xml"):
            job_id = parsed.path.split("/")[3]
            history = load_json_file(HISTORY_FILE, [])
            job = next((item for item in history if item.get("id") == job_id), None)
            if not job:
                self.send_json({"error": "Historico nao encontrado."}, 404)
                return
            self.send_xml(job_to_xml(job), f"lote_{job_id}.xml")
            return

        if parsed.path.startswith("/api/history/") and parsed.path.endswith("/export.xls"):
            job_id = parsed.path.split("/")[3]
            history = load_json_file(HISTORY_FILE, [])
            job = next((item for item in history if item.get("id") == job_id), None)
            if not job:
                self.send_json({"error": "Historico nao encontrado."}, 404)
                return
            self.send_xls(job_to_xls(job), f"lote_{job_id}.xls")
            return

        self.send_json({"error": "Rota nao encontrada."}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.check_auth():
            self.send_json({"error": "Token invalido."}, 401)
            return

        if parsed.path != "/api/jobs":
            self.send_json({"error": "Rota nao encontrada."}, 404)
            return

        try:
            payload = self.read_json()
            dry_run = bool(payload.get("dry_run", False))
            config = build_job_config(payload)
            targets = expand_targets(config["ont"])
            if not targets:
                raise ValueError("Informe pelo menos um IP, range, CIDR ou URL.")
            job_id = str(uuid.uuid4())
            job = {
                "id": job_id,
                "status": "queued",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_at": None,
                "finished_at": None,
                "total": len(targets),
                "completed": 0,
                "success_count": 0,
                "error_count": 0,
                "dry_run": dry_run,
                "results": [],
            }
            with JOBS_LOCK:
                JOBS[job_id] = job
            thread = threading.Thread(target=run_job_thread, args=(job_id, config, dry_run), daemon=True)
            thread.start()
            self.send_json(job, 201)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)


INDEX_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Painel TR-069 ONT</title>
  <style>
    :root { font-family: Arial, Helvetica, sans-serif; background: #f3f6fa; color: #17212b; }
    body { margin: 0; }
    header { background: #1769aa; color: white; padding: 18px 28px; }
    main { padding: 24px; display: grid; gap: 18px; grid-template-columns: 420px 1fr; }
    section { background: white; border: 1px solid #dfe7ef; border-radius: 8px; padding: 18px; }
    h1, h2 { margin: 0 0 12px; }
    label { display: block; margin-top: 10px; font-weight: 700; font-size: 13px; }
    input, textarea { width: 100%; box-sizing: border-box; margin-top: 5px; padding: 9px; border: 1px solid #bcc7d3; border-radius: 5px; font: inherit; }
    textarea { min-height: 120px; resize: vertical; }
    button { margin-top: 14px; padding: 10px 14px; border: 0; border-radius: 5px; background: #1769aa; color: white; font-weight: 700; cursor: pointer; }
    button.secondary { background: #5b6875; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
    th, td { border-bottom: 1px solid #edf1f5; padding: 8px; text-align: left; vertical-align: top; }
    .ok { color: #137333; font-weight: 700; }
    .err { color: #b3261e; font-weight: 700; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .muted { color: #5b6875; font-size: 13px; }
    canvas { max-width: 320px; max-height: 220px; }
    @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Painel TR-069 ONT</h1>
    <div>Cadastro em lote, status por IP, modelo detectado e gráfico de resultado. Versão ${APP_VERSION}</div>
  </header>
  <main>
    <section>
      <h2>Novo lote</h2>
      <div class="grid">
        <div><label>Protocolo</label><input id="protocol" value="https"></div>
        <div><label>Porta</label><input id="port" value="80"></div>
      </div>
      <label>IPs, ranges, CIDR ou URLs</label>
      <textarea id="targets" placeholder="192.168.88.1&#10;192.168.88.10-20&#10;192.168.89.0/24"></textarea>
      <div class="grid">
        <div><label>Usuario ONT</label><input id="ont_username" value="telecomadmin"></div>
        <div><label>Senha ONT</label><input id="ont_password" type="password"></div>
      </div>
      <label>ACS URL</label><input id="acs_url" placeholder="http://10.99.99.62:7547">
      <div class="grid">
        <div><label>Usuario ACS</label><input id="acs_username" value="admin"></div>
        <div><label>Senha ACS</label><input id="acs_password" type="password"></div>
      </div>
      <div class="grid">
        <div><label>Connection Request User</label><input id="connection_request_username"></div>
        <div><label>Connection Request Password</label><input id="connection_request_password" type="password"></div>
      </div>
      <div class="grid">
        <div><label>Intervalo</label><input id="informing_interval" value="43200"></div>
        <div><label>DSCP</label><input id="dscp" value="0"></div>
      </div>
      <label>TR-069 path</label><input id="tr069_path" value="/html/ssmp/tr069/tr069.asp">
      <label><input id="dry_run" type="checkbox" style="width:auto"> Testar sem clicar em Apply</label>
      <button onclick="startJob()">Executar lote</button>
      <button class="secondary" onclick="loadHistory()">Atualizar histórico</button>
      <button class="secondary" onclick="exportHistoryXls()">Exportar histórico XLS</button>
      <p class="muted" id="message"></p>
    </section>

    <section>
      <h2>Resultado atual</h2>
      <canvas id="chart" width="320" height="220"></canvas>
      <div class="muted" id="progress">Nenhum lote em execução.</div>
      <table>
        <thead><tr><th>ONT</th><th>Modelo</th><th>Status</th><th>Erro</th></tr></thead>
        <tbody id="results"></tbody>
      </table>
    </section>

    <section style="grid-column: 1 / -1;">
      <h2>Histórico</h2>
      <table>
        <thead><tr><th>Data</th><th>Status</th><th>Total</th><th>Sucesso</th><th>Erro</th><th>XLS</th></tr></thead>
        <tbody id="history"></tbody>
      </table>
    </section>
  </main>
  <script>
    let currentJob = null;
    const $ = id => document.getElementById(id);

    function headers() {
      return { "Content-Type": "application/json" };
    }

    function payload() {
      const ids = ["protocol","port","targets","ont_username","ont_password","acs_url","acs_username","acs_password",
        "connection_request_username","connection_request_password","informing_interval","dscp","tr069_path"];
      const data = {};
      ids.forEach(id => data[id] = $(id).value);
      data.dry_run = $("dry_run").checked;
      return data;
    }

    async function startJob() {
      $("message").textContent = "Enviando lote...";
      const res = await fetch("/api/jobs", { method: "POST", headers: headers(), body: JSON.stringify(payload()) });
      const data = await res.json();
      if (!res.ok) { $("message").textContent = data.error || "Erro ao iniciar."; return; }
      currentJob = data.id;
      $("message").textContent = "Lote iniciado.";
      pollJob();
    }

    async function pollJob() {
      if (!currentJob) return;
      const res = await fetch(`/api/jobs/${currentJob}`, { headers: headers() });
      const job = await res.json();
      renderJob(job);
      if (["queued","running"].includes(job.status)) setTimeout(pollJob, 1500);
      else loadHistory();
    }

    function renderJob(job) {
      $("progress").textContent = `${job.status} | ${job.completed || 0}/${job.total || 0}`;
      $("results").innerHTML = (job.results || []).map(r => `<tr><td>${r.target}</td><td>${r.model || ""}</td><td class="${r.status === "SUCESSO" ? "ok" : "err"}">${r.status}</td><td>${r.error || ""}</td></tr>`).join("");
      drawChart(job.success_count || 0, job.error_count || 0);
    }

    async function loadHistory() {
      const res = await fetch("/api/history", { headers: headers() });
      const items = await res.json();
      $("history").innerHTML = items.map(j => `<tr><td>${j.finished_at || j.created_at}</td><td>${j.status}</td><td>${j.total}</td><td class="ok">${j.success_count || 0}</td><td class="err">${j.error_count || 0}</td><td><button class="secondary" onclick="exportJobXls('${j.id}')">XLS</button></td></tr>`).join("");
    }

    function exportJobXls(id) {
      window.location.href = `/api/history/${id}/export.xls`;
    }

    function exportHistoryXls() {
      window.location.href = "/api/history/export.xls";
    }

    function drawChart(success, error) {
      const canvas = $("chart"), ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const total = success + error || 1;
      const values = [
        { label: "Sucesso", value: success, color: "#188038" },
        { label: "Erro", value: error, color: "#d93025" }
      ];
      let start = -Math.PI / 2;
      values.forEach(item => {
        const end = start + (item.value / total) * Math.PI * 2;
        ctx.beginPath(); ctx.moveTo(105, 105); ctx.arc(105, 105, 80, start, end); ctx.closePath();
        ctx.fillStyle = item.color; ctx.fill(); start = end;
      });
      ctx.fillStyle = "#17212b"; ctx.font = "14px Arial";
      ctx.fillText(`Sucesso: ${success}`, 220, 90);
      ctx.fillText(`Erro: ${error}`, 220, 120);
    }

    loadHistory();
  </script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Painel web para configurar TR-069 em ONTs.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), WebHandler)
    print(f"Painel TR-069 ouvindo em http://{args.host}:{args.port}")
    print("Use firewall, VPN ou proxy reverso com HTTPS para proteger o painel.")
    server.serve_forever()


if __name__ == "__main__":
    main()
