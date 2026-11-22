"""Jared: a read-only bridge from a chat agent to a Paperless-ngx document archive, run by its own unix user.

The agent searches, opens and reads documents over MCP. Jared holds the API token of a view-only Paperless account
and can only send GET requests to a fixed list of paths on this machine. Nothing in it adds, changes or deletes.
"""
