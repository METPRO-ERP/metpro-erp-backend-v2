"""Neutral label for the 1.8% construction tax type (Fabian, 2026-11-26).

The law citation is gone from the label. Tax logic, rates and calculations do not change, and the code value
construction_17_2017 never changes. Unit tests run everywhere; DB tests need METPRO_LEDGER_TEST_DB.
"""
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
OLD, NEW, CODE = "Ley 17-2017 (1.8%)", "Construcción (1.8%)", "construction_17_2017"
MIG = "2026-11-26_tax_label_neutral.sql"
ROLLBACK = "2026-11-26_tax_label_neutral.rollback.sql"
HISTORICAL = {"2026-10-22_tax_types.sql", MIG, ROLLBACK}


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _sql(name):
    return "\n".join(l.split("--")[0] for l in _read("migrations", name).splitlines())


def _sources():
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith("v_")]
        for f in files:
            if f.endswith((".py", ".sql")):
                yield os.path.join(base, f)


# ---------------- absence pin ----------------

def test_no_source_names_the_law_in_a_label():
    """Pin: the citation is absent everywhere except the applied-history migration and this change's own files."""
    this = os.path.abspath(__file__)
    hits = []
    for p in _sources():
        if os.path.abspath(p) == this or os.path.basename(p) in HISTORICAL:
            continue
        if re.search(r"Ley 17", open(p, encoding="utf-8", errors="ignore").read()):
            hits.append(os.path.relpath(p, ROOT))
    assert hits == []


def test_ci_schema_seed_carries_the_neutral_label():
    seed = [l for l in _read("tests", "fixtures", "ci_schema.sql").splitlines() if f"'{CODE}'" in l]
    assert seed and all(NEW in l and "Ley" not in l for l in seed)


def test_the_code_value_is_unchanged_where_logic_uses_it():
    assert f'"{CODE}"' in _read("tax", "service.py") or f"'{CODE}'" in _read("tax", "service.py")
    assert CODE in _read("estimator", "service.py")
    assert CODE not in _sql(MIG).replace(f"'{CODE}'", "") and _sql(MIG).count(f"'{CODE}'") == 1


# ---------------- migration shape ----------------

def test_migration_touches_only_the_label_of_one_row():
    sql = _sql(MIG).upper()
    assert len(re.findall(r"\bUPDATE\b", sql)) == 1 and "PUBLIC.TAX_TYPES" in sql
    assert re.search(r"SET\s+LABEL\s*=\s*'CONSTRUCCIÓN \(1\.8%\)',\s*UPDATED_AT\s*=\s*NOW\(\)\s+WHERE\s+CODE", sql)
    for word in ("RATE", "ACTIVE", "IS_DEFAULT", "SORT_ORDER"):
        assert not re.search(rf"SET[^;]*\b{word}\s*=", sql), word
    for word in ("QUOTES", "INVOICES", "DELETE", "DROP", "ALTER", "INSERT", "TRUNCATE"):
        assert word not in sql, word
    assert "AND LABEL = 'LEY 17-2017 (1.8%)'" in sql  # guarded: only the original seed label is renamed


def test_rollback_is_guarded_and_restores_only_the_migration_label():
    sql = _sql(ROLLBACK).upper()
    assert len(re.findall(r"\bUPDATE\b", sql)) == 1 and "AND LABEL = 'CONSTRUCCIÓN (1.8%)'" in sql
    assert "QUOTES" not in sql and "INVOICES" not in sql


# ---------------- golden PDFs are independent of this label ----------------

def test_golden_pdf_builds_never_pass_a_tax_label():
    """The goldens print the default 'ITBIS' (no tax_label passed), so a label change cannot move their hashes."""
    for name in ("test_pdf_medidas_m2.py", "test_ncf_phases.py", "test_pdf_correctness.py"):
        src = _read("tests", name)
        assert "tax_label" not in src, name
        assert "Ley 17" not in src and "Construcción (1.8%)" not in src, name


# ---------------- database behavior ----------------

DSN = os.environ.get("METPRO_LEDGER_TEST_DB")
needs_db = pytest.mark.skipif(not DSN, reason="METPRO_LEDGER_TEST_DB not set")


@pytest.fixture
def conn():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    c = psycopg2.connect(DSN, cursor_factory=RealDictCursor)
    cur = c.cursor()
    cur.execute("SELECT label, rate, active, is_default, sort_order FROM tax_types WHERE code = %s", (CODE,))
    saved = cur.fetchone()
    c.commit()
    yield c
    c.rollback()
    cur = c.cursor()
    cur.execute("UPDATE tax_types SET label=%s, rate=%s, active=%s, is_default=%s, sort_order=%s WHERE code=%s",
                (saved["label"], saved["rate"], saved["active"], saved["is_default"], saved["sort_order"], CODE))
    c.commit()
    c.close()


def _state(c):
    cur = c.cursor()
    cur.execute("SELECT code, label, rate, active, is_default, sort_order FROM tax_types ORDER BY code")
    return cur.fetchall()


def _run(c, name):
    cur = c.cursor()
    cur.execute(_read("migrations", name))
    c.commit()


def _set(c, label, **extra):
    cur = c.cursor()
    cur.execute("UPDATE tax_types SET label = %s WHERE code = %s", (label, CODE))
    for k, v in extra.items():
        cur.execute(f"UPDATE tax_types SET {k} = %s WHERE code = %s", (v, CODE))
    c.commit()


@needs_db
def test_migration_renames_the_seed_label_and_nothing_else(conn):
    _set(conn, OLD)
    before = {r["code"]: r for r in _state(conn)}
    _run(conn, MIG)
    after = {r["code"]: r for r in _state(conn)}
    assert after[CODE]["label"] == NEW
    for code, row in after.items():
        for col in ("code", "rate", "active", "is_default", "sort_order"):
            assert row[col] == before[code][col], (code, col)
        if code != CODE:
            assert row["label"] == before[code]["label"]
    assert float(after[CODE]["rate"]) == pytest.approx(0.018)


@needs_db
def test_migration_is_idempotent_and_leaves_an_edited_label_alone(conn):
    _set(conn, OLD)
    _run(conn, MIG)
    _run(conn, MIG)
    assert [r["label"] for r in _state(conn) if r["code"] == CODE] == [NEW]
    _set(conn, "Etiqueta del administrador")
    _run(conn, MIG)
    assert [r["label"] for r in _state(conn) if r["code"] == CODE] == ["Etiqueta del administrador"]


@needs_db
def test_rollback_restores_the_old_label_only_when_it_still_holds_the_new_one(conn):
    _set(conn, NEW)
    _run(conn, ROLLBACK)
    assert [r["label"] for r in _state(conn) if r["code"] == CODE] == [OLD]
    _set(conn, "Etiqueta del administrador")
    _run(conn, ROLLBACK)
    assert [r["label"] for r in _state(conn) if r["code"] == CODE] == ["Etiqueta del administrador"]


@needs_db
def test_migration_does_not_touch_stored_document_snapshots(conn):
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(string_agg(tax_label, '|' ORDER BY quote_id), '') AS q FROM quotes")
    q0 = cur.fetchone()["q"]
    cur.execute("SELECT COALESCE(string_agg(tax_label, '|' ORDER BY id), '') AS i FROM invoices")
    i0 = cur.fetchone()["i"]
    conn.commit()
    _set(conn, OLD)
    _run(conn, MIG)
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(string_agg(tax_label, '|' ORDER BY quote_id), '') AS q FROM quotes")
    assert cur.fetchone()["q"] == q0
    cur.execute("SELECT COALESCE(string_agg(tax_label, '|' ORDER BY id), '') AS i FROM invoices")
    assert cur.fetchone()["i"] == i0
    conn.commit()
