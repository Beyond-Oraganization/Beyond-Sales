function escapeHtml(s){
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function sleep(ms){ return new Promise(resolve => setTimeout(resolve, ms)); }

async function readJson(res){
  try { return await res.json(); } catch (err) { return {}; }
}

// Każde POST musi iść z Content-Type: application/json — serwer odrzuca resztę (ochrona przed CSRF).
async function api(path, payload){
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  return {ok: res.ok, status: res.status, data: await readJson(res)};
}

async function apiGet(path){
  const res = await fetch(path);
  return {ok: res.ok, status: res.status, data: await readJson(res)};
}

// Eksport wskazanych wierszy: POST z listą id → plik zapisywany przez przeglądarkę (nazwa z Content-Disposition).
// Zwraca {ok, name, rows, size} albo {ok: false, error}.
async function downloadExport(payload){
  const res = await fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok){
    const data = await readJson(res);
    return {ok: false, status: res.status, error: data.error || 'Nie udało się przygotować pliku.'};
  }
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
  const name = match ? match[1] : 'Maile.' + (payload && payload.format || 'csv');
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return {ok: true, name, rows: Number(res.headers.get('X-Export-Rows') || 0), size: blob.size};
}
