# ── Application package bootstrap ─────────────────────────────────
#
# 이 파일은 `app` 패키지를 import 하는 모든 진입점 (run.py / uvicorn / arq worker /
# pytest) 이 가장 먼저 평가하는 코드. 환경 의존성을 여기서 한 번에 셋업해 진입점
# 별로 빠지지 않도록 보장.
#
# 1. .env 자동 load
#    - run.py 는 `load_dotenv()` 를 직접 호출하지만 `uvicorn app.main:app`
#      형태로 부팅 시엔 우회됨. 여기서 보장.
#
# 2. Windows + Python 3.13 SSL CA store 호환 fix
#    - 현상: Windows 시스템 CA store 가 일부 외부 서비스 (Neo4j AuraDB 등) 의
#      Let's Encrypt 체인을 검증 못 해 `SSLCertVerificationError: self-signed
#      certificate in certificate chain` 으로 실패. Antivirus / 보안 SW 의 TLS
#      가로채기, 또는 Python 의 windows-curated CA store 미흡이 원인.
#    - 해결: `certifi` cert bundle 을 SSL_CERT_FILE 로 강제. ssl 모듈은 import
#      시점에 이 환경변수를 읽으므로, 다른 import 보다 먼저 set.
import os
import sys

# 1) .env load — 모든 진입점에서 한 번만
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 2) Windows SSL CA fix
if sys.platform == "win32" and not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
