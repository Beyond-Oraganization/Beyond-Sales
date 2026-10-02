"""Odmiana Pan/Pani/Państwo — przeniesione 1:1 z FORMS w Maile_GTM_Eventy/index.html."""

FORMS = {
    "Pan": {
        "chcial": "chciałby Pan",
        "dop": "Pana",
        "musi": "Nie musi już Pan",
        "pyta": "Chciałby Pan",
    },
    "Pani": {
        "chcial": "chciałaby Pani",
        "dop": "Pani",
        "musi": "Nie musi już Pani",
        "pyta": "Chciałaby Pani",
    },
    "Panstwo": {
        "chcial": "chcieliby Państwo",
        "dop": "Państwa",
        "musi": "Nie muszą już Państwo",
        "pyta": "Chcieliby Państwo",
    },
}

SIGNATURE_TOKEN = "[Imię i nazwisko]"


def conjugate(mode: str) -> dict:
    return FORMS.get(mode, FORMS["Panstwo"])
