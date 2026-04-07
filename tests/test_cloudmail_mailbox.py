import unittest
from unittest import mock

from core.base_mailbox import CloudMailMailbox, MailboxAccount, create_mailbox


class CloudMailMailboxTests(unittest.TestCase):
    def setUp(self):
        CloudMailMailbox._token_cache.clear()
        CloudMailMailbox._seen_ids.clear()

    def test_get_email_uses_configured_domain(self):
        mailbox = create_mailbox(
            "cloudmail",
            extra={
                "cloudmail_api_base": "https://cloudmail.example.com",
                "cloudmail_admin_password": "secret",
                "cloudmail_domain": "mail.example.com",
            },
        )

        account = mailbox.get_email()

        self.assertTrue(account.email.endswith("@mail.example.com"))
        self.assertEqual(account.account_id, account.email)

    def test_get_email_supports_legacy_field_names(self):
        mailbox = create_mailbox(
            "cloudmail",
            extra={
                "base_url": "https://cloudmail.example.com",
                "admin_password": "secret",
                "domain": "mail.example.com",
                "subdomain": "pool-a",
            },
        )

        account = mailbox.get_email()

        self.assertTrue(account.email.endswith("@pool-a.mail.example.com"))
        self.assertEqual(account.account_id, account.email)

    @mock.patch("requests.post")
    def test_wait_for_code_retries_after_auth_failure(self, mock_post):
        mock_post.side_effect = [
            _json_response({"code": 200, "data": {"token": "tok-1"}}),
            _text_response(401, "unauthorized"),
            _json_response({"code": 200, "data": {"token": "tok-2"}}),
            _json_response(
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": "m-1",
                            "toEmail": "demo@example.com",
                            "subject": "Your verification code is 654321",
                            "content": "",
                        }
                    ],
                }
            ),
        ]

        mailbox = create_mailbox(
            "cloudmail",
            extra={
                "cloudmail_api_base": "https://cloudmail.example.com",
                "cloudmail_admin_email": "admin@example.com",
                "cloudmail_admin_password": "secret",
                "cloudmail_domain": "mail.example.com",
            },
        )
        account = MailboxAccount(email="demo@example.com", account_id="demo@example.com")

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "654321")
        self.assertEqual(mock_post.call_count, 4)

    @mock.patch("requests.post")
    def test_wait_for_code_allows_small_timestamp_skew_after_otp_resend(self, mock_post):
        mock_post.side_effect = [
            _json_response({"code": 200, "data": {"token": "tok-1"}}),
            _json_response(
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": "m-2",
                            "toEmail": "demo@example.com",
                            "subject": "Your verification code is 777888",
                            "content": "",
                            "time": 998.5,
                        }
                    ],
                }
            ),
        ]

        mailbox = create_mailbox(
            "cloudmail",
            extra={
                "cloudmail_api_base": "https://cloudmail.example.com",
                "cloudmail_admin_email": "admin@example.com",
                "cloudmail_admin_password": "secret",
                "cloudmail_domain": "mail.example.com",
            },
        )
        account = MailboxAccount(email="demo@example.com", account_id="demo@example.com")

        code = mailbox.wait_for_code(account, timeout=5, otp_sent_at=1000)

        self.assertEqual(code, "777888")


def _json_response(payload: dict, status_code: int = 200):
    response = mock.Mock()
    response.status_code = status_code
    response.text = str(payload)
    response.json.return_value = payload
    return response


def _text_response(status_code: int, text: str):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    response.json.side_effect = ValueError("not json")
    return response


if __name__ == "__main__":
    unittest.main()
