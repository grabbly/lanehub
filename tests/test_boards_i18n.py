import json
from pathlib import Path
from app.boards.store import LANGS

I18N_DIR = Path(__file__).resolve().parents[1] / "app" / "boards" / "i18n"


def test_i18n_keys_match():
    dicts = {}
    for lang in LANGS:
        file_path = I18N_DIR / f"{lang}.json"
        assert file_path.exists(), f"Missing i18n file for {lang}"
        with open(file_path, "r", encoding="utf-8") as f:
            dicts[lang] = json.load(f)

    base_lang = LANGS[0]
    base_keys = set(dicts[base_lang].keys())

    for lang in LANGS[1:]:
        keys = set(dicts[lang].keys())
        diff_missing = base_keys - keys
        diff_extra = keys - base_keys
        assert not diff_missing, f"{lang} is missing keys: {diff_missing}"
        assert not diff_extra, f"{lang} has extra keys: {diff_extra}"
