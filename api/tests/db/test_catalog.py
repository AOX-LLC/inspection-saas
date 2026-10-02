"""Catalog checks: the roles, grants, RLS flags and policies the isolation design relies on.

These read the system catalogs, so they cover new tables and functions
automatically. Each detector is also run against a deliberately unsafe object
created in a rolled-back transaction, to prove it would catch one.
"""

import psycopg
import pytest

APP_ROLE = "inspection_app"
OWNER_ROLE = "inspection_owner"
AUTH_ROLE = "inspection_auth"
WORKER_ROLE = "inspection_worker"
DISPATCHER_ROLE = "inspection_dispatcher"

# Who may own SECURITY DEFINER functions, by schema. They run with the owner's
# rights, so the owner is a decision, not a default: a new definer function
# fails the ownership check until its schema is listed here on purpose.
DEFINER_OWNER_BY_SCHEMA = {"auth": AUTH_ROLE}

# Global reference data (not tenant-owned) goes here, read-only for the app: a
# table on this list must not grant the app INSERT, UPDATE or DELETE, because
# default privileges hand a new table all three and RLS no longer limits it.
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
    "credentials": set(),
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


def definer_functions(connection: psycopg.Connection) -> list[tuple[str, str, str, set[str]]]:
    """Every SECURITY DEFINER function: (signature, schema, owner, EXECUTE grantees).

    Grantees exclude the owner, who always holds EXECUTE; PUBLIC appears as 'PUBLIC'.
    """
    rows = connection.execute(
        f"""
        SELECT
            p.oid::regprocedure::text,
            n.nspname,
            owner.rolname,
            coalesce(
                array_agg(DISTINCT coalesce(grantee.rolname, 'PUBLIC'))
                    FILTER (WHERE acl.privilege_type = 'EXECUTE' AND acl.grantee <> p.proowner),
                '{{}}'
            )
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_roles owner ON owner.oid = p.proowner
        LEFT JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl ON true
        LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee
        WHERE p.prosecdef
          AND {USER_SCHEMAS}
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
          )
        GROUP BY p.oid, n.nspname, owner.rolname
        """
    ).fetchall()
    return [(name, schema, owner, set(grantees)) for name, schema, owner, grantees in rows]


def definer_functions_with_wrong_owner(connection: psycopg.Connection) -> dict[str, str]:
    """Definer functions not owned by the role their schema is approved for.

    The approved owner must also be unable to log in and not a superuser, so a
    definer function can never run with a login role's or superuser's rights.
    """
    problems = {}
    for name, schema, owner, _ in definer_functions(connection):
        if DEFINER_OWNER_BY_SCHEMA.get(schema) != owner:
            problems[name] = f"owned by {owner}"
            continue
        can_login, is_super = connection.execute(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s", (owner,)
        ).fetchone()
        if can_login or is_super:
            problems[name] = f"{owner} can log in or is a superuser"
    return problems


def definer_functions_with_unexpected_execute(connection: psycopg.Connection) -> dict[str, set]:
    """Definer functions whose EXECUTE grantees (besides the owner) are not exactly the app."""
    return {
        name: grantees
        for name, _, _, grantees in definer_functions(connection)
        if grantees != {APP_ROLE}
    }


WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def global_tables_with_write_grants(
    connection: psycopg.Connection, tables: frozenset[str] | set[str]
) -> dict[str, set[str]]:
    """Tables the app can write to, from a set that must be read-only for it.

    Covers table-level grants and column-level INSERT or UPDATE grants.
    """
    problems: dict[str, set[str]] = {}
    for table in tables:
        granted = {
            privilege
            for privilege in WRITE_PRIVILEGES
            if connection.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, privilege)
            ).fetchone()[0]
        }
        for privilege in ("INSERT", "UPDATE"):
            column_grant = connection.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (APP_ROLE, table, privilege)
            ).fetchone()[0]
            if column_grant:
                granted.add(privilege)
        if granted:
            problems[table] = granted
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


@pytest.mark.parametrize("role", [APP_ROLE, OWNER_ROLE, WORKER_ROLE])
def test_role_has_no_elevated_attributes(app_conn, role):
    row = app_conn.execute(
        """
        SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication
        FROM pg_roles WHERE rolname = %s
        """,
        (role,),
    ).fetchone()
    assert row == (False, False, False, False, False)


def test_auth_role_can_bypass_rls_but_nothing_else(app_conn):
    row = app_conn.execute(
        """
        SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin
        FROM pg_roles WHERE rolname = %s
        """,
        (AUTH_ROLE,),
    ).fetchone()
    assert row == (False, True, False, False, False, False)


def test_app_role_inherits_no_other_role(app_conn):
    count = app_conn.execute(
        "SELECT count(*) FROM pg_auth_members WHERE member = %s::regrole", (APP_ROLE,)
    ).fetchone()[0]
    assert count == 0


def test_owner_may_set_role_to_the_definer_roles_but_does_not_inherit_them(app_conn):
    """The owner's only memberships are the two NOLOGIN definer roles: SET ROLE, no inherit."""
    memberships = app_conn.execute(
        """
        SELECT roleid::regrole::text, inherit_option, set_option, admin_option
        FROM pg_auth_members WHERE member = %s::regrole
        """,
        (OWNER_ROLE,),
    ).fetchall()
    assert sorted(memberships) == [
        (AUTH_ROLE, False, True, False),
        (DISPATCHER_ROLE, False, True, False),
    ]


def test_nobody_else_is_a_member_of_the_auth_role(app_conn):
    members = {
        row[0]
        for row in app_conn.execute(
            "SELECT member::regrole::text FROM pg_auth_members WHERE roleid = %s::regrole",
            (AUTH_ROLE,),
        ).fetchall()
    }
    assert members == {OWNER_ROLE}


def test_auth_role_owns_only_the_auth_functions(app_conn):
    """Everything the BYPASSRLS role owns is a function in schema auth, and all of them are."""
    owned = app_conn.execute(
        """
        SELECT d.classid::regclass::text, d.objid
        FROM pg_shdepend d
        WHERE d.refobjid = %s::regrole AND d.deptype = 'o'
          AND d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
        """,
        (AUTH_ROLE,),
    ).fetchall()
    assert owned, "the auth role owns nothing; the functions are missing"
    assert {classid for classid, _ in owned} == {"pg_proc"}
    owned_functions = {objid for _, objid in owned}
    in_auth_schema = {
        row[0]
        for row in app_conn.execute(
            """
            SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'auth'
            """
        ).fetchall()
    }
    assert owned_functions == in_auth_schema
    owned_databases = app_conn.execute(
        "SELECT count(*) FROM pg_database WHERE datdba = %s::regrole", (AUTH_ROLE,)
    ).fetchone()[0]
    assert owned_databases == 0


def test_auth_role_holds_only_the_grants_it_needs(app_conn):
    rows = app_conn.execute(
        """
        SELECT c.relname, acl.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE acl.grantee = %s::regrole AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        """,
        (AUTH_ROLE,),
    ).fetchall()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)
    assert granted == {
        "users": {"SELECT"},
        "credentials": {"SELECT", "INSERT", "UPDATE"},
        "sessions": {"SELECT", "INSERT", "UPDATE"},
    }


def test_owner_role_inherits_nothing(app_conn):
    count = app_conn.execute(
        """
        SELECT count(*) FROM pg_auth_members WHERE member = %s::regrole AND inherit_option
        """,
        (OWNER_ROLE,),
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


def test_nobody_can_create_objects_in_the_auth_schema(app_conn):
    rows = {
        role: app_conn.execute(
            "SELECT has_schema_privilege(%s, 'auth', 'CREATE')", (role,)
        ).fetchone()[0]
        for role in (APP_ROLE, AUTH_ROLE)
    }
    assert rows == {APP_ROLE: False, AUTH_ROLE: False}


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
    assert global_tables_with_write_grants(app_conn, GLOBAL_TABLES) == {}


def test_global_table_check_catches_write_grants(owner_conn):
    # A new table inherits DML for the app from default privileges, so a global
    # table that forgets to revoke it is open for writing.
    owner_conn.execute(
        f"""
        CREATE TABLE public.global_open_probe (id int, note text);
        CREATE TABLE public.global_closed_probe (id int, note text);
        REVOKE INSERT, UPDATE, DELETE ON public.global_closed_probe FROM {APP_ROLE};
        CREATE TABLE public.global_column_probe (id int, note text);
        REVOKE INSERT, UPDATE, DELETE ON public.global_column_probe FROM {APP_ROLE};
        GRANT UPDATE (note) ON public.global_column_probe TO {APP_ROLE};
        """
    )
    probes = {"global_open_probe", "global_closed_probe", "global_column_probe"}

    assert global_tables_with_write_grants(owner_conn, probes) == {
        "global_open_probe": {"INSERT", "UPDATE", "DELETE"},
        "global_column_probe": {"UPDATE"},
    }


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


def test_definer_functions_exist(app_conn):
    # Guards the checks below against passing on an empty set.
    names = {name for name, *_ in definer_functions(app_conn)}
    assert names == {
        "auth.verify_login(text)",
        "auth.create_session(uuid,bytea,integer)",
        "auth.resolve_session(bytea,integer)",
        "auth.revoke_session(bytea)",
    }


def test_definer_functions_are_owned_by_the_auth_role(app_conn):
    assert definer_functions_with_wrong_owner(app_conn) == {}


def test_definer_functions_are_executable_by_the_app_alone(app_conn):
    assert definer_functions_with_unexpected_execute(app_conn) == {}


def test_definer_owner_check_catches_the_wrong_owner(owner_conn):
    # Created as the table owner, which is exactly what must not happen.
    owner_conn.execute("GRANT CREATE ON SCHEMA auth TO inspection_owner")
    owner_conn.execute("CREATE SCHEMA probe_schema")
    owner_conn.execute(
        """
        CREATE FUNCTION probe_schema.unlisted_schema() RETURNS int
            LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$ SELECT 1 $$
        """
    )
    owner_conn.execute(
        """
        CREATE FUNCTION auth.wrong_owner() RETURNS int
            LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$ SELECT 1 $$
        """
    )

    assert definer_functions_with_wrong_owner(owner_conn) == {
        "probe_schema.unlisted_schema()": "owned by inspection_owner",
        "auth.wrong_owner()": "owned by inspection_owner",
    }


def test_definer_execute_check_catches_extra_grantees(owner_conn):
    owner_conn.execute("SET LOCAL ROLE inspection_auth")
    owner_conn.execute("GRANT EXECUTE ON FUNCTION auth.revoke_session(bytea) TO inspection_owner")
    owner_conn.execute("RESET ROLE")
    owner_conn.execute("SET LOCAL ROLE inspection_auth")
    owner_conn.execute("GRANT EXECUTE ON FUNCTION auth.verify_login(text) TO PUBLIC")
    owner_conn.execute("RESET ROLE")

    assert definer_functions_with_unexpected_execute(owner_conn) == {
        "auth.revoke_session(bytea)": {APP_ROLE, OWNER_ROLE},
        "auth.verify_login(text)": {APP_ROLE, "PUBLIC"},
    }


def test_owner_cannot_run_the_auth_functions(owner_conn):
    # The functions exist for the app role alone; the owner is bound by RLS and
    # must not reach sessions or credentials through them either.
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="permission denied for function"
    ):
        owner_conn.execute("SELECT * FROM auth.verify_login('x@y.example')")


def test_context_accessors_run_as_the_caller(app_conn):
    # Accessors must never be SECURITY DEFINER: they read the caller's settings.
    definer_accessors = app_conn.execute(
        """
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'app' AND p.prosecdef
        """
    ).fetchone()[0]
    assert definer_accessors == 0
