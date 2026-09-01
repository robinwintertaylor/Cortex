import os

os.environ.setdefault("CORTEX_ADMIN_KEY", "cx-admin-test")
os.environ.setdefault("CORTEX_HOOK_TOKEN", "hook-token-test")
os.environ.setdefault("CORTEX_LLM_BASE_URL", "")  # extraction disabled in tests
