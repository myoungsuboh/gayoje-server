"""[2026-06-04] 일회용 이메일 차단 — is_disposable_email 단위 테스트.

[2026-06 OAuth 전용] SignupRequest 검증 테스트는 제거(이메일/비번 가입 폐지).
is_disposable_email 함수 자체는 유지 — 향후 OAuth 이메일 검증 등 재사용 가능.
"""
from app.core.disposable_emails import is_disposable_email


def test_known_disposable_domains_detected():
    for e in [
        "x@mailinator.com", "y@guerrillamail.com", "z@10minutemail.com",
        "a@temp-mail.org", "b@yopmail.com", "c@sub.mailinator.com",
    ]:
        assert is_disposable_email(e) is True, e


def test_normal_domains_allowed():
    for e in [
        "user@gmail.com", "user@naver.com", "user@daum.net",
        "user@outlook.com", "ceo@mycompany.co.kr", "dev@example.com",
    ]:
        assert is_disposable_email(e) is False, e
