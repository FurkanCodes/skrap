from __future__ import annotations

"""Shared availability normalization across locales/languages."""

import re
import unicodedata

_CANONICAL_AVAILABILITY_VALUES = {"in_stock", "out_of_stock", "preorder", "discontinued"}

_OUT_OF_STOCK_MARKERS = (
    # English
    "outofstock",
    "out of stock",
    "currently unavailable",
    "temporarily out of stock",
    "sold out",
    "not in stock",
    "not available",
    "no longer available",
    "unavailable",
    # Turkish
    "stokta yok",
    "stokta bulunmuyor",
    "mevcut degil",
    "su anda mevcut degil",
    "tukendi",
    "satista degil",
    "satisi yok",
    # Spanish
    "agotado",
    "sin stock",
    "sin existencias",
    "no disponible",
    "actualmente no disponible",
    "temporalmente sin stock",
    "temporalmente agotado",
    "fuera de stock",
    "fuera de existencias",
    "no hay stock",
    # French
    "rupture de stock",
    "en rupture de stock",
    "indisponible",
    "actuellement indisponible",
    "epuise",
    "plus en stock",
    # German
    "nicht auf lager",
    "nicht verfugbar",
    "derzeit nicht verfugbar",
    "ausverkauft",
    "nicht lieferbar",
    # Italian
    "non disponibile",
    "temporaneamente non disponibile",
    "esaurito",
    # Portuguese
    "esgotado",
    "sem estoque",
    "indisponivel",
    "temporariamente indisponivel",
    "fora de estoque",
    # Dutch
    "niet op voorraad",
    "uitverkocht",
    "tijdelijk niet beschikbaar",
    # Polish
    "brak w magazynie",
    "chwilowo niedostepny",
    "wyprzedany",
    # Nordic
    "slut i lager",
    "inte tillganglig",
    "utsolgt",
    "ikke pa lager",
    "udsolgt",
    "loppu varastosta",
    "ei varastossa",
    # Central/Eastern Europe
    "stoc epuizat",
    "indisponibil",
    "nincs keszleten",
    "elfogyott",
    "nema na stanju",
    # Cyrillic languages
    "нет в наличии",
    "распродано",
    "временно нет в наличии",
    "немає в наявності",
    "розпродано",
    # Asian languages
    "缺货",
    "無貨",
    "无货",
    "暂无库存",
    "在庫切れ",
    "在庫なし",
    "품절",
    "재고 없음",
    "หมดสต็อก",
    "สินค้าหมด",
    "hết hàng",
    "tam het hang",
    # Arabic / Hebrew / Hindi
    "غير متوفر",
    "نفدت الكمية",
    "אזל מהמלאי",
    "לא זמין",
    "स्टॉक में नहीं",
    "उपलब्ध नहीं",
    # Indonesian / Malay
    "stok habis",
    "tidak tersedia",
)

_IN_STOCK_MARKERS = (
    "instock",
    "in stock",
    "available to ship",
    "available now",
    "ready to ship",
    "stokta var",
    "stokta mevcut",
    "en stock",
    "disponible",
    "hay existencias",
    "auf lager",
    "verfugbar",
    "sofort lieferbar",
    "em estoque",
    "op voorraad",
    "dostepny",
    "в наличии",
    "в наявності",
    "有货",
    "有貨",
    "现货",
    "有库存",
    "在庫あり",
    "재고 있음",
    "재고있음",
    "متوفر",
    "במלאי",
    "स्टॉक में",
    "stok tersedia",
    "con hang",
)

_PREORDER_MARKERS = (
    "pre-order",
    "preorder",
    "stoklara gelince",
    "on siparis",
    "reserva",
    "preventa",
    "pedido anticipado",
    "sur commande",
    "precommande",
    "vorbestellung",
    "preordine",
    "pre-venda",
    "dostepne w przedsprzedazy",
    "предзаказ",
    "попереднє замовлення",
    "予約注文",
    "预售",
    "預售",
    "예약 구매",
)

_DISCONTINUED_MARKERS = (
    "discontinued",
    "production discontinued",
    "uretimi durduruldu",
    "descatalogado",
    "descontinuado",
    "discontinuado",
    "arrete",
    "eingestellt",
    "fuori produzione",
    "fora de linha",
    "wycofany",
    "прекращено",
    "знято з виробництва",
    "生産終了",
    "已停产",
    "단종",
)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _normalize_for_lookup(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[_\-]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    folded = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


_OUT_OF_STOCK_MARKERS_NORMALIZED = tuple(_normalize_for_lookup(marker) for marker in _OUT_OF_STOCK_MARKERS)
_IN_STOCK_MARKERS_NORMALIZED = tuple(_normalize_for_lookup(marker) for marker in _IN_STOCK_MARKERS)
_PREORDER_MARKERS_NORMALIZED = tuple(_normalize_for_lookup(marker) for marker in _PREORDER_MARKERS)
_DISCONTINUED_MARKERS_NORMALIZED = tuple(_normalize_for_lookup(marker) for marker in _DISCONTINUED_MARKERS)


def normalize_availability(value: str | None) -> str | None:
    """Return canonical availability when recognized, else cleaned original text."""
    cleaned = _clean_text(value)
    if not cleaned:
        return None

    normalized = _normalize_for_lookup(cleaned)

    if normalized.endswith("/outofstock") or any(
        marker in normalized for marker in _OUT_OF_STOCK_MARKERS_NORMALIZED
    ):
        return "out_of_stock"
    if normalized.endswith("/preorder") or any(marker in normalized for marker in _PREORDER_MARKERS_NORMALIZED):
        return "preorder"
    if normalized.endswith("/discontinued") or any(
        marker in normalized for marker in _DISCONTINUED_MARKERS_NORMALIZED
    ):
        return "discontinued"
    if normalized.endswith("/instock") or any(marker in normalized for marker in _IN_STOCK_MARKERS_NORMALIZED):
        return "in_stock"
    return cleaned


def is_canonical_availability(value: str | None) -> bool:
    return value in _CANONICAL_AVAILABILITY_VALUES
