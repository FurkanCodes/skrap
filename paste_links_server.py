from __future__ import annotations

import argparse
import html
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from price_scraper import SUPPORTED_SITES, parse_sites_blob, parse_urls_blob, scrape_prices


HTML_PAGE = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Product Price Scraper</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f4f6fb;
        --panel: #ffffff;
        --text: #1d2330;
        --muted: #5a6272;
        --accent: #0f6fff;
        --ok: #177245;
        --err: #b42318;
        --border: #dde3ee;
      }

      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
        background: radial-gradient(circle at top right, #dfe9ff 0%, var(--bg) 45%);
        color: var(--text);
      }

      main {
        max-width: 980px;
        margin: 40px auto;
        padding: 0 20px;
      }

      .panel {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 16px;
        box-shadow: 0 10px 25px rgba(12, 27, 56, 0.08);
        padding: 22px;
      }

      h1 {
        margin-top: 0;
        margin-bottom: 8px;
        font-size: 1.65rem;
      }

      p { margin-top: 0; color: var(--muted); }

      textarea {
        width: 100%;
        min-height: 180px;
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px;
        font-size: 0.95rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        resize: vertical;
      }

      button {
        margin-top: 14px;
        background: var(--accent);
        color: #fff;
        border: 0;
        border-radius: 10px;
        padding: 10px 16px;
        font-size: 0.95rem;
        font-weight: 600;
        cursor: pointer;
      }

      .results {
        margin-top: 20px;
        overflow-x: auto;
      }

      table {
        width: 100%;
        border-collapse: collapse;
      }

      th, td {
        text-align: left;
        vertical-align: top;
        border-bottom: 1px solid var(--border);
        padding: 10px 8px;
        font-size: 0.92rem;
      }

      th {
        color: var(--muted);
        font-weight: 600;
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.02em;
      }

      .status-ok { color: var(--ok); font-weight: 600; }
      .status-err { color: var(--err); font-weight: 600; }
    </style>
  </head>
  <body>
    <main>
      <section class=\"panel\">
        <h1>Product Price Scraper</h1>
        <p>Paste product URLs (one per line), submit, and we will fetch all visible listings/prices for each URL.</p>
        <form method=\"post\" action=\"/\">
          <textarea name=\"urls\" placeholder=\"https://example.com/product-1\nhttps://example.com/product-2\">{urls_text}</textarea>
          <br />
          <input
            type=\"text\"
            name=\"sites\"
            placeholder=\"all (or: akakce,newegg,generic)\"
            value=\"{sites_text}\"
            style=\"margin-top:12px;width:100%;border:1px solid var(--border);border-radius:10px;padding:10px 12px;font-size:0.92rem;\"
          />
          <p style=\"margin:8px 0 0 0;font-size:0.82rem;\">Supported site keys: {supported_sites}</p>
          <br />
          <button type=\"submit\">Fetch Prices</button>
        </form>
        {results_block}
      </section>
    </main>
  </body>
</html>
"""


def _render_results(results: list[dict], error_message: str = "") -> str:
    if not results:
        if not error_message:
            return ""
        return f'<p class="status-err" style="margin-top:12px;">{html.escape(error_message)}</p>'

    rows = []
    for result in results:
        if result.get("success") and result.get("listings"):
            listings = result.get("listings") or []
            for idx, listing in enumerate(listings, 1):
                rows.append(
                    """
                    <tr>
                      <td>{url}</td>
                      <td>{product_id}</td>
                      <td>{idx}</td>
                      <td>{item_id}</td>
                      <td>{amount}</td>
                      <td>{currency}</td>
                      <td>{seller}</td>
                      <td>{listing_url}</td>
                      <td>{source}</td>
                      <td class=\"status-ok\">ok</td>
                    </tr>
                    """.format(
                        url=html.escape(result.get("url") or "-") if idx == 1 else "",
                        product_id=html.escape(result.get("product_id") or "-") if idx == 1 else "",
                        idx=idx,
                        item_id=html.escape(listing.get("item_id") or "-"),
                        amount=html.escape(listing.get("amount") or "-"),
                        currency=html.escape(listing.get("currency") or "-"),
                        seller=html.escape(listing.get("seller") or "-"),
                        listing_url=html.escape(listing.get("listing_url") or "-"),
                        source=html.escape(listing.get("source") or "-"),
                    )
                )
            continue

        status_text = result.get("error") or "failed"
        rows.append(
            """
            <tr>
              <td>{url}</td>
              <td>{product_id}</td>
              <td>-</td>
              <td>-</td>
              <td>-</td>
              <td>-</td>
              <td>-</td>
              <td>-</td>
              <td>-</td>
              <td class=\"status-err\">{status}</td>
            </tr>
            """.format(
                url=html.escape(result.get("url") or "-"),
                product_id=html.escape(result.get("product_id") or "-"),
                status=html.escape(status_text),
            )
        )

    table = """
    <div class=\"results\">
      {error_html}
      <table>
        <thead>
          <tr>
            <th>URL</th>
            <th>Product ID</th>
            <th>#</th>
            <th>Item ID</th>
            <th>Amount</th>
            <th>Currency</th>
            <th>Seller</th>
            <th>Listing URL</th>
            <th>Source</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
    """.format(
        rows="\n".join(rows),
        error_html=(f'<p class="status-err">{html.escape(error_message)}</p>' if error_message else ""),
    )

    return table


class PriceHandler(BaseHTTPRequestHandler):
    def _send_html(self, page: str) -> None:
        payload = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        page = HTML_PAGE.format(
            urls_text="",
            sites_text="",
            supported_sites=", ".join(sorted(SUPPORTED_SITES)),
            results_block="",
        )
        self._send_html(page)

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="ignore")
        data = parse_qs(raw_body)

        urls_text = data.get("urls", [""])[0]
        sites_text = data.get("sites", [""])[0]
        urls = parse_urls_blob(urls_text)
        error_message = ""

        try:
            selected_sites = parse_sites_blob(sites_text)
        except ValueError as exc:
            selected_sites = set()
            error_message = str(exc)

        selected_sites_or_none = selected_sites if selected_sites else None
        results = [asdict(item) for item in scrape_prices(urls, selected_sites=selected_sites_or_none)]

        page = HTML_PAGE.format(
            urls_text=html.escape(urls_text),
            sites_text=html.escape(sites_text),
            supported_sites=", ".join(sorted(SUPPORTED_SITES)),
            results_block=_render_results(results, error_message=error_message),
        )
        self._send_html(page)


def run_server(host: str, port: int) -> None:
    server = HTTPServer((host, port), PriceHandler)
    print(f"Serving paste-links UI at http://{host}:{port}")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local paste-links server for product price scraping.")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    args = parser.parse_args()

    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
