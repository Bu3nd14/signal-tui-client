"""Unified Python launcher — replaces every ``.sh`` script in this repo.

Kept dependency-free (stdlib only): ``install`` runs *before* anything in
requirements.txt is on disk, so nothing under this package may import a
third-party library at module scope.
"""
