"""Adaptery systemu operacyjnego (macOS): uruchamianie przy logowaniu (launchd) i zasilanie (caffeinate, bateria).

Każda funkcja dostaje wstrzykiwane wywołanie podprocesu, więc testy nie ruszają prawdziwego `launchctl` ani `pmset`.
"""
