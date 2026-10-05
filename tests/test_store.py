from app.store import clean_dsn


def test_clean_dsn_drops_pgbouncer_param():
    dsn = "postgresql://postgres.ref:pw@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?pgbouncer=true"
    assert clean_dsn(dsn) == "postgresql://postgres.ref:pw@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres"
    assert clean_dsn(f'"{dsn}"').endswith("/postgres")


def test_clean_dsn_keeps_other_params():
    assert clean_dsn("postgresql://u:p@h:5432/db?sslmode=require&pgbouncer=true") == "postgresql://u:p@h:5432/db?sslmode=require"
