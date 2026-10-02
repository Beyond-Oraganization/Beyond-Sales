"""Generuje statyczny index.html z osadzonymi danymi z bazy (jako JSON w <script>).

To tylko podgląd do odczytu: dane są wklejone bezpośrednio do pliku HTML, więc
działa po zwykłym dwukliku, bez serwera i bez problemów CORS przy file://.
Interaktywny panel (edycja, wysyłka, eksport) to `python3 server.py`.

Plik powstaje na żądanie (`python3 dashboard.py`), a nie po każdym zapisie: wklejanie całej tabeli do HTML-a
kosztuje tyle, ile ma wierszy, więc przy dziesiątkach tysięcy firm nie może towarzyszyć każdej zmianie.
"""

from __future__ import annotations

import json
import os

from perno.db import Database
from perno.domain import countries
from perno.domain.statuses import STATUS_META as STATUS_LABELS

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Perno — Maile (dashboard)</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --border:#e2e5ea; --text:#1b1f27; --muted:#6b7280;
    --accent:#2f6feb; --row-hover:#f0f3fa;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0f1115; --card:#171a21; --border:#282d37; --text:#e7e9ee; --muted:#9aa2b1;
           --accent:#5b8cff; --row-hover:#1c2029; }
  }
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);}
  header{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--border);
         padding:18px 24px 14px;}
  header h1{margin:0 0 10px;font-size:19px;font-weight:650;}
  .summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
  .summary .chip{font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;
                  background:var(--card);border:1px solid var(--border);}
  .controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
  .controls input, .controls select{font-family:inherit;font-size:13px;padding:7px 10px;
      border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--text);}
  .controls input[type="text"]{flex:1 1 240px;min-width:200px;}
  .count{font-size:12.5px;color:var(--muted);white-space:nowrap;}
  main{padding:16px 24px 60px;}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);
        border-radius:10px;overflow:hidden;}
  thead th{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;
           color:var(--muted);padding:10px 12px;border-bottom:1px solid var(--border);}
  tbody tr{border-bottom:1px solid var(--border);cursor:pointer;}
  tbody tr:hover{background:var(--row-hover);}
  tbody td{padding:9px 12px;font-size:13px;vertical-align:top;}
  .badge{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;color:#fff;
         white-space:nowrap;}
  .site-link{margin-left:6px;text-decoration:none;opacity:.7;}
  .site-link:hover{opacity:1;}
  .detail-row td{background:var(--row-hover);padding:16px 20px;}
  .detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  .detail-grid h4{margin:0 0 6px;font-size:11.5px;text-transform:uppercase;color:var(--muted);}
  .body-preview{border:1px solid var(--border);border-radius:8px;background:var(--card);padding:12px;
                max-height:420px;overflow:auto;font-size:13px;}
  .body-text{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;
             border:1px solid var(--border);border-radius:8px;background:var(--card);padding:12px;
             max-height:420px;overflow:auto;}
  .error-box{color:#c0392b;font-size:12.5px;margin-top:8px;}
  .empty-state{padding:50px;text-align:center;color:var(--muted);}
  .muted{color:var(--muted);}
</style>
</head>
<body>

<header>
  <h1>Perno — dashboard maili (Eventy + Driverzy)</h1>
  <div class="summary" id="summary"></div>
  <div class="controls">
    <select id="segmentFilter"><option value="">Wszystkie segmenty</option></select>
    <select id="statusFilter"><option value="">Wszystkie statusy</option></select>
    <input type="text" id="search" placeholder="Szukaj po firmie lub e-mailu…">
    <span class="count" id="count"></span>
  </div>
</header>

<main>
  <table>
    <thead>
      <tr>
        <th>Segment</th><th>Firma</th><th>Kraj</th><th>E-mail</th><th>Temat</th><th>Status</th>
        <th>Wygenerowano</th><th>Wysłano</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="empty-state" id="emptyState" style="display:none;">Brak wierszy pasujących do filtrów.</div>
</main>

<script>
const MAILE_ROWS = /*__DATA__*/;
const STATUS_LABELS = /*__STATUS_LABELS__*/;
const COUNTRIES = /*__COUNTRIES__*/;  // kod -> [nazwa po polsku, flaga]

const tbody = document.getElementById('tbody');
const emptyState = document.getElementById('emptyState');
const countEl = document.getElementById('count');
const searchInput = document.getElementById('search');
const segmentFilter = document.getElementById('segmentFilter');
const statusFilter = document.getElementById('statusFilter');
let expandedId = null;

function unique(list){ return [...new Set(list)]; }

function fillSelect(select, values){
  values.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v; opt.textContent = v;
    select.appendChild(opt);
  });
}
fillSelect(segmentFilter, unique(MAILE_ROWS.map(r => r.segment)).sort());
fillSelect(statusFilter, unique(MAILE_ROWS.map(r => r.status)).sort());

function escapeHtml(s){
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function badge(status){
  const info = STATUS_LABELS[status] || [status, '#6b7280'];
  return `<span class="badge" style="background:${info[1]}">${escapeHtml(info[0])}</span>`;
}

function countryCell(row){
  const info = COUNTRIES[row.country];
  if (!info) return row.country ? escapeHtml(row.country) : '<span class="muted">—</span>';
  const city = row.city ? ` <span class="muted">${escapeHtml(row.city)}</span>` : '';
  return `<span title="${escapeHtml(info[0])}">${info[1]} ${escapeHtml(row.country)}</span>${city}`;
}

function websiteLink(url){
  if (!url) return '';
  return `<a class="site-link" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${escapeHtml(url)}">🔗</a>`;
}

function matches(row){
  const q = searchInput.value.trim().toLowerCase();
  const seg = segmentFilter.value;
  const st = statusFilter.value;
  if (seg && row.segment !== seg) return false;
  if (st && row.status !== st) return false;
  if (q && !(`${row.company} ${row.email}`.toLowerCase().includes(q))) return false;
  return true;
}

function renderSummary(){
  const counts = {};
  MAILE_ROWS.forEach(r => { counts[r.status] = (counts[r.status]||0) + 1; });
  const el = document.getElementById('summary');
  el.innerHTML = Object.entries(counts).map(([status, n]) => {
    const info = STATUS_LABELS[status] || [status, '#6b7280'];
    return `<span class="chip" style="border-color:${info[1]};color:${info[1]}">${escapeHtml(info[0])}: ${n}</span>`;
  }).join('');
}

function renderBodies(row){
  if (!('body_html' in row)) {
    return `<div class="muted" style="font-size:12.5px;">Ten podgląd jest bez treści maili${row.has_body ? '' : ' (ten wiersz i tak jej nie ma)'}.
      Przeczytasz ją w panelu (<code>python3 server.py</code>) albo w podglądzie z treściami:
      <code>python3 dashboard.py --with-bodies</code>.</div>`;
  }
  return `
    <div class="detail-grid">
      <div>
        <h4>Treść (HTML)</h4>
        <div class="body-preview">${row.body_html || '<span class="muted">(brak)</span>'}</div>
      </div>
      <div>
        <h4>Treść (plain text)</h4>
        <div class="body-text">${escapeHtml(row.body_text || '(brak)')}</div>
      </div>
    </div>`;
}

function renderDetail(row){
  const tr = document.createElement('tr');
  tr.className = 'detail-row';
  const td = document.createElement('td');
  td.colSpan = 8;
  td.innerHTML = `
    ${renderBodies(row)}
    ${row.error ? `<div class="error-box"><strong>Błąd:</strong> ${escapeHtml(row.error)}</div>` : ''}
    ${row.attachments ? `<div class="muted" style="margin-top:8px;font-size:12px;">Załączniki/obrazy: ${escapeHtml(row.attachments)} ${escapeHtml(row.inline_images||'')}</div>` : ''}
  `;
  tr.appendChild(td);
  return tr;
}

function render(){
  tbody.innerHTML = '';
  const visible = MAILE_ROWS.filter(matches);
  visible.forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(row.segment)}</td>
      <td>${escapeHtml(row.company)}${websiteLink(row.website)}</td>
      <td>${countryCell(row)}</td>
      <td>${escapeHtml(row.email)}</td>
      <td>${escapeHtml(row.subject)}</td>
      <td>${badge(row.status)}</td>
      <td class="muted">${escapeHtml(row.generated_at)}</td>
      <td class="muted">${escapeHtml(row.sent_at)}</td>
    `;
    tr.addEventListener('click', () => {
      expandedId = expandedId === row.id ? null : row.id;
      render();
    });
    const siteLink = tr.querySelector('.site-link');
    if (siteLink) siteLink.addEventListener('click', e => e.stopPropagation());
    tbody.appendChild(tr);
    if (expandedId === row.id) {
      tbody.appendChild(renderDetail(row));
    }
  });
  emptyState.style.display = visible.length ? 'none' : 'block';
  countEl.textContent = `${visible.length} / ${MAILE_ROWS.length} wierszy`;
}

[searchInput, segmentFilter, statusFilter].forEach(el => el.addEventListener('input', render));
renderSummary();
render();
</script>

</body>
</html>
"""


def write_dashboard(db: Database, output_path: str, *, with_bodies: bool = False) -> int:
    """Zapisuje podgląd z bazy i zwraca liczbę wierszy. Domyślnie bez treści maili (to większość rozmiaru pliku)."""
    rows = db.fetch_all(light=not with_bodies)
    generate_dashboard(rows, output_path)
    return len(rows)


def generate_dashboard(rows: list[dict], output_path: str) -> None:
    """Zapisuje statyczny podgląd (HTML z osadzonymi wierszami) atomowo: najpierw plik tymczasowy, potem podmiana."""
    data_json = json.dumps(rows, ensure_ascii=False).replace("</script", "<\\/script")
    labels_json = json.dumps(STATUS_LABELS, ensure_ascii=False)
    countries_json = json.dumps({code: [name, flag] for code, name, flag in countries.choices()}, ensure_ascii=False)
    html = (_PAGE_TEMPLATE.replace("/*__DATA__*/", data_json).replace("/*__STATUS_LABELS__*/", labels_json)
            .replace("/*__COUNTRIES__*/", countries_json))
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_path, output_path)
