#!/usr/bin/env python3
"""Wyrywkowy test mutacyjny: czy testy wychwytują zmianę logiki w danym pliku?

Użycie (z korzenia projektu, interpreterem projektu):

    .venv/bin/python scripts/mutation_check.py src/perno/domain/mail_rules.py tests/test_mail_rules.py tests/test_db.py

Skrypt kopiuje projekt do katalogu tymczasowego (Twoje pliki zostają nietknięte), zmienia w kopii po jednym operatorze
(porównania, and/or, not, True/False, liczby całkowite) i uruchamia podane testy. Mutant, przy którym testy nadal
przechodzą, „przeżył”: albo asercje są za słabe, albo zmiana nic nie zmienia (mutant równoważny, np. liczba w stałej,
którą testy czytają symbolicznie). Przeżywców przejrzyj ręcznie. Dotyczy to zwłaszcza kodu pisanego z pomocą AI: testy
napisane „pod kod” potrafią przechodzić mimo zepsutej logiki.

    --max N     sprawdź najwyżej N mutantów (równomiernie rozłożonych po pliku), gdy plik jest duży
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = shutil.ignore_patterns(".venv", ".git", "backup", "logs", "__pycache__", ".mypy_cache", ".ruff_cache",
                                ".pytest_cache", "node_modules", "*.egg-info")
COMPARE = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
           ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def sites(tree: ast.AST) -> list:
    """Miejsca, w których potrafimy zmienić jeden operator: (rodzaj, węzeł, indeks)."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            found += [("cmp", node, i) for i, op in enumerate(node.ops) if type(op) in COMPARE]
        elif isinstance(node, ast.BoolOp):
            found.append(("bool", node, 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append(("not", node, 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append(("const", node, 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            found.append(("int", node, 0))
    return found


def describe(kind: str, node: ast.AST, index: int) -> str:
    """Opis mutanta do raportu: numer linii i zmiana."""
    line = getattr(node, "lineno", "?")
    if kind == "cmp":
        return f"l.{line}: {type(node.ops[index]).__name__} -> {COMPARE[type(node.ops[index])].__name__}"
    if kind == "bool":
        return f"l.{line}: {type(node.op).__name__} -> {'Or' if isinstance(node.op, ast.And) else 'And'}"
    if kind == "not":
        return f"l.{line}: usunięte `not`"
    if kind == "const":
        return f"l.{line}: {node.value} -> {not node.value}"
    return f"l.{line}: {node.value} -> {node.value + 1}"


def mutate(source: str, number: int) -> str:
    """Kod źródłowy z jedną zmianą w `number`-tym miejscu."""
    tree = ast.parse(source)
    kind, node, index = sites(tree)[number]
    if kind == "cmp":
        node.ops[index] = COMPARE[type(node.ops[index])]()
    elif kind == "bool":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "not":
        class Unwrap(ast.NodeTransformer):
            def visit_UnaryOp(self, current: ast.UnaryOp) -> ast.AST:
                self.generic_visit(current)
                return current.operand if current is node else current

        tree = Unwrap().visit(tree)
        ast.fix_missing_locations(tree)
    elif kind == "const":
        node.value = not node.value
    else:
        node.value = node.value + 1
    return ast.unparse(tree)


def main() -> int:
    """Kopiuje projekt, uruchamia testy dla każdego mutanta i wypisuje wynik."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="plik z kodem do zmutowania, np. src/perno/domain/mail_rules.py")
    parser.add_argument("tests", nargs="+", help="pliki testów uruchamiane dla każdego mutanta")
    parser.add_argument("--max", type=int, default=0, help="najwyżej tyle mutantów (równomiernie po pliku)")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="perno-mutation-")
    try:
        project = os.path.join(workdir, "projekt")
        shutil.copytree(ROOT, project, ignore=IGNORE)
        target = os.path.join(project, args.target)
        with open(target, encoding="utf-8") as handle:
            original = handle.read()
        total = len(sites(ast.parse(original)))
        labels = [describe(*site) for site in sites(ast.parse(original))]
        numbers = list(range(total))
        if args.max and total > args.max:
            step = total / args.max
            numbers = sorted({int(i * step) for i in range(args.max)})
        env = {**os.environ, "PYTHONPATH": os.path.join(project, "src")}
        print(f"{args.target}: {len(numbers)} z {total} mutantów, testy: {' '.join(args.tests)}", flush=True)
        killed, survived, started = 0, [], time.time()
        for done, number in enumerate(numbers, start=1):
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(mutate(original, number))
            try:
                code = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--no-header",
                                       *args.tests], cwd=project, env=env, capture_output=True, text=True, timeout=180).returncode
            except subprocess.TimeoutExpired:
                code = 1  # zawieszenie też oznacza wykrycie
            if code == 0:
                survived.append(labels[number])
            else:
                killed += 1
            if done % 10 == 0:
                print(f"  ... {done}/{len(numbers)}, przeżyło dotąd {len(survived)} ({time.time() - started:.0f}s)", flush=True)
        score = 100.0 * killed / max(1, killed + len(survived))
        print(f"WYNIK: wykryto {killed}, przeżyło {len(survived)}; wskaźnik wykrycia {score:.0f}%")
        for label in survived:
            print("  PRZEŻYŁ", label)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
