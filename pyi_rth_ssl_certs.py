# PyInstaller runtime hook — point the TLS trust store at the bundled certifi.
#
# Runs once, before main.py, inside the frozen process. Without it the stdlib
# `ssl` layer falls back to OpenSSL's compiled-in CApath (e.g. the build box's
# /opt/homebrew/etc/openssl@3), which does NOT exist on an end user's machine,
# so any HTTPS the ML net contour makes — huggingface_hub / transformers weight
# download on the Linux first-run path (weight_manager.snapshot_download) — dies
# with CERTIFICATE_VERIFY_FAILED. certifi.where() already resolves to the
# cacert.pem we ship via collect_data_files("certifi") in TensorMedia.spec; here
# we export it so BOTH requests (REQUESTS_CA_BUNDLE) and raw stdlib ssl/urllib
# (SSL_CERT_FILE) trust it. setdefault: never clobber an env the user set.
import os

try:
    import certifi

    _ca = certifi.where()
    if _ca and os.path.exists(_ca):
        os.environ.setdefault("SSL_CERT_FILE", _ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    # A missing/odd certifi must not block app start — offline (macOS/Windows,
    # TRANSFORMERS_OFFLINE=1, weights bundled) does not touch the network at all.
    pass
