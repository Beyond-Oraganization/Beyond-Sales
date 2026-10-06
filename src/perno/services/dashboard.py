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
from perno.design import read_tokens
from perno.domain import countries
from perno.domain.statuses import STATUS_META as STATUS_LABELS

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>Perno — Maile (dashboard)</title>
<style>
/*__TOKENS__*/
  *{box-sizing:border-box;}
  body{margin:0;font:400 14px/1.45 var(--font);background:var(--bg);color:var(--label);-webkit-font-smoothing:antialiased;}
  :focus-visible{outline:3px solid var(--accent-ring);outline-offset:1px;}
  header{position:sticky;top:0;z-index:var(--z-nav);padding:var(--s-5) var(--s-6) var(--s-3);background:var(--material);
         -webkit-backdrop-filter:saturate(180%) blur(24px);backdrop-filter:saturate(180%) blur(24px);
         border-bottom:1px solid var(--separator);}
  header h1{margin:0 0 var(--s-3);font-size:var(--fs-page);font-weight:700;letter-spacing:-.02em;line-height:1.2;}
  .summary{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:var(--s-3);}
  .summary .chip{--c:var(--label-2);display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 11px;border-radius:var(--r-pill);
                 background:color-mix(in srgb,var(--c) 14%,transparent);color:color-mix(in srgb,var(--c) var(--ink-mix),var(--label));
                 font-size:var(--fs-foot);font-weight:500;}
  .summary .chip::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--c);}
  .controls{display:flex;gap:var(--s-2);flex-wrap:wrap;align-items:center;}
  .controls input,.controls select{min-height:var(--control-h);padding:0 10px;font:400 var(--fs-ui)/1.3 var(--font);color:var(--label);
      background:var(--surface);border:1px solid var(--field-border);border-radius:var(--r-md);}
  .controls select{-webkit-appearance:none;appearance:none;padding-right:28px;background-image:var(--select-chev);
      background-repeat:no-repeat;background-position:right 9px center;background-size:12px;}
  .controls input[type="text"]{flex:1 1 240px;min-width:200px;border-color:transparent;background-color:var(--fill);}
  .controls input:focus,.controls select:focus{outline:3px solid var(--accent-ring);outline-offset:0;border-color:var(--accent);}
  .count{font-size:var(--fs-foot);color:var(--label-2);white-space:nowrap;}
  main{padding:var(--s-4) var(--s-6) var(--s-8);}
  table{width:100%;border-collapse:separate;border-spacing:0;font-size:var(--fs-ui);font-variant-numeric:tabular-nums;
        background:var(--surface);border:1px solid var(--separator);border-radius:var(--r-lg);box-shadow:var(--sh-1);}
  thead th{text-align:left;padding:0 var(--s-3);height:36px;font-size:var(--fs-foot);font-weight:600;color:var(--label-2);
           background:var(--surface-2);border-bottom:1px solid var(--separator);}
  thead th:first-child{border-top-left-radius:calc(var(--r-lg) - 1px);}
  thead th:last-child{border-top-right-radius:calc(var(--r-lg) - 1px);}
  tbody td{padding:var(--s-2) var(--s-3);vertical-align:top;border-bottom:1px solid var(--separator);}
  tbody tr:last-child > td{border-bottom:none;}
  tbody tr{cursor:pointer;}
  tbody tr:hover > td{background:var(--fill);}
  .badge{--c:var(--label-2);display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:var(--r-pill);
         font-size:var(--fs-foot);font-weight:500;white-space:nowrap;
         background:color-mix(in srgb,var(--c) 16%,transparent);color:color-mix(in srgb,var(--c) var(--ink-mix),var(--label));}
  .badge::before{content:"";flex:none;width:6px;height:6px;border-radius:50%;background:var(--c);}
  .site-link{display:inline-grid;place-content:center;width:18px;height:18px;margin-left:6px;vertical-align:-3px;border-radius:var(--r-sm);
             color:var(--label-3);text-decoration:none;}
  .site-link::before{content:"";width:12px;height:12px;background:currentColor;
                     -webkit-mask:var(--i-link) center/contain no-repeat;mask:var(--i-link) center/contain no-repeat;}
  .site-link:hover{color:var(--accent-ink);background:var(--accent-tint);}
  tbody tr.detail-row{cursor:default;}
  tbody tr.detail-row > td{padding:var(--s-5);background:var(--surface-2);}
  .detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--s-4);}
  .detail-grid h4{margin:0 0 6px;font-size:var(--fs-foot);font-weight:600;color:var(--label-2);}
  .body-preview{padding:var(--s-3);max-height:420px;overflow:auto;font-size:var(--fs-ui);background:var(--surface);
                border:1px solid var(--separator);border-radius:var(--r-md);}
  .body-text{padding:var(--s-3);max-height:420px;overflow:auto;white-space:pre-wrap;font:12px/1.5 var(--mono);background:var(--surface);
             border:1px solid var(--separator);border-radius:var(--r-md);}
  .error-box{margin-top:var(--s-2);font-size:var(--fs-foot);color:var(--danger);}
  .note{margin-top:var(--s-2);font-size:var(--fs-foot);}
  .empty-state{padding:var(--s-8) var(--s-4);text-align:center;color:var(--label-2);}
  .muted{color:var(--label-2);}
  td.muted{white-space:nowrap;font-size:var(--fs-foot);}
  code{font:12px/1.4 var(--mono);background:var(--fill);border-radius:5px;padding:1px 5px;}
  @media (max-width:760px){header{padding:var(--s-4) var(--s-3) var(--s-2);}main{padding:var(--s-3);}.detail-grid{grid-template-columns:1fr;}}
  @media (prefers-reduced-motion: reduce){*{transition:none !important;animation:none !important;}}
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
  <div class="empty-state" id="emptyState" hidden>Brak wierszy pasujących do filtrów.</div>
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
  return `<span class="badge" style="--c:${escapeHtml(info[1])}">${escapeHtml(info[0])}</span>`;
}

function countryCell(row){
  const info = COUNTRIES[row.country];
  if (!info) return row.country ? escapeHtml(row.country) : '<span class="muted">—</span>';
  const city = row.city ? ` <span class="muted">${escapeHtml(row.city)}</span>` : '';
  return `<span title="${escapeHtml(info[0])}">${info[1]} ${escapeHtml(row.country)}</span>${city}`;
}

function websiteLink(url){
  if (!url) return '';
  return `<a class="site-link" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${escapeHtml(url)}" aria-label="Otwórz stronę firmy"></a>`;
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
    return `<span class="chip" style="--c:${escapeHtml(info[1])}">${escapeHtml(info[0])}: ${n}</span>`;
  }).join('');
}

function renderBodies(row){
  if (!('body_html' in row)) {
    return `<div class="muted note">Ten podgląd jest bez treści maili${row.has_body ? '' : ' (ten wiersz i tak jej nie ma)'}.
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
    ${row.attachments ? `<div class="muted note">Załączniki/obrazy: ${escapeHtml(row.attachments)} ${escapeHtml(row.inline_images||'')}</div>` : ''}
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
  emptyState.hidden = visible.length > 0;
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
            .replace("/*__COUNTRIES__*/", countries_json).replace("/*__TOKENS__*/", read_tokens()))
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_path, output_path)
