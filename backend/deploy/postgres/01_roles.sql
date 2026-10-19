-- Database roles for the booking engine (run ONCE per database, as a superuser, before the first migration).
--
--   psql -v ON_ERROR_STOP=1 -v owner_password="..." -v app_password="..." -v backup_password="..." -d booking -f deploy/postgres/01_roles.sql
--
-- Why separate roles: Row-Level Security (the tenant isolation) is ignored by superusers and by roles with BYPASSRLS, and the owner of a
-- table can switch it off with ALTER TABLE. So
--   booking_owner  owns the schema and runs the migrations (alembic). It is NOT a superuser. Used only by the migrate step.
--   booking_app    what the API and the worker connect as. It can read and write rows and nothing else: no DDL, no BYPASSRLS,
--                  not the owner of any table. A bug or an injection in the application therefore cannot lift tenant isolation.
--   booking_backup read-only, may bypass the policies so pg_dump can see every tenant. Used by deploy/backup.sh only.
-- The extensions (PostGIS is not "trusted") are created here, as the superuser, so the owner never needs superuser rights.
--
-- Safe to run again: it creates what is missing and re-applies the passwords and grants.

\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS postgis;

SELECT format('CREATE ROLE booking_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_owner') \gexec
SELECT format('ALTER ROLE booking_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'owner_password') \gexec

SELECT format('CREATE ROLE booking_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_app') \gexec
SELECT format('ALTER ROLE booking_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'app_password') \gexec

-- booking_backup reads everything for pg_dump and can change nothing. It needs BYPASSRLS because the tenant policies are FORCED (they
-- apply to the table owner too, so even booking_owner cannot dump); that is why backups use this role and the application never does.
SELECT format('CREATE ROLE booking_backup LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'backup_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_backup') \gexec
SELECT format('ALTER ROLE booking_backup LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'backup_password') \gexec
GRANT pg_read_all_data TO booking_backup;

-- the owner builds the schema; nobody else may create objects in it
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, CREATE ON DATABASE %I TO booking_owner', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO booking_app, booking_backup', current_database()) \gexec

ALTER SCHEMA public OWNER TO booking_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO booking_app;

-- PostGIS keeps its reference table in public; the extension's own functions are executable by everyone already
GRANT SELECT ON TABLE public.spatial_ref_sys TO booking_app;

-- every table and sequence the owner creates from now on (that is: every migration) is usable by the app, as rows only
ALTER DEFAULT PRIVILEGES FOR ROLE booking_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO booking_app;
ALTER DEFAULT PRIVILEGES FOR ROLE booking_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO booking_app;

-- ...and what the owner already created (re-running this script after a migration fixes up anything made before the defaults).
-- Only the owner's own objects: PostGIS's tables belong to the superuser and stay read-only for the app.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = 'booking_owner' LOOP
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO booking_app', r.tablename);
  END LOOP;
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S' AND o.rolname = 'booking_owner' LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.%I TO booking_app', r.relname);
  END LOOP;
  -- the migration bookkeeping is the owner's alone
  IF to_regclass('public.alembic_version') IS NOT NULL THEN
    REVOKE ALL ON TABLE public.alembic_version FROM booking_app;
  END IF;
END $$;
