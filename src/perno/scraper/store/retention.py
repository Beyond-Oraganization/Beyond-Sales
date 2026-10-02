"""Retencja danych skanu (RODO: minimalizacja przechowywania)."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from perno.scraper import statuses as st
from perno.scraper.schema import firmy, strony, strony_tresc
from perno.scraper.store.companies import CompanyStore
from perno.scraper.store.errors import FirmaBusy


class RetentionStore(CompanyStore):
    def purge_older_than(self, days: int) -> int:
        """Usuwa dane skanu firm zakończonych przed `days` dniami (RODO: minimalizacja przechowywania)."""
        cutoff = self.db.now() - timedelta(days=days)
        with self._read() as conn:
            ids = [r.id for r in conn.execute(sa.select(firmy.c.id).where(
                firmy.c.skan_end_at.is_not(None), firmy.c.skan_end_at < cutoff,
                firmy.c.skan_status != st.FIRMA_SKANOWANIE))]
        purged = 0
        for firma_id in ids:
            try:
                self.delete_firma_data(firma_id)
                purged += 1
            except FirmaBusy:
                continue
        return purged

    def purge_html(self, days: int) -> int:
        """Kasuje tylko zapisany surowy HTML (tekst i kontakty zostają) ze stron pobranych przed `days` dniami."""
        cutoff = self.db.now() - timedelta(days=days)
        with self._tx() as conn:
            old = sa.select(strony.c.id).where(strony.c.pobrano_at.is_not(None), strony.c.pobrano_at < cutoff)
            result = conn.execute(sa.update(strony_tresc).where(
                strony_tresc.c.html_gz.is_not(None), strony_tresc.c.strona_id.in_(old)).values(html_gz=None))
            return result.rowcount
