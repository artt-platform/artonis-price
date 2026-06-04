"""Lê Auction House — leauctions.vn (Hà Nội)

STATUS: Not yet implementable as automated crawler.

Why:
- WordPress shell at leauctions.vn renders only minimal lot metadata server-side
  (og:title gives "Lot 23 - LƯU CÔNG NHÂN (1930-2007) Chị Mĩ"; no price/dims/medium).
- All actual price/estimate/hammer data lives in the Bidspirit backend
  (leauction.bidspirit.com) — an Angular SPA with authenticated API endpoints
  not exposed via public REST.
- lot-sitemap1.xml lists 684 lot URLs but each only resolves to a stub WP page.

Options for the future:
  1. Playwright + login (free Bidspirit account) to scrape rendered lot pages
  2. Reverse-engineer Bidspirit's auth + WebSocket protocol
  3. Manual export from Le Auction (request bulk data dump)

For now: skipped from automated workflow. Their VN art auctions are however
covered in part by sothebys / christies / phillips (where the same lots may
have appeared at international houses).

Featured artists at Lê Auction (from public archive titles):
  Lê Phổ, Mai Trung Thứ, Bùi Xuân Phái, Vũ Cao Đàm, Nguyễn Phan Chánh,
  Trần Văn Cẩn, Tô Ngọc Vân, Lê Văn Đệ, Nguyễn Gia Trí, Lưu Công Nhân.
"""

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=False, max_pages=200):
    """Stub — see module docstring for why this isn't implemented yet."""
    if verbose:
        print("[le_auction] skipped — site requires Bidspirit auth (see module docstring)")
    return 0, 0
