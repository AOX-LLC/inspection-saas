"""Catalog checks: the roles, grants, RLS flags and policies the isolation design relies on.

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

# What `org_id = (SELECT app.org_id())` reads back as from the catalog.
TENANT_CHECK = "(org_id = ( SELECT app.org_id() AS org_id))"
# Identity tables with an org_id column whose policies combine conditions with
# OR. The behaviour tests pin them instead.
OR_COMPOSED_TABLES = frozenset({"memberships"})

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


def public_relations(connection: psycopg.Connection) -> set[str]:
    """Tables, views, materialized views and foreign tables in public."""
    rows = connection.execute(
        """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        """
    ).fetchall()
    return {row[0] for row in rows}


HAS_ORG_ID = """
    EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = {relation} AND a.attname = 'org_id' AND NOT a.attisdropped
    )
"""


def loose_tenant_policies(connection: psycopg.Connection) -> dict[str, list[str]]:
    """Permissive policies on org_id tables whose expressions are not the tenant check.

    Permissive policies are OR-ed together, so a single loose one opens the
    table. Restrictive policies only narrow access and are not checked.
    """
    rows = connection.execute(
        f"""
        SELECT
            c.oid::regclass::text,
            p.polname,
            pg_get_expr(p.polqual, p.polrelid),
            pg_get_expr(p.polwithcheck, p.polrelid)
        FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE p.polpermissive
          AND {USER_SCHEMAS}
          AND {HAS_ORG_ID.format(relation="c.oid")}
        """
    ).fetchall()
    problems: dict[str, list[str]] = {}
    for table, policy, using, with_check in rows:
        if table in OR_COMPOSED_TABLES:
            continue
        loose = [
            f"{clause}: {expression}"
            for clause, expression in (("USING", using), ("WITH CHECK", with_check))
            if expression is not None and expression != TENANT_CHECK
        ]
        if loose:
            problems[f"{table}.{policy}"] = loose
    return problems


def foreign_keys_without_org_id(connection: psycopg.Connection) -> set[str]:
    """FKs between two org_id tables that do not pair org_id with org_id."""
    rows = connection.execute(
        f"""
        SELECT con.conrelid::regclass::text || '.' || con.conname
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype = 'f'
          AND {USER_SCHEMAS}
          AND {HAS_ORG_ID.format(relation="con.conrelid")}
          AND {HAS_ORG_ID.format(relation="con.confrelid")}
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(con.conkey, con.confkey) AS pair (child, parent)
              JOIN pg_attribute child ON child.attrelid = con.conrelid
                                     AND child.attnum = pair.child
              JOIN pg_attribute parent ON parent.attrelid = con.confrelid
                                      AND parent.attnum = pair.parent
              WHERE child.attname = 'org_id' AND parent.attname = 'org_id'
          )
        """
    ).fetchall()
    return {row[0] for row in rows}


def relations_that_skip_rls(connection: psycopg.Connection) -> set[str]:
    """Relations that can expose rows without the caller's policies applying.

    Materialized views and foreign tables cannot carry RLS. A plain view runs
    as its owner unless it is marked security_invoker.
    """
    rows = connection.execute(
        f"""
        SELECT c.oid::regclass::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {USER_SCHEMAS}
          AND (
              c.relkind IN ('m', 'f')
              OR (
                  c.relkind = 'v'
                  AND NOT EXISTS (
                      SELECT 1 FROM unnest(c.reloptions) option
                      WHERE option IN (
                          'security_invoker=true', 'security_invoker=on', 'security_invoker=1'
                      )
                  )
              )
          )
        """
    ).fetchall()
    return {row[0] for row in rows} - GLOBAL_TABLES


def app_column_grants(connection: psycopg.Connection) -> set[str]:
    """Column-level grants to the app, which has_table_privilege does not see."""
    rows = connection.execute(
        f"""
        SELECT c.oid::regclass::text || '.' || a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(a.attacl) acl
        WHERE a.attacl IS NOT NULL
          AND acl.grantee = '{APP_ROLE}'::regrole
          AND {USER_SCHEMAS}
        """
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
    assert public_relations(app_conn) >= set(EXPECTED_APP_PRIVILEGES)


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
    actual = {table: app_privileges(app_conn, table) for table in public_relations(app_conn)}
    assert actual == EXPECTED_APP_PRIVILEGES


def test_app_has_no_column_level_grants(app_conn):
    assert app_column_grants(app_conn) == set()


def test_column_grant_check_catches_a_grant(owner_conn):
    owner_conn.execute(f"GRANT SELECT (token_sha256) ON sessions TO {APP_ROLE}")
    assert app_column_grants(owner_conn) == {"sessions.token_sha256"}


def test_tenant_policies_compare_org_id_with_the_context(app_conn):
    assert loose_tenant_policies(app_conn) == {}


def test_policy_check_catches_a_loose_policy(owner_conn):
    owner_conn.execute(
        """
        CREATE TABLE public.loose_probe (org_id uuid NOT NULL);
        CREATE POLICY loose_all ON public.loose_probe USING (true);
        CREATE POLICY loose_insert ON public.loose_probe
            FOR INSERT WITH CHECK (org_id IS NOT NULL);
        CREATE TABLE public.tight_probe (org_id uuid NOT NULL);
        CREATE POLICY tight ON public.tight_probe
            USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));
        CREATE POLICY narrowing ON public.tight_probe AS RESTRICTIVE USING (false);
        """
    )
    assert loose_tenant_policies(owner_conn) == {
        "loose_probe.loose_all": ["USING: true"],
        "loose_probe.loose_insert": ["WITH CHECK: (org_id IS NOT NULL)"],
    }


def test_child_tables_reference_parents_by_org_id(app_conn):
    assert foreign_keys_without_org_id(app_conn) == set()


def test_foreign_key_check_catches_a_single_column_reference(owner_conn):
    owner_conn.execute(
        """
        CREATE TABLE public.loose_child_probe (
            org_id uuid NOT NULL,
            project_id uuid REFERENCES projects (id)
        );
        CREATE TABLE public.tight_child_probe (
            org_id uuid NOT NULL,
            project_id uuid,
            FOREIGN KEY (org_id, project_id) REFERENCES projects (org_id, id)
        );
        """
    )
    assert foreign_keys_without_org_id(owner_conn) == {
        "loose_child_probe.loose_child_probe_project_id_fkey"
    }


def test_no_relation_skips_row_level_security(app_conn):
    assert relations_that_skip_rls(app_conn) == set()


def test_view_check_catches_relations_that_skip_rls(owner_conn):
    owner_conn.execute(
        """
        CREATE VIEW public.owner_view_probe AS SELECT 1 AS x;
        CREATE VIEW public.invoker_view_probe WITH (security_invoker = true) AS SELECT 1 AS x;
        CREATE MATERIALIZED VIEW public.matview_probe AS SELECT 1 AS x;
        """
    )
    assert relations_that_skip_rls(owner_conn) == {"owner_view_probe", "matview_probe"}


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
