"""Quick diagnostic: check what data has been persisted to Supabase.

Usage:
    python3 -m smart_trader.scripts.check_supabase
"""
import sys

import requests

from smart_trader.settings.credentials import load_credentials


def _get(url, headers, path, params=None):
    resp = requests.get(f"{url}/rest/v1/{path}", headers=headers, params=params or {}, timeout=15)
    return resp


def main():
    creds = load_credentials()
    url = creds.get("supabase_url", "")
    key = creds.get("supabase_key", "")

    if not url or not key:
        print("ERROR: SUPABASE_URL or SUPABASE_KEY not set in .env")
        sys.exit(1)

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    print("=" * 60)
    print("Supabase Data Check")
    print("=" * 60)

    # --- 1. smart_money_filings ---
    print("\n1. smart_money_filings")
    print("-" * 50)
    resp = _get(url, headers, "smart_money_filings", {"select": "source", "limit": "10000"})
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            from collections import Counter
            counts = Counter(r["source"] for r in rows)
            print(f"   Total rows: {len(rows)}")
            for src, cnt in sorted(counts.items()):
                print(f"   {src}: {cnt}")
        else:
            print("   (empty — 0 rows)")
    elif resp.status_code == 404:
        print("   TABLE NOT FOUND")
    else:
        print(f"   Error {resp.status_code}: {resp.text[:200]}")

    # --- 2. smart_money_candidates ---
    print("\n2. smart_money_candidates")
    print("-" * 50)
    resp = _get(url, headers, "smart_money_candidates", {
        "select": "symbol,conviction_score,sources,generated_at",
        "order": "generated_at.desc,conviction_score.desc",
        "limit": "50",
    })
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            latest_ts = rows[0].get("generated_at", "?")
            batch = [r for r in rows if r.get("generated_at") == latest_ts]
            print(f"   Latest batch: {latest_ts}")
            print(f"   Candidates in batch: {len(batch)}")
            for r in batch[:10]:
                print(f"     {r['symbol']:6s}  conviction={r['conviction_score']:.2f}  sources={r.get('sources')}")
            if len(batch) > 10:
                print(f"     ... and {len(batch) - 10} more")
        else:
            print("   (empty — 0 rows)")
    elif resp.status_code == 404:
        print("   TABLE NOT FOUND")
    else:
        print(f"   Error {resp.status_code}: {resp.text[:200]}")

    # --- 3. portfolio_stocks ---
    print("\n3. portfolio_stocks")
    print("-" * 50)
    resp = _get(url, headers, "portfolio_stocks", {
        "select": "symbol,rank,in_top_n,composite_score,overlap_count,generated_at",
        "order": "generated_at.desc,rank.asc",
        "limit": "200",
    })
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            latest_ts = rows[0].get("generated_at", "?")
            batch = [r for r in rows if r.get("generated_at") == latest_ts]
            top_n = [r for r in batch if r.get("in_top_n")]
            print(f"   Latest snapshot: {latest_ts}")
            print(f"   Total stocks: {len(batch)}")
            print(f"   In top-N: {len(top_n)}")
            for r in top_n[:10]:
                print(
                    f"     #{r['rank']:3d}  {r['symbol']:6s}  "
                    f"score={r['composite_score']:.4f}  overlap={r['overlap_count']}"
                )
            if len(top_n) > 10:
                print(f"     ... and {len(top_n) - 10} more")
        else:
            print("   (empty — 0 rows)")
    elif resp.status_code == 404:
        print("   TABLE NOT FOUND")
    else:
        print(f"   Error {resp.status_code}: {resp.text[:200]}")

    # --- 4. ohlcv_bars ---
    print("\n4. ohlcv_bars")
    print("-" * 50)
    resp = _get(url, headers, "ohlcv_bars", {"select": "symbol", "limit": "5000"})
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            symbols = sorted(set(r["symbol"] for r in rows))
            print(f"   Total rows: {len(rows)}")
            print(f"   Distinct symbols: {len(symbols)}")
            print(f"   Symbols: {', '.join(symbols[:30])}")
            if len(symbols) > 30:
                print(f"   ... and {len(symbols) - 30} more")
        else:
            print("   (empty — 0 rows)")
    elif resp.status_code == 404:
        print("   TABLE NOT FOUND")
    else:
        print(f"   Error {resp.status_code}: {resp.text[:200]}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
