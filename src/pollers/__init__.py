"""Long-running poller processes.

These are operator-side daemons that pull external state and write
into Career Caddy via the HTTP api. One module per source:

- ``email_catchall`` — B3 catchall mailbox → JobPost. Resolves the
  forwarded-to localpart to a Career Caddy user and posts a JobPost
  with ``source="email-forward"`` provenance.
"""
