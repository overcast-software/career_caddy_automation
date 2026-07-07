#!/usr/bin/env python3
"""Reconcile the fediverse-published set of MY job posts to a CSV keep-list.

Two steps, in order:
  1. UNPUBLISH every currently-published post of mine that is NOT in the CSV.
  2. PUBLISH every post in the CSV (idempotent; already-public ones no-op).

Owner-scoped: `meta.published` on the list endpoint is only true for posts the
caller owns, so "all other jps" naturally means *my own* other public posts —
other users' posts are never touched.

Auth + base-url come from automation/.env (CC_API_TOKEN jh_* key +
CC_API_BASE_URL), exactly like the other cc_auto scripts. Uses the cc_auto
ApiClient so the Bearer-auth scheme is correct.

Dry-run by default. Run from the automation repo so `src` is importable:

    uv run --directory <parent>/automation python scripts/publish_from_csv.py
    uv run --directory <parent>/automation python scripts/publish_from_csv.py --apply
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.client.api_client import ApiClient

PUBLISH = "/api/v1/job-posts/{id}/publish/"
UNPUBLISH = "/api/v1/job-posts/{id}/unpublish/"


def load_keep_ids(csv_path: str) -> list[str]:
    ids: list[str] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            jid = (row.get("id") or "").strip()
            if jid and jid.upper() != "NULL":
                ids.append(jid)
    return list(dict.fromkeys(ids))  # dedupe, preserve order


async def fetch_state(api: ApiClient, per_page: int) -> tuple[set[str], set[str]]:
    """Page the FULL job-post universe. Return (all_ids, my_published_ids).

    Two non-obvious requirements, both load-bearing for correctness:
      * include_closed=true -- the default list view EXCLUDES
        posting_status="closed" (jobs.py get_queryset). A closed *and*
        published post not in the keep-list must still be unpublishable, so
        the scan has to see closed posts.
      * sort=id -- the default queryset has no stable ordering, so LIMIT/OFFSET
        paging over 20 pages can skip or repeat rows. A unique key makes paging
        deterministic.
    The caller asserts len(all_ids) == server total before any mutation.
    """
    all_ids: set[str] = set()
    published: set[str] = set()
    page = 1
    total_pages = None
    server_total = None
    base_params = {"per_page": per_page, "include_closed": "true", "sort": "id"}
    while True:
        raw = await api.get("/api/v1/job-posts/", params={**base_params, "page": page})
        ret = json.loads(raw)
        if not ret.get("success"):
            raise RuntimeError(f"list page {page} failed: {ret.get('error')}")
        doc = ret["data"]
        items = doc.get("data", []) or []
        meta = doc.get("meta", {}) or {}
        if total_pages is None:
            total_pages = meta.get("total_pages")
            server_total = meta.get("total")
            print(
                f"  server: {server_total} posts / {total_pages} pages "
                f"(per_page={meta.get('per_page')}, include_closed, sort=id)"
            )
        for it in items:
            jid = it.get("id")
            if not jid:
                continue
            all_ids.add(jid)
            if (it.get("meta") or {}).get("published") is True:
                published.add(jid)
        if not items or (total_pages and page >= total_pages):
            break
        page += 1

    print(f"  scanned {len(all_ids)} unique ids (server total {server_total})")
    if server_total is not None and len(all_ids) != server_total:
        raise RuntimeError(
            f"INCOMPLETE SCAN: collected {len(all_ids)} unique ids but server "
            f"reports {server_total}. Refusing -- pagination dropped rows."
        )
    return all_ids, published


async def act(api: ApiClient, tmpl: str, ids: list[str], concurrency: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    results: dict = {"ok": [], "fail": []}
    done = 0
    total = len(ids)

    async def one(jid: str):
        nonlocal done
        async with sem:
            ret = json.loads(await api.post(tmpl.format(id=jid), {}))
            if ret.get("success"):
                results["ok"].append(jid)
            else:
                results["fail"].append((jid, ret.get("status_code"), ret.get("error")))
            done += 1
            if done % 50 == 0 or done == total:
                print(f"    {done}/{total}")

    await asyncio.gather(*(one(j) for j in ids))
    return results


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(Path.home() / "Sandbox/job-posts-to-publish.csv"))
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--per-page", type=int, default=200)
    ap.add_argument(
        "--unpublish-ids",
        default="",
        help="comma-separated ids to unpublish directly (targeted retry; skips scan)",
    )
    ap.add_argument(
        "--publish-ids",
        default="",
        help="comma-separated ids to publish directly (targeted retry; skips scan)",
    )
    args = ap.parse_args()

    load_dotenv()
    token = os.environ.get("CC_API_TOKEN")
    base_url = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")
    if not token:
        sys.exit("CC_API_TOKEN not set (jh_* key). Source automation/.env first.")
    api = ApiClient(base_url, token)
    print(f"target: {base_url}")

    # Targeted retry mode: hit only the given ids, no scan, no keep-list diff.
    if args.unpublish_ids or args.publish_ids:
        un = [s.strip() for s in args.unpublish_ids.split(",") if s.strip()]
        pub = [s.strip() for s in args.publish_ids.split(",") if s.strip()]
        print(f"TARGETED retry: unpublish {len(un)}, publish {len(pub)}")
        if not args.apply:
            print("DRY RUN -- re-run with --apply.")
            return
        if un:
            r = await act(api, UNPUBLISH, un, args.concurrency)
            print(f"  unpublish: ok={len(r['ok'])} fail={len(r['fail'])}")
            for jid, sc, err in r["fail"]:
                print(f"    FAIL {jid} [{sc}] {err}")
        if pub:
            r = await act(api, PUBLISH, pub, args.concurrency)
            print(f"  publish: ok={len(r['ok'])} fail={len(r['fail'])}")
            for jid, sc, err in r["fail"]:
                print(f"    FAIL {jid} [{sc}] {err}")
        print("DONE (targeted)")
        return

    keep = load_keep_ids(args.csv)
    keep_set = set(keep)
    print(f"keep-list: {len(keep)} ids <- {args.csv}")

    print("scanning current published state...")
    all_ids, published = await fetch_state(api, args.per_page)
    print(f"  my currently-published: {len(published)}")

    missing = sorted(keep_set - all_ids)
    to_publish = [k for k in keep if k in all_ids]
    already_pub = keep_set & published
    to_unpublish = sorted(published - keep_set)
    new_publishes = [k for k in to_publish if k not in published]

    print("\nPLAN")
    print(f"  unpublish (mine, public, not in keep) : {len(to_unpublish)}")
    print(
        f"  publish   (in keep, visible)          : {len(to_publish)}"
        f"  [{len(already_pub)} already public no-op, "
        f"{len(new_publishes)} NEW -> fans out a Create]"
    )
    if missing:
        shown = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
        print(f"  SKIP (keep-id not visible/owned)      : {len(missing)}  [{shown}]")
    if to_unpublish:
        print(f"  e.g. unpublish: {', '.join(to_unpublish[:8])}")
    if new_publishes:
        print(f"  e.g. new-pub  : {', '.join(new_publishes[:8])}")

    if not args.apply:
        print("\nDRY RUN -- no changes made. Re-run with --apply to execute.")
        return

    print(f"\nAPPLY -> {base_url}")
    print(f"step 1/2: unpublishing {len(to_unpublish)}...")
    u = await act(api, UNPUBLISH, to_unpublish, args.concurrency)
    print(f"  unpublish: ok={len(u['ok'])} fail={len(u['fail'])}")
    for jid, sc, err in u["fail"][:20]:
        print(f"    FAIL {jid} [{sc}] {err}")

    print(f"step 2/2: publishing {len(to_publish)}...")
    p = await act(api, PUBLISH, to_publish, args.concurrency)
    print(f"  publish: ok={len(p['ok'])} fail={len(p['fail'])}")
    for jid, sc, err in p["fail"][:20]:
        print(f"    FAIL {jid} [{sc}] {err}")

    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
