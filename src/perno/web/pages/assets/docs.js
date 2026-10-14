// Podstrony z dokumentacją: treść i spis są już w HTML (serwer renderuje Markdown), tu tylko kopiowanie i zachowanie spisu na wąskim ekranie.

bindCopyButtons();  // przycisk z nazwą pliku kopiuje odwołanie dla Claude Code

// Przycisk „Kopiuj” przy blokach kodu jest w HTML ukryty, bo bez skryptu nic by nie robił.
document.querySelectorAll('[data-copy-code]').forEach((button) => button.removeAttribute('hidden'));
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-copy-code]');
  if (!button) return;
  const code = button.closest('.doc-code').querySelector('code');
  copyWithToast(code.textContent.replace(/\n$/, ''), 'Skopiowano blok kodu.');
});

// Spis treści: rozwinięty na szerokim ekranie (stoi obok treści), zwinięty na wąskim (zajmowałby ekran przed treścią).
const docToc = document.getElementById('docToc');
if (docToc && window.matchMedia) {
  const wide = window.matchMedia('(min-width: 980px)');
  const syncToc = () => { docToc.open = wide.matches; };
  syncToc();
  wide.addEventListener('change', syncToc);
}
