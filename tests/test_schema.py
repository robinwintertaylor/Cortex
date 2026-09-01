"""DDL templating must survive SQL '{}' empty-array defaults."""

from cortex.schema import ddl_statements


def test_ddl_statements_substitutes_dim_and_keeps_empty_arrays():
    stmts = ddl_statements(1024)
    joined = "\n".join(stmts)
    assert "vector(1024)" in joined
    assert "{dim}" not in joined
    assert "DEFAULT '{}'" in joined
    assert "json_build_object(" in joined
