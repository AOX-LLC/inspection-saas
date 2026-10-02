"""Catalog checks: the roles, grants and RLS flags the isolation design relies on.

These read the system catalogs, so they cover new tables and functions
automatically. Each detector is also run against a deliberately unsafe object
created in a rolled-back transaction, to prove it would catch one.
"""

import psycopg
import pytest

APP_ROLE = "inspection_app"
OWNER_ROLE = "inspection_owner"

# Global reference data (not tenant-owned) goes here, read-only for the app.
# Every other table must force row-level security.
GLOBAL_TABLES: frozenset[str] = frozenset()

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}

# Exactly what the app role may do to each table. A new table fails here until
# someone decides its grants on purpose.
EXPECTED_APP_PRIVILEGES = {
    "orgs": DML - {"DELETE"},
    "users": DML - {"DELETE"},
    "memberships": DML,
    "projects": DML,
    "files": DML,
    "audit_events": {"SELECT", "INSERT"},
    "sessions": set(),
}

USER_SCHEMAS = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%'
    AND n.nspname NOT LIKE 'pg\\_temp%'
"""


def tables_without_forced_rls(connection: psycopg.Connection) -> set[str]:
    """Tables in user schemas that do not force RLS, outside the allowlist.

    Any table with an org_id column is included wherever it lives; other
    tables are included unless they sit in the app-inaccessible migrations
    schema.
    """
    rows = connection.execute(
        f"""
        SELECT c.oid::regclass::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND {USER_SCHEMAS}
          AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
          AND (
              n.nspname <> 'migrations'
              OR EXISTS (
                  SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attname = 'org_id' AND NOT a.attisdropped
              )
          )
        """
    ).fetchall()
    return {row[0] for row in rows} - GLOBAL_TABLES


def unsafe_security_definer_functions(connection: psycopg.Connection) -> dict[str, list[str]]:
    """SECURITY DEFINER functions without a pinned search_path or with PUBLIC EXECUTE."""
    rows = connection.execute(
        f"""
        SELECT
            p.oid::regprocedure::text,
            EXISTS (
                SELECT 1 FROM unnest(p.proconfig) setting
                WHERE setting LIKE 'search\\_path=%'
            ) AS search_path_pinned,
            EXISTS (
                SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
                WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
            ) AS public_execute
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef
          AND {USER_SCHEMAS}
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
          )
        """
    ).fetchall()
    problems: dict[str, list[str]] = {}
    for name, search_path_pinned, public_execute in rows:
        reasons = []
        if not search_path_pinned:
            reasons.append("search_path not pinned")
        if public_execute:
            reasons.append("PUBLIC has EXECUTE")
        if reasons:
            problems[name] = reasons
    return problems


def app_privileges(connection: psycopg.Connection, table: str) -> set[str]:
    return {
        privilege
        for privilege in TABLE_PRIVILEGES
        if connection.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, privilege)
        ).fetchone()[0]
    }


def public_tables(connection: psycopg.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {row[0] for row in rows}


@pytest.mark.parametrize("role", [APP_ROLE, OWNER_ROLE])
def test_role_has_no_elevated_attributes(app_conn, role):
    row = app_conn.execute(
        """
        SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication
        FROM pg_roles WHERE rolname = %s
        """,
        (role,),
    ).fetchone()
    assert row == (False, False, False, False, False)


@pytest.mark.parametrize("role", [APP_ROLE, OWNER_ROLE])
def test_role_inherits_no_other_role(app_conn, role):
    count = app_conn.execute(
        "SELECT count(*) FROM pg_auth_members WHERE member = %s::regrole", (role,)
    ).fetchone()[0]
    assert count == 0


def test_app_role_owns_nothing(app_conn):
    owned_here = app_conn.execute(
        """
        SELECT count(*) FROM pg_shdepend
        WHERE refobjid = %s::regrole AND deptype = 'o'
        """,
        (APP_ROLE,),
    ).fetchone()[0]
    owned_databases = app_conn.execute(
        "SELECT count(*) FROM pg_database WHERE datdba = %s::regrole", (APP_ROLE,)
    ).fetchone()[0]
    assert (owned_here, owned_databases) == (0, 0)


def test_app_role_cannot_create_objects(app_conn):
    row = app_conn.execute(
        """
        SELECT
            has_schema_privilege(%(role)s, 'public', 'CREATE'),
            has_schema_privilege(%(role)s, 'app', 'CREATE'),
            has_database_privilege(%(role)s, current_database(), 'CREATE'),
            has_database_privilege(%(role)s, current_database(), 'TEMPORARY')
        """,
        {"role": APP_ROLE},
    ).fetchone()
    assert row == (False, False, False, False)


def test_app_role_cannot_reach_migration_history(owner_conn):
    # Asked as the owner: the app role cannot even resolve names in the schema.
    usage = owner_conn.execute(
        "SELECT has_schema_privilege(%s, 'migrations', 'USAGE')", (APP_ROLE,)
    ).fetchone()[0]
    assert usage is False
    assert app_privileges(owner_conn, "migrations.alembic_version") == set()


def test_expected_tables_exist(app_conn):
    # Guards the checks below against passing on an empty schema.
    assert public_tables(app_conn) >= set(EXPECTED_APP_PRIVILEGES)


def test_every_table_forces_row_level_security(app_conn):
    assert tables_without_forced_rls(app_conn) == set()


def test_rls_check_catches_an_unprotected_table(owner_conn):
    owner_conn.execute("CREATE TABLE public.unprotected_probe (org_id uuid NOT NULL)")
    owner_conn.execute("CREATE TABLE migrations.misplaced_probe (org_id uuid NOT NULL)")
    owner_conn.execute("CREATE TABLE public.enabled_not_forced_probe (id int)")
    owner_conn.execute("ALTER TABLE public.enabled_not_forced_probe ENABLE ROW LEVEL SECURITY")

    assert tables_without_forced_rls(owner_conn) == {
        "unprotected_probe",
        "migrations.misplaced_probe",
        "enabled_not_forced_probe",
    }


def test_global_tables_are_read_only_for_the_app(app_conn):
    for table in GLOBAL_TABLES:
        assert app_privileges(app_conn, table) <= {"SELECT"}, table


def test_app_privileges_are_exactly_as_designed(app_conn):
    actual = {table: app_privileges(app_conn, table) for table in public_tables(app_conn)}
    assert actual == EXPECTED_APP_PRIVILEGES


def test_security_definer_functions_are_locked_down(app_conn):
    assert unsafe_security_definer_functions(app_conn) == {}


def test_definer_check_catches_an_unsafe_function(owner_conn):
    owner_conn.execute(
        """
        CREATE FUNCTION public.unsafe_probe() RETURNS int
            LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$
        """
    )
    owner_conn.execute(
        """
        CREATE FUNCTION public.safe_probe() RETURNS int
            LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$ SELECT 1 $$
        """
    )
    owner_conn.execute("REVOKE ALL ON FUNCTION public.safe_probe() FROM PUBLIC")

    assert unsafe_security_definer_functions(owner_conn) == {
        "unsafe_probe()": ["search_path not pinned", "PUBLIC has EXECUTE"],
    }


def test_context_accessors_run_as_the_caller(app_conn):
    # Accessors must never be SECURITY DEFINER: they read the caller's settings.
    definer_accessors = app_conn.execute(
        """
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'app' AND p.prosecdef
        """
    ).fetchone()[0]
    assert definer_accessors == 0
