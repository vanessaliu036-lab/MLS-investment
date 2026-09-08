"""個股卡片快取的籌碼新鮮度回歸測試。

卡片快取以交易日為 key、當天只算一次，但籌碼四條來源鏈是當天稍晚才陸續定案。
2026-09-08 首頁的籌碼顧問卡就因此一直寫著「籌碼資料日 2026-09-04」。
"""

import json
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import extras  # noqa: E402


def _prepare(tmp_path, cached_chip_date, live_chip_date):
    (tmp_path / "chips_cache.json").write_text(json.dumps({"stocks": {
        "4979": {"source_date": live_chip_date},
        "detail:4979": {"margin_source_date": live_chip_date,
                        "lending_source_date": live_chip_date},
    }}), encoding="utf-8")
    card_dir = tmp_path / "card_cache"
    card_dir.mkdir()
    (card_dir / "2026-09-08_4979.json").write_text(json.dumps({
        "_card_cache_version": extras._CARD_CACHE_VERSION,
        "ok": True,
        "card": {"chip": {"source_date": cached_chip_date,
                          "margin_source_date": cached_chip_date,
                          "lending_source_date": cached_chip_date}},
    }), encoding="utf-8")
    return card_dir


def _patch(monkeypatch, tmp_path, card_dir):
    monkeypatch.setattr(extras, "_BASE", tmp_path)
    monkeypatch.setattr(extras, "_CARD_DIR", card_dir)
    monkeypatch.setattr(extras, "_CARD_MEM", {})
    monkeypatch.setattr(extras, "_CHIP_DATE_CACHE", {"mtime": None, "map": {}})


def test_card_cache_drops_when_chip_source_date_advances(monkeypatch, tmp_path):
    card_dir = _prepare(tmp_path, "2026-09-04", "2026-09-07")
    _patch(monkeypatch, tmp_path, card_dir)

    assert extras._card_cache_read("4979", "2026-09-08") is None


def test_card_cache_kept_when_chip_source_date_matches(monkeypatch, tmp_path):
    card_dir = _prepare(tmp_path, "2026-09-07", "2026-09-07")
    _patch(monkeypatch, tmp_path, card_dir)

    hit = extras._card_cache_read("4979", "2026-09-08")
    assert hit is not None
    assert hit["card"]["chip"]["margin_source_date"] == "2026-09-07"


def test_memory_hit_also_checks_chip_freshness(monkeypatch, tmp_path):
    card_dir = _prepare(tmp_path, "2026-09-04", "2026-09-07")
    _patch(monkeypatch, tmp_path, card_dir)
    monkeypatch.setattr(extras, "_CARD_MEM", {"2026-09-08_4979": {
        "card": {"chip": {"source_date": "2026-09-04"}}}})

    assert extras._card_cache_read("4979", "2026-09-08") is None
