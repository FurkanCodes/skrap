from __future__ import annotations

import re

CURRENCY_TOKEN_TO_CODE = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "¥": "JPY",
    "JPY": "JPY",
    "₺": "TRY",
    "TL": "TRY",
    "TRY": "TRY",
    "₹": "INR",
    "INR": "INR",
    "₽": "RUB",
    "RUB": "RUB",
    "₩": "KRW",
    "KRW": "KRW",
    "AED": "AED",
    "SAR": "SAR",
    "QAR": "QAR",
    "KWD": "KWD",
    "BHD": "BHD",
    "OMR": "OMR",
    "CAD": "CAD",
    "AUD": "AUD",
    "NZD": "NZD",
    "CHF": "CHF",
    "CNY": "CNY",
    "HKD": "HKD",
    "SGD": "SGD",
    "BRL": "BRL",
    "MXN": "MXN",
    "ZAR": "ZAR",
    "PLN": "PLN",
    "NOK": "NOK",
    "SEK": "SEK",
    "DKK": "DKK",
}

CURRENCY_TOKEN_PATTERN = "|".join(
    sorted((re.escape(token) for token in CURRENCY_TOKEN_TO_CODE), key=len, reverse=True)
)

PRICE_PATTERN = re.compile(
    rf"(?P<currency_before>{CURRENCY_TOKEN_PATTERN})?\s*"
    r"(?P<amount>(?:\d{1,3}(?:[\s.,]\d{3})+|\d+)(?:[.,]\d{2})?)"
    rf"\s*(?P<currency_after>{CURRENCY_TOKEN_PATTERN})?",
    re.IGNORECASE,
)

META_PRICE_SELECTORS = [
    ("meta[itemprop='price']::attr(content)", "meta[itemprop=price]"),
    ("meta[property='product:price:amount']::attr(content)", "meta[property=product:price:amount]"),
    ("meta[property='og:price:amount']::attr(content)", "meta[property=og:price:amount]"),
    ("meta[property='twitter:data1']::attr(content)", "meta[property=twitter:data1]"),
]

META_CURRENCY_SELECTORS = [
    "meta[itemprop='priceCurrency']::attr(content)",
    "meta[property='product:price:currency']::attr(content)",
    "meta[property='og:price:currency']::attr(content)",
]

DOM_PRICE_SELECTORS = [
    "[itemprop='price']",
    "[data-price]",
    "[data-testid*='price']",
    ".a-price .a-offscreen",
    ".price-current",
    ".product-price",
    ".sale-price",
    ".our-price",
    ".price",
    "[class*='price']",
    "[id*='price']",
]

FIND_PRICE_ATTRIBUTE_FILTERS: list[dict[str, str]] = [
    {"itemprop": "price"},
    {"class*": "price"},
    {"id*": "price"},
    {"data-testid*": "price"},
    {"data-qa*": "price"},
    {"data-price*": ""},
    {"aria-label*": "price"},
]

FIND_TEXT_PRICE_REGEX = re.compile(
    rf"(?:{CURRENCY_TOKEN_PATTERN})\s*\d[\d\s.,]*|\d[\d\s.,]*\s*(?:{CURRENCY_TOKEN_PATTERN})",
    re.IGNORECASE,
)

SKIPPED_FIND_REGEX_TAGS = {
    "html",
    "body",
    "main",
    "section",
    "article",
    "ul",
    "ol",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
}

SUPPORTED_FETCH_MODES: dict[str, str] = {
    "auto": "Start with HTTP Fetcher, escalate to browser fetchers when needed",
    "http": "Use HTTP Fetcher only",
    "dynamic": "Use DynamicFetcher only",
    "stealth": "Use StealthyFetcher only",
}

PROTECTION_STATUS_CODES = {401, 403, 429, 503}

JAVASCRIPT_HINTS = (
    "enable javascript",
    "javascript is required",
    "please turn javascript",
    "requires javascript",
)

PROTECTION_HINTS = (
    "access denied",
    "attention required",
    "bot detection",
    "captcha required",
    "captcha verification",
    "complete the captcha",
    "solve captcha",
)

CLOUDFLARE_HINTS = (
    "cloudflare",
    "turnstile",
    "cf-chl",
    "cf_clearance",
    "checking your browser",
    "verify you are human",
)

MAX_DOM_ELEMENTS = 300
MAX_FIND_ELEMENTS = 200
MAX_PAGE_TEXT_CANDIDATES = 200
