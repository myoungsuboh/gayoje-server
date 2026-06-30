"""
Harness Backend Entrypoint
프론트엔드(Vue) → 본 백엔드(FastAPI) → Neo4j / Gemini 의 진입점.
"""
import os
import sys
import uvicorn
from dotenv import load_dotenv

# Windows cmd.exe(cp949) 환경에서도 유니코드 print가 깨지지 않도록 stdout/stderr를 UTF-8로 강제.
# Python 3.7+에서 reconfigure 사용 가능.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# .env 로드 (환경변수 주입)
load_dotenv()

# 현재 디렉토리를 Python path에 추가 (모듈 import용)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

if __name__ == "__main__":
    # 개발/운영 모드 분리 (기본: development)
    is_dev = os.getenv("ENV", "development") == "development"
    port = int(os.getenv("PORT", "8000"))

    print(f"🚀 Harness Backend 시작 ({'Development' if is_dev else 'Production'} 모드)")
    print(f"   - 포트: {port}")

    uvicorn.run(
        "app.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,      # 개발 모드일 때만 자동 재시작
    )
